"""Vote on AniGame through the same visible UI shown in the user's recording.

The application owns the 15-second countdown and final registration. This client
only authenticates, clicks the exact free-vote control, and observes the result.
"""

import argparse
import asyncio
import base64
import json
import os
import time
from contextlib import suppress
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import nodriver as uc

import anigame_page as page
import browser_environment
import ui_click
import vote as core
from recovery_state import SubmissionJournal


MAX_RETRIES = 5
AUTH_TIMEOUT = 75
READY_TIMEOUT = 45
RESULT_TIMEOUT = 100
POLL_INTERVAL = 1
RETRY_DELAY = 5
LABELS = ["Sign in with Discord", "Login with Discord", "Accedi con Discord"]
ROME = ZoneInfo("Europe/Rome")


async def snapshot(tab):
    try:
        return await page.snapshot(tab, core.evaluate)
    except Exception:
        return {"kind": "unknown", "ready": False}


async def read(tab, endpoint):
    try:
        return await page.read_endpoint(tab, core.evaluate, endpoint)
    except Exception:
        return {"status": None}


async def availability(tab):
    return page.availability(await read(tab, "/api/vote/availability"))


def expected_user_id(token):
    try:
        value = token.split(".", 1)[0]
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("ascii")
        return decoded if decoded.isascii() and decoded.isdigit() and 17 <= len(decoded) <= 20 else None
    except (ValueError, UnicodeError):
        return None


def session_matches(probe, token):
    if probe.get("status") != 200 or probe.get("authenticated") is not True:
        return False
    expected, observed = expected_user_id(token), probe.get("user_id")
    return not (expected and isinstance(observed, str) and observed != expected)


def valid_oauth(url):
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        callback = urlsplit(query.get("redirect_uri", [""])[0])
        return (
            parsed.scheme == "https" and parsed.hostname in {"discord.com", "www.discord.com"}
            and parsed.port in {None, 443} and not parsed.username and not parsed.password
            and parsed.path == "/oauth2/authorize"
            and query.get("client_id") == [page.BOT_ID]
            and callback.scheme == "https" and callback.hostname in page.HOSTS
            and callback.port in {None, 443} and not callback.username and not callback.password
            and callback.path == "/api/auth/discord/callback"
            and set(query.get("scope", [""])[0].split()) <= {"identify", "email", "guilds"}
        )
    except (ValueError, TypeError):
        return False


def load_cookies(raw, count):
    if not raw.strip():
        return [[] for _ in range(count)]
    lines = raw.strip().splitlines()
    if len(lines) != count:
        raise ValueError("ANIGAME_COOKIES_JSON must have one JSON array per account")
    result = []
    for line in lines:
        try:
            cookies = json.loads(line)
        except ValueError as exc:
            raise ValueError("Invalid ANIGAME_COOKIES_JSON") from exc
        if not isinstance(cookies, list):
            raise ValueError("AniGame cookies must be a JSON array")
        allowed = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise ValueError("Invalid AniGame cookie")
            domain = cookie.get("domain", "www.anigame.gg")
            name, value = cookie.get("name"), cookie.get("value")
            if (not isinstance(domain, str) or domain.lstrip(".").lower() not in page.HOSTS
                    or not isinstance(name, str) or not name or not isinstance(value, str)):
                raise ValueError("Cookie must belong to AniGame")
            # Browser verification belongs to the current machine/session.
            if name.lower().startswith(("cf_", "__cf")):
                continue
            allowed.append({**cookie, "domain": domain.lower()})
        result.append(allowed)
    return result


async def inject_cookies(browser, cookies):
    params = []
    for cookie in cookies:
        name = cookie["name"]
        options = {"name": name, "value": cookie["value"], "path": cookie.get("path") or "/",
                   "secure": True, "http_only": cookie.get("httpOnly") is True}
        if name.startswith("__Host-"):
            options["url"] = "https://" + cookie["domain"].lstrip(".") + "/"
            options["path"] = "/"
        else:
            options["domain"] = cookie["domain"]
        same_site = cookie.get("sameSite", "")
        if isinstance(same_site, str) and same_site.lower() in {"lax", "strict", "none"}:
            options["same_site"] = uc.cdp.network.CookieSameSite(same_site.capitalize())
        expires = cookie.get("expires")
        if type(expires) in {int, float} and expires > 0:
            options["expires"] = uc.cdp.network.TimeSinceEpoch(expires)
        params.append(uc.cdp.network.CookieParam(**options))
    if params:
        await asyncio.wait_for(browser.connection.send(uc.cdp.network.set_cookies(params)), 8)


class ReadOnlyGuard:
    """In inspection mode, deny application writes before authentication."""

    def __init__(self, tab):
        self.tab, self.denied = tab, 0

    async def paused(self, event):
        parsed = urlsplit(event.request.url)
        method = event.request.method.upper()
        permitted = (method in {"GET", "HEAD", "OPTIONS"}
                     or parsed.path.startswith(("/api/auth/", "/cdn-cgi/"))
                     or parsed.path == "/api/status")
        if parsed.hostname in page.HOSTS and not permitted:
            self.denied += 1
            await self.tab.send(uc.cdp.fetch.fail_request(
                event.request_id, uc.cdp.network.ErrorReason.BLOCKED_BY_CLIENT))
        else:
            await self.tab.send(uc.cdp.fetch.continue_request(event.request_id))

    async def start(self):
        self.tab.add_handler(uc.cdp.fetch.RequestPaused, self.paused)
        await self.tab.send(uc.cdp.fetch.enable(
            patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))


async def clear_challenge(tab, state):
    if not state.get("challenge"):
        return True
    print("  → AniGame/Discord verification is active")
    return await core.solve_turnstile(tab)


async def authenticate(browser, tab, token, cookies):
    if cookies:
        try:
            await inject_cookies(browser, cookies)
        except Exception as exc:
            print("  → Optional AniGame cookie import unavailable: " + type(exc).__name__)
        await core.navigate_page(tab, page.VOTE_URL)
    for _ in range(3):
        state = await snapshot(tab)
        if not await clear_challenge(tab, state):
            return False, "blocked"
        probe = await read(tab, "/api/auth/me")
        if session_matches(probe, token):
            print("  → AniGame session authenticated")
            return True, None
        if state.get("login"):
            break
        await asyncio.sleep(1)

    print("  → Preparing the existing Discord account session")
    discord_tab = await asyncio.wait_for(browser.get(core.DISCORD_LOGIN_URL, new_tab=True), 15)
    try:
        await asyncio.sleep(2)
        url = urlsplit(await core.current_url(discord_tab))
        if url.scheme != "https" or url.hostname not in {"discord.com", "www.discord.com"}:
            return False, "auth_failed"
        await core.evaluate(discord_tab, """(() => {
            const token = __TOKEN__;
            localStorage.setItem('token', JSON.stringify(token));
            localStorage.setItem('tokens', JSON.stringify({default:token}));
        })()""".replace("__TOKEN__", json.dumps(token)))
        await asyncio.wait_for(discord_tab.reload(), 10)
        await asyncio.sleep(3)
    finally:
        with suppress(Exception):
            await asyncio.wait_for(discord_tab.close(), 8)
        await browser_environment.foreground(tab)

    state = await snapshot(tab)
    if not state.get("login"):
        await core.navigate_page(tab, page.VOTE_URL)
        await asyncio.sleep(2)
    probe = await read(tab, "/api/auth/me")
    if session_matches(probe, token):
        return True, None
    if not await core._click_exact_element(tab, 'button,a,[role="button"]', LABELS, "data-anigame-login"):
        return False, "auth_failed"

    deadline = asyncio.get_running_loop().time() + AUTH_TIMEOUT
    async with asyncio.timeout(AUTH_TIMEOUT):
        while asyncio.get_running_loop().time() < deadline:
            url = await core.current_url(tab)
            state = await snapshot(tab)
            if not await clear_challenge(tab, state):
                return False, "blocked"
            if state.get("kind") in {"vote", "confirmation"}:
                if session_matches(await read(tab, "/api/auth/me"), token):
                    print("  → Discord OAuth returned to the authenticated AniGame page")
                    return True, None
            elif valid_oauth(url):
                # Only AniGame's own Discord app and inspected callback are accepted.
                if await core._mark_exact_element(tab, "button", ["Authorize", "Authorise", "Autorizza"], "data-anigame-authorize"):
                    await core._click_exact_element(tab, "button", ["Authorize", "Authorise", "Autorizza"], "data-anigame-authorize")
            elif urlsplit(url).hostname not in {"discord.com", "www.discord.com", *page.HOSTS}:
                return False, "auth_failed"
            await asyncio.sleep(POLL_INTERVAL)
    return False, "auth_failed"


def result(status, detail, **extra):
    return {"bot_id": page.BOT_ID, "status": status, "detail": detail, **extra}


def cooldown_result(state, remote):
    if (state.get("kind") != "vote" or not state.get("ready") or state.get("login")
            or state.get("challenge") or state.get("registering") or state.get("error")
            or state.get("vote_enabled")):
        return None
    seconds = state.get("cooldown_seconds")
    if seconds is None:
        return None
    # Prefer the server's exact original deadline; never reset twelve hours
    # merely because a manual/retry Action sees an existing cooldown.
    retry_at = remote.get("retry_at") if remote.get("available") is False else None
    retry_at = retry_at or (int(time.time()) + seconds + 65)
    return result("cooldown", "AniGame cooldown confirmed", retry_at=retry_at)


async def wait_for_result(tab):
    acknowledged = 0
    last_countdown = None
    deadline = asyncio.get_running_loop().time() + RESULT_TIMEOUT
    try:
        async with asyncio.timeout(RESULT_TIMEOUT):
            while asyncio.get_running_loop().time() < deadline:
                state = await snapshot(tab)
                if not state.get("visible"):
                    await browser_environment.foreground(tab)
                if state.get("challenge"):
                    if not await clear_challenge(tab, state):
                        return result("blocked", "AniGame verification persisted after Vote", vote_submitted=True)
                    continue
                if state.get("login"):
                    return result("uncertain", "AniGame session expired after Vote", vote_submitted=True)
                if state.get("error"):
                    return result("uncertain", "AniGame reported an application error after Vote", vote_submitted=True)
                if state.get("countdown") != last_countdown:
                    last_countdown = state.get("countdown")
                    if last_countdown is not None:
                        print(f"  → AniGame registration countdown: {last_countdown}s")
                if state.get("kind") == "confirmation" and state.get("confirmed") and not state.get("error"):
                    acknowledged += 1
                    if acknowledged >= 2:
                        remote = await availability(tab)
                        if remote.get("available") is not True:
                            print("  → AniGame Vote confirmed page and reward receipt observed")
                            return result("success", "AniGame Vote confirmed and rewards added",
                                retry_at=remote.get("retry_at") or int(time.time()) + 12 * 3600 + 30)
                else:
                    acknowledged = 0
                if state.get("kind") == "external":
                    return result("uncertain", "Unexpected navigation after Vote", vote_submitted=True)
                await asyncio.sleep(POLL_INTERVAL)
    except TimeoutError:
        pass
    return result("uncertain", "AniGame registration did not produce a verified receipt", vote_submitted=True)


async def vote_once(tab, journal=None, *, inspect_only=False, prior_pending=False):
    await browser_environment.foreground(tab)
    ready_count, cooldown_count = 0, 0
    already_registering = False
    deadline = asyncio.get_running_loop().time() + READY_TIMEOUT
    async with asyncio.timeout(READY_TIMEOUT + 15):
        while asyncio.get_running_loop().time() < deadline:
            state = await snapshot(tab)
            if not await clear_challenge(tab, state):
                return result("blocked", "AniGame challenge before free Vote")
            remote = await availability(tab)
            cooldown = cooldown_result(state, remote)
            cooldown_count = cooldown_count + 1 if cooldown else 0
            if cooldown_count >= 2:
                print("  → Existing AniGame cooldown confirmed; preserving its deadline")
                return cooldown
            if (inspect_only and state.get("ready") and state.get("kind") == "vote" and not state.get("login")
                    and (state.get("vote_present") or state.get("registering") or state.get("cooldown_seconds") is not None)):
                print("  Inspection: " + json.dumps({key: state.get(key) for key in (
                    "kind", "vote_present", "vote_enabled", "registering", "countdown", "cooldown_seconds")}, sort_keys=True))
                return result("inspected", "Authenticated AniGame page inspected; no Vote input")
            if state.get("registering"):
                # A restored application may already be registering a vote.
                already_registering = True
                break
            eligible = (state.get("kind") == "vote" and state.get("ready")
                        and state.get("vote_enabled") and not state.get("login")
                        and not state.get("challenge") and not state.get("error"))
            ready_count = ready_count + 1 if eligible else 0
            if ready_count >= (2 if prior_pending else 1):
                if remote.get("available") is False and remote.get("retry_at"):
                    # Let the application's disabled control catch up to a
                    # freshly observed server cooldown rather than pressing it.
                    ready_count = 0
                else:
                    if prior_pending and journal is not None:
                        journal.clear_current()
                    break
            await asyncio.sleep(POLL_INTERVAL)
        else:
            return result("error", "AniGame free Vote did not become actionable")

    if inspect_only:
        return result("error", "Read-only inspection could not observe the AniGame voting page")
    if already_registering:
        return await wait_for_result(tab)

    async def reacquire():
        return bool(await core.evaluate(tab, page.MARK_VOTE_JS))

    if journal is not None:
        journal.before_press()
    receipt = await ui_click.click_control(tab, core.evaluate, '[data-anigame-vote="1"]',
        kind="control", reacquire=reacquire, timeout=10)
    print("  → AniGame free Vote input: " + json.dumps(receipt, sort_keys=True))
    if not receipt.get("input_sent"):
        if journal is not None:
            journal.clear_current()
        return result("error", "No AniGame Vote mouse input was sent", vote_submitted=False)
    # Keep this document open. The site owns the countdown and automatically
    # navigates to its receipt; a reload here would interrupt registration.
    return await wait_for_result(tab)


async def process_account(token, cookies, journal=None, *, inspect_only=False):
    account_id = core.account_fingerprint(token)
    record = journal.select(token, page.BOT_ID) if journal else None
    if record and record["kind"] == "complete" and not inspect_only:
        return result("cooldown", "Confirmed AniGame vote retained from an earlier Action",
                      retry_at=record["until"], account_id=account_id)
    last = result("error", "AniGame account was not processed")
    for attempt in range(1, (1 if inspect_only else MAX_RETRIES) + 1):
        browser = None
        print(f"  → AniGame account {account_id}, attempt {attempt}/{1 if inspect_only else MAX_RETRIES}")
        try:
            browser = await core.start_browser(initial_url=page.VOTE_URL)
            tab = browser.tabs[0]
            if inspect_only:
                await ReadOnlyGuard(tab).start()
            if page.page_kind(await core.current_url(tab)) not in {"vote", "confirmation"}:
                await core.navigate_page(tab, page.VOTE_URL)
            ok, failure = await authenticate(browser, tab, token, cookies)
            if not ok:
                last = result(failure, "AniGame authentication could not be established")
            else:
                record = journal.select(token, page.BOT_ID) if journal else None
                last = await vote_once(tab, journal, inspect_only=inspect_only,
                    prior_pending=bool(record and record["kind"] == "pending"))
                if journal is not None and not inspect_only and last["status"] in {"success", "cooldown"}:
                    journal.record_result(last)
                if last["status"] in {"success", "cooldown", "inspected"}:
                    return {**last, "account_id": account_id}
        except core.BrowserStartupError:
            last = {**result("error", "Browser startup failed: AniGame browser unavailable"),
                    "bot_id": "all"}
            # start_browser already exhausted its own startup retries. Let the
            # existing fresh-Action recovery obtain a new runner immediately.
            break
        except Exception as exc:
            print("  → AniGame attempt ended: " + type(exc).__name__)
            last = result("uncertain" if journal and journal.select(token, page.BOT_ID) else "error",
                          "AniGame browser operation could not be verified")
        finally:
            if browser is not None:
                await core.close_browser_safely(browser, "AniGame account")
        if not inspect_only and attempt < MAX_RETRIES:
            print("  → Retrying AniGame with a fresh browser; checking persisted state before another Vote")
            await asyncio.sleep(RETRY_DELAY)
    return {**last, "account_id": account_id}


def publish_summary(results, inspect_only):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## AniGame vote inspection" if inspect_only else "## AniGame vote result", "",
             "| Account | Result |", "|---|---|"]
    lines += [f"| {item['account_id']} | {item['status']} |" for item in results]
    lines += ["", "Target: https://www.anigame.gg/vote.",
              "The site owns the visible countdown. Success requires its Vote confirmed page and reward receipt.", ""]
    with suppress(OSError):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))


async def main(*, inspect_only=False):
    raw_tokens = core.consume_secret("TOKENS")
    raw_cookies = core.consume_secret("ANIGAME_COOKIES_JSON")
    telegram_token = core.consume_secret("TG_BOT_TOKEN")
    telegram_chat = core.consume_secret("TG_CHAT_ID")
    core.SENSITIVE_VALUES = [raw_tokens, raw_cookies, telegram_token, telegram_chat]
    core.scrub_browser_environment()
    if inspect_only:
        core.TG_BOT_TOKEN = core.TG_CHAT_ID = ""
        core.PRIVACY_DISMISS_REPORTED = True
    else:
        core.TG_BOT_TOKEN, core.TG_CHAT_ID = telegram_token.strip(), telegram_chat.strip()
    tokens = core.load_tokens(raw_tokens)
    if not tokens:
        print("AniGame requires the existing TOKENS account secret")
        return 1
    try:
        cookies = load_cookies(raw_cookies, len(tokens))
    except ValueError as exc:
        print(str(exc))
        return 1
    core.SENSITIVE_VALUES.extend(cookie["value"] for group in cookies for cookie in group)
    recovery_path = os.environ.get("RECOVERY_STATE_FILE", "")
    journal = (SubmissionJournal(recovery_path, tokens, [page.BOT_ID], namespace=page.RECOVERY_NAMESPACE)
               if recovery_path and not inspect_only else None)
    print("AniGame Auto Vote — " + datetime.now(ROME).strftime("%Y-%m-%d %H:%M:%S %Z"))
    print("Target: " + page.VOTE_URL)
    results = []
    for index, token in enumerate(tokens):
        results.append(await process_account(token, cookies[index], journal, inspect_only=inspect_only))
    publish_summary(results, inspect_only)
    if inspect_only:
        return 0 if all(item["status"] in {"cooldown", "inspected"} for item in results) else 1
    all_results = [[item] for item in results]
    next_vote_at = core.write_next_vote_state(all_results)
    if next_vote_at is not None:
        print("Next AniGame vote: " + datetime.fromtimestamp(next_vote_at, ROME).strftime("%Y-%m-%d %H:%M:%S %Z"))
    core.write_browser_startup_retry_state(all_results)
    core.write_protection_retry_state(all_results)
    lines = ["🗳️ <b>AniGame Auto Vote Report</b>"]
    lines += [f"{escape(item['account_id'])}: {escape(item['status'])} — {escape(item['detail'])}" for item in results]
    core.send_notification("\n".join(lines))
    return 1 if core.has_business_failure(all_results) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true", help="Authenticate and inspect; never press Vote")
    args = parser.parse_args()
    raise SystemExit(uc.loop().run_until_complete(main(inspect_only=args.inspect)))
