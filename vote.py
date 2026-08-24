#!/usr/bin/env python3
"""Automated top.gg voting via nodriver, cookie-first auth, and Discord OAuth fallback."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import nodriver as uc
import requests

WIB = timezone(timedelta(hours=7))
DEFAULT_BOT_IDS = ["830530156048285716"]
TIMEOUT_OAUTH_SEC = 25
TIMEOUT_VOTE_SEC = 30
DELAY_BETWEEN_BOTS_SEC = 3
DELAY_BETWEEN_ACCOUNTS_SEC = 5
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10
FINAL_STATUSES = frozenset({"success", "cooldown", "captcha_required"})
COMPLETED_STATUSES = frozenset({"success", "cooldown"})
TRANSIENT_STATUSES = frozenset({"error", "auth_failed", "uncertain"})
BROWSER_START_RETRIES = 10
BROWSER_START_RETRY_SEC = 3
AUTHENTICATED = "authenticated"
AUTH_INVALID = "invalid"
AUTH_CAPTCHA_REQUIRED = "captcha_required"
TELEGRAM_MESSAGE_LIMIT = 3500
DIAGNOSTIC_DETAIL_LIMIT = 600
BROWSER_RETRY_REASON = "browser_startup_failed"
BROWSER_STARTUP_DETAIL_PREFIX = "Browser startup failed:"
COOLDOWN_SAFETY_BUFFER_SEC = 5 * 60
SUCCESS_NEXT_VOTE_DELAY_SEC = 12 * 60 * 60 + 30
MIN_COOLDOWN_SEC = 60
MAX_COOLDOWN_SEC = 24 * 60 * 60
COOLDOWN_UNITS_SEC = {
    "second": 1,
    "minute": 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
}
COOLDOWN_PATTERN = re.compile(
    r"(?:you\s+)?can\s+vote\s+again\s+in\s+"
    r"(?:about\s+|approximately\s+)?"
    r"(?P<amount>\d+(?:\.\d+)?|a|an|one)\s*"
    r"(?P<unit>seconds?|minutes?|hours?|days?)\b",
    re.IGNORECASE,
)


class BrowserStartupError(RuntimeError):
    """Credential-free nodriver startup failure safe for workflow logs."""


class BrowserCleanupError(RuntimeError):
    """Browser process or sensitive profile cleanup failed."""


TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
SENSITIVE_VALUES: list[str] = []
PRIVACY_DISMISS_REPORTED = False


def _load_dotenv(path: str | Path = ".env") -> None:
    # ponytail: minimal stdlib parser. secrets stay local (.gitignore).
    # Supports KEY=VALUE, optional quotes, avoids overwriting existing env.
    # Upgrade to python-dotenv only if comment values / variable expansion needed.
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv()

DEBUG = os.environ.get("DEBUG", "").strip() == "1"
SEND_ERROR_SCREENSHOTS = os.environ.get("SEND_ERROR_SCREENSHOTS", "").strip() == "1"


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"    [dbg] {msg}")


def safe_exception_detail(exc: Exception) -> str:
    """Redact configured credentials before exposing a short diagnostic."""
    return redact_diagnostic(str(exc), 200)


def consume_secret(name: str) -> str:
    """Read a secret once, unlink its file, and remove all environment references."""
    file_path = os.environ.pop(f"{name}_FILE", "").strip()
    env_value = os.environ.pop(name, "")
    if not file_path:
        return env_value
    path = Path(file_path)
    try:
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def scrub_browser_environment() -> None:
    for name in ("TOKENS", "TOPGG_COOKIES_JSON", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        os.environ.pop(name, None)
        os.environ.pop(f"{name}_FILE", None)


async def browser_screenshot(tab: Any, path: str, *, required: bool = False) -> str | None:
    if not required and not SEND_ERROR_SCREENSHOTS:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await tab.save_screenshot(filename=path, format="png")
        return path
    except Exception as exc:
        dbg(f"Browser screenshot failed: {type(exc).__name__}")
        if required:
            print("  ⚠️  Could not capture CAPTCHA browser screenshot")
        return None


async def error_screenshot(tab: Any, path: str) -> str | None:
    return await browser_screenshot(tab, path)


def send_telegram_photo(path: str, caption: str = "") -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        with open(path, "rb") as file:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": file},
                timeout=30,
            )
        try:
            payload = response.json()
        except ValueError:
            return False
        return response.status_code == 200 and payload.get("ok") is True
    except Exception as exc:
        dbg(f"Telegram photo failed: {type(exc).__name__}")
        return False


async def notify_error_screenshot(bot_id: str, path: str, detail: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    caption = f"❌ Vote failed for {escape(str(bot_id))}\n{escape(str(detail))}"
    try:
        sent = await asyncio.to_thread(send_telegram_photo, path, caption)
        print("  📸 Error screenshot sent to Telegram" if sent else "  ⚠️  Could not send error screenshot to Telegram")
    finally:
        with suppress(OSError):
            Path(path).unlink()


async def report_privacy_dismiss_failure(tab: Any, detail: str) -> None:
    global PRIVACY_DISMISS_REPORTED
    if PRIVACY_DISMISS_REPORTED:
        return
    PRIVACY_DISMISS_REPORTED = True
    safe_detail = redact_diagnostic(detail, 300)
    print(f"  ⚠️  Privacy modal dismiss failed: {safe_detail}")
    path = await browser_screenshot(
        tab,
        "screenshots/privacy_overlay_dismiss_failed.png",
        required=True,
    )
    if not path:
        if TG_BOT_TOKEN and TG_CHAT_ID:
            send_notification(f"⚠️ Privacy modal dismiss failed\n{escape(safe_detail)}")
        return
    caption = f"⚠️ <b>Privacy modal dismiss failed</b>\n{escape(safe_detail)}"
    try:
        if TG_BOT_TOKEN and TG_CHAT_ID:
            sent = await asyncio.to_thread(send_telegram_photo, path, caption)
            print(
                "  📸 Privacy modal failure screenshot sent to Telegram"
                if sent else "  ⚠️  Could not send privacy modal screenshot to Telegram"
            )
    finally:
        with suppress(OSError):
            Path(path).unlink()


async def send_captcha_screenshots(all_results: list[list[dict]]) -> int:
    """Send each CAPTCHA screenshot after the text report, then delete local evidence."""
    sent_count = 0
    handled_paths: set[str] = set()
    for account_results in all_results:
        for result in account_results:
            if not is_captcha_related_result(result):
                continue
            path = result.get("screenshot_path")
            if not isinstance(path, str) or not path or path in handled_paths:
                continue
            handled_paths.add(path)
            account_id = escape(str(result.get("account_id", "?")))
            bot_id = escape(str(result.get("bot_id", "?")))
            detail = escape(str(result.get("detail", "CAPTCHA required")))
            caption = (
                "🔒 <b>CAPTCHA Browser Screenshot</b>\n"
                f"👤 Account {account_id}\n"
                f"🤖 {bot_id}: {detail}"
            )
            try:
                sent = await asyncio.to_thread(send_telegram_photo, path, caption)
                sent_count += int(sent)
                print(
                    "  📸 CAPTCHA screenshot sent to Telegram"
                    if sent else "  ⚠️  Could not send CAPTCHA screenshot to Telegram"
                )
            finally:
                with suppress(OSError):
                    Path(path).unlink()
    return sent_count


async def send_auth_failure_screenshots(all_results: list[list[dict]]) -> int:
    """Send final auth failure screenshots after the text report, then delete local evidence."""
    sent_count = 0
    handled_paths: set[str] = set()
    for account_results in all_results:
        for result in account_results:
            if result.get("status") != "auth_failed":
                continue
            path = result.get("screenshot_path")
            if not isinstance(path, str) or not path or path in handled_paths:
                continue
            handled_paths.add(path)
            account_id = escape(str(result.get("account_id", "?")))
            detail = escape(str(result.get("detail", "Top.gg authentication failed")))
            caption = (
                "❌ <b>Auth Failure Browser Screenshot</b>\n"
                f"👤 Account {account_id}\n"
                f"{detail}"
            )
            try:
                sent = await asyncio.to_thread(send_telegram_photo, path, caption)
                sent_count += int(sent)
                print(
                    "  📸 Auth failure screenshot sent to Telegram"
                    if sent else "  ⚠️  Could not send auth failure screenshot to Telegram"
                )
            finally:
                with suppress(OSError):
                    Path(path).unlink()
    return sent_count


def load_tokens(raw: str | None = None) -> list[str]:
    if raw is None:
        raw = os.environ.get("TOKENS", "")
    raw = raw.strip()
    return [line.strip() for line in raw.splitlines() if line.strip()] if raw else []


def load_bot_ids() -> list[str]:
    raw = os.environ.get("BOT_IDS", "").strip()
    ids = [line.strip() for line in raw.splitlines() if line.strip()] if raw else DEFAULT_BOT_IDS
    invalid = [bot_id for bot_id in ids if not (bot_id.isdigit() and 17 <= len(bot_id) <= 20)]
    if invalid:
        raise ValueError(f"Invalid BOT_IDS value: {invalid[0]!r}; expected a 17-20 digit Discord ID")
    return ids


def account_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def is_captcha_related_result(result: dict) -> bool:
    return (
        result.get("status") == "captcha_required"
        or "captcha" in str(result.get("detail", "")).casefold()
    )


def is_retryable_result(result: dict) -> bool:
    return result.get("status") in TRANSIENT_STATUSES


def retryable_bot_ids(results: list[dict]) -> list[str]:
    return [
        str(result["bot_id"])
        for result in results
        if result.get("bot_id") not in {None, "all"} and is_retryable_result(result)
    ]


def parse_cooldown_seconds(text: str) -> int | None:
    """Parse bounded top.gg relative cooldown text; reject unrelated durations."""
    normalized = " ".join(text.split())
    match = COOLDOWN_PATTERN.search(normalized)
    if not match:
        return None
    amount_text = match.group("amount").lower()
    amount = 1.0 if amount_text in {"a", "an", "one"} else float(amount_text)
    unit = match.group("unit").lower().rstrip("s")
    seconds = round(amount * COOLDOWN_UNITS_SEC[unit])
    return seconds if MIN_COOLDOWN_SEC <= seconds <= MAX_COOLDOWN_SEC else None


def cooldown_retry_at(text: str, now: datetime | None = None) -> int | None:
    seconds = parse_cooldown_seconds(text)
    if seconds is None:
        return None
    current = now or datetime.now(timezone.utc)
    return int(current.timestamp()) + seconds + COOLDOWN_SAFETY_BUFFER_SEC


def cooldown_result(bot_id: str, text: str, now: datetime | None = None) -> dict:
    retry_at = cooldown_retry_at(text, now)
    detail = "Cooldown active"
    result = {"bot_id": bot_id, "status": "cooldown", "detail": detail}
    if retry_at is not None:
        result["retry_at"] = retry_at
        result["detail"] = f"Cooldown active; retry after {format_retry_at(retry_at)}"
    return result

def successful_vote_result(bot_id: str) -> dict:
    confirmed_at = int(datetime.now(timezone.utc).timestamp())
    return {
        "bot_id": bot_id,
        "status": "success",
        "detail": "Vote successful",
        "retry_at": confirmed_at + SUCCESS_NEXT_VOTE_DELAY_SEC,
    }


def earliest_retry_at(all_results: list[list[dict]]) -> int | None:
    retry_times = [
        result.get("retry_at")
        for account_results in all_results
        for result in account_results
        if result.get("status") in {"cooldown", "success"}
        and isinstance(result.get("retry_at"), int)
    ]
    return min(retry_times) if retry_times else None


def format_retry_at(retry_at: int) -> str:
    return datetime.fromtimestamp(retry_at, WIB).strftime("%Y-%m-%d %H:%M WIB")


def write_next_vote_state(all_results: list[list[dict]], path_value: str | None = None) -> int | None:
    path_text = path_value if path_value is not None else os.environ.get("NEXT_VOTE_STATE_FILE", "")
    retry_at = earliest_retry_at(all_results)
    if retry_at is None or not path_text.strip():
        return retry_at
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"next_vote_at": retry_at}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return retry_at


def is_browser_startup_result(result: dict) -> bool:
    return (
        result.get("bot_id") == "all"
        and result.get("status") == "error"
        and str(result.get("detail", "")).startswith(BROWSER_STARTUP_DETAIL_PREFIX)
    )


def should_request_browser_startup_retry(all_results: list[list[dict]]) -> bool:
    return bool(all_results) and all(
        len(account_results) == 1 and is_browser_startup_result(account_results[0])
        for account_results in all_results
    )


def write_browser_startup_retry_state(
    all_results: list[list[dict]], path_value: str | None = None
) -> bool:
    path_text = path_value if path_value is not None else os.environ.get("BROWSER_RETRY_STATE_FILE", "")
    if not path_text.strip() or not should_request_browser_startup_retry(all_results):
        return False
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"reason": BROWSER_RETRY_REASON}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return True


def _normalize_cookie(cookie: dict, line_number: int = 0) -> dict:
    prefix = f"TOPGG_COOKIES_JSON line {line_number}" if line_number else "Cookie"
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{prefix} Auth.js cookie requires a non-empty name")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{prefix} Auth.js cookie {name!r} requires a non-empty value")

    domain = cookie.get("domain", ".top.gg")
    path = cookie.get("path", "/")
    if not isinstance(domain, str) or domain.lower() not in {"top.gg", ".top.gg"}:
        raise ValueError(f"{prefix} Auth.js cookie {name!r} has invalid domain")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"{prefix} Auth.js cookie {name!r} has invalid path")
    for field in ("secure", "httpOnly"):
        if field in cookie and not isinstance(cookie[field], bool):
            raise ValueError(f"{prefix} Auth.js cookie {name!r} has invalid {field}")

    normalized = {
        "name": name,
        "value": value,
        "domain": domain.lower(),
        "path": path,
        "secure": True,
        "httpOnly": "session-token" in name.lower() or cookie.get("httpOnly", False),
    }
    expiration = cookie.get("expirationDate", cookie.get("expires"))
    if expiration is not None:
        if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
            raise ValueError(f"{prefix} Auth.js cookie {name!r} has invalid expiry")
        if expiration > 0:
            normalized["expires"] = float(expiration)
    raw_same_site = cookie.get("sameSite", "Lax")
    same_site = "unspecified" if raw_same_site is None else str(raw_same_site).lower()
    same_site_map = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    if same_site not in same_site_map:
        raise ValueError(f"{prefix} Auth.js cookie {name!r} has invalid sameSite")
    normalized["sameSite"] = same_site_map[same_site]
    return normalized


def load_topgg_cookies(
    expected_accounts: int | None = None,
    raw: str | None = None,
) -> list[list[dict]]:
    if raw is None:
        raw = os.environ.get("TOPGG_COOKIES_JSON", "")
    if not raw.strip():
        return []
    lines = raw.strip("\r\n").splitlines()
    if expected_accounts is not None and len(lines) != expected_accounts:
        raise ValueError(
            "TOPGG_COOKIES_JSON line count must match TOKENS; use [] for accounts without cookies"
        )

    account_cookies = []
    for index, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            raise ValueError(f"TOPGG_COOKIES_JSON line {index} is blank; use [] instead")
        if line == "[]":
            account_cookies.append([])
            continue
        try:
            cookies = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid TOPGG_COOKIES_JSON at line {index}") from exc
        if not isinstance(cookies, list):
            raise ValueError(f"TOPGG_COOKIES_JSON line {index} must be a JSON array")

        authjs = []
        for item_index, cookie in enumerate(cookies, 1):
            if not isinstance(cookie, dict):
                raise ValueError(
                    f"TOPGG_COOKIES_JSON line {index} item {item_index} must be an object"
                )
            domain = str(cookie.get("domain", "")).lower()
            name = str(cookie.get("name", ""))
            if domain in {"top.gg", ".top.gg"} and "authjs" in name.lower():
                authjs.append(_normalize_cookie(cookie, index))
        if not authjs:
            raise ValueError(
                f"TOPGG_COOKIES_JSON line {index} contains no top.gg Auth.js cookies"
            )
        account_cookies.append(authjs)
    return account_cookies


def split_telegram_message(message: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split on report lines; hard-split only an individual oversized line."""
    if not message:
        return []
    chunks = []
    current = ""
    for line in message.splitlines():
        pieces = [line[index:index + limit] for index in range(0, len(line), limit)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def send_notification(message: str) -> bool:
    print("\n" + "=" * 45)
    print(message)
    print("=" * 45)
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️  TG_BOT_TOKEN / TG_CHAT_ID not set — skip notification.")
        return False
    all_sent = True
    for chunk in split_telegram_message(message):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            delivered = response.status_code == 200 and payload.get("ok") is True
            all_sent = all_sent and delivered
            if not delivered:
                print(f"⚠️  Notification chunk failed: {response.status_code}")
        except Exception as exc:
            all_sent = False
            dbg(f"Telegram message failed: {type(exc).__name__}")
            print("⚠️  Notification exception; enable DEBUG for error type.")
    if all_sent:
        print("📨 Telegram notification sent.")
    return all_sent


async def evaluate(tab: Any, expression: str) -> Any:
    remote_object, exception = await tab.send(uc.cdp.runtime.evaluate(
        expression=expression,
        user_gesture=True,
        await_promise=True,
        return_by_value=True,
        allow_unsafe_eval_blocked_by_csp=True,
    ))
    if exception:
        raise RuntimeError("JavaScript evaluation failed")
    return remote_object.value if remote_object else None


async def body_text(tab: Any) -> str:
    return str(await evaluate(tab, "document.body ? document.body.innerText : ''") or "")


async def current_url(tab: Any) -> str:
    return str(await evaluate(tab, "location.href") or "")


def url_has_domain(url: str, domain: str) -> bool:
    """Match exact hostname or its subdomain, never URL query/path text."""
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


async def wait_for_domain(tab: Any, domain: str, timeout: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if url_has_domain(await current_url(tab), domain):
            return True
        await asyncio.sleep(1)
    return False


async def _mark_exact_element(tab: Any, selector: str, texts: list[str], marker: str) -> bool:
    script = f"""(() => {{
        const wanted = {json.dumps(texts)};
        const nodes = [...document.querySelectorAll({json.dumps(selector)})];
        const element = nodes.find(node => wanted.includes((node.textContent || '').trim()));
        if (!element) return false;
        element.setAttribute({json.dumps(marker)}, '1');
        return true;
    }})()"""
    return bool(await evaluate(tab, script))


async def _click_marked(tab: Any, marker: str) -> bool:
    await dismiss_privacy_overlay(tab)
    try:
        element = await tab.select(f'[{marker}="1"]', timeout=2)
        await element.scroll_into_view()
        await element.click()
        return True
    except Exception as exc:
        dbg(f"Marked click failed: {type(exc).__name__}")
        return False


async def dismiss_privacy_overlay(tab: Any) -> bool:
    try:
        result = await evaluate(tab, """(() => {
            const body = document.body ? document.body.innerText.toLowerCase() : '';
            const looksLikeConsent = body.includes('we value your privacy') ||
                body.includes('partners store and/or access information') ||
                body.includes('personalised ads and content');
            if (!looksLikeConsent) return {present: false, dismissed: false, reason: 'not_present'};
            const labels = new Set(['agree', 'accept', 'accept all', 'allow all', 'i agree']);
            const direct = document.querySelector('#accept-btn');
            const controls = [...document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')];
            const target = direct || controls.find(el => {
                const text = [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.getAttribute('aria-label'),
                    el.id,
                ].filter(Boolean).join(' ').trim().toLowerCase();
                return labels.has(text) || text.includes('agree') || text.includes('accept');
            });
            if (!target) return {present: true, dismissed: false, reason: 'consent_button_not_found'};
            try {
                target.click();
                return {present: true, dismissed: true, reason: 'clicked'};
            } catch (error) {
                return {present: true, dismissed: false, reason: `click_failed:${error && error.name ? error.name : 'Error'}`};
            }
        })()""")
    except Exception as exc:
        detail = f"JavaScript check failed: {type(exc).__name__}: {safe_exception_detail(exc)}"
        dbg(f"Privacy overlay dismiss skipped: {type(exc).__name__}")
        await report_privacy_dismiss_failure(tab, detail)
        return False
    if not isinstance(result, dict) or not result.get("present"):
        return False
    if result.get("dismissed"):
        return True
    await report_privacy_dismiss_failure(tab, str(result.get("reason", "unknown dismiss failure")))
    return False


async def settle_privacy_overlay(tab: Any, attempts: int = 4, delay: float = 0.75) -> bool:
    dismissed = False
    for attempt in range(attempts):
        dismissed = await dismiss_privacy_overlay(tab) or dismissed
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    return dismissed


async def inject_topgg_cookies(browser: Any, cookies: list[dict]) -> None:
    params = []
    for cookie in cookies:
        same_site = uc.cdp.network.CookieSameSite(cookie["sameSite"])
        expires = uc.cdp.network.TimeSinceEpoch(cookie["expires"]) if cookie.get("expires") else None
        params.append(uc.cdp.network.CookieParam(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie["domain"],
            path=cookie["path"],
            secure=cookie["secure"],
            http_only=cookie["httpOnly"],
            same_site=same_site,
            expires=expires,
        ))
    if params:
        await browser.cookies.set_all(params)


async def is_topgg_authenticated(tab: Any) -> bool:
    result = await evaluate(tab, """(async () => {
        try {
            const response = await fetch('/api/auth/session', {credentials: 'include'});
            if (!response.ok) return false;
            const session = await response.json();
            return Boolean(session && session.user);
        } catch (_) {
            return false;
        }
    })()""")
    return bool(result)


async def topgg_auth_state(tab: Any) -> str:
    await dismiss_privacy_overlay(tab)
    if await is_topgg_authenticated(tab):
        return AUTHENTICATED
    if await is_turnstile_present(tab):
        if not await solve_turnstile(tab):
            return AUTH_CAPTCHA_REQUIRED
        await asyncio.sleep(2)
        if await is_topgg_authenticated(tab):
            return AUTHENTICATED
    return AUTH_INVALID


async def login_with_cookies(tab: Any, cookies: list[dict], bot_ids: list[str]) -> str:
    if not cookies:
        return AUTH_INVALID
    print("  → Injecting top.gg Auth.js cookies...")
    await inject_topgg_cookies(tab.browser, cookies)
    await tab.get(f"https://top.gg/bot/{bot_ids[0]}/vote")
    await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Authenticated via top.gg cookies")
    elif state == AUTH_CAPTCHA_REQUIRED:
        print("  🔒 CAPTCHA blocked top.gg cookie authentication")
    else:
        print("  ⚠️  Cookie session invalid or expired")
    return state


async def _handle_discord_oauth(tab: Any) -> str:
    print("  → Handling Discord OAuth dialog...")
    for attempt in range(12):
        if url_has_domain(await current_url(tab), "top.gg"):
            return AUTHENTICATED
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return AUTH_CAPTCHA_REQUIRED
        marker = "data-auto-oauth"
        if await _mark_exact_element(tab, "button", ["Authorize", "Authorise"], marker):
            if await _click_marked(tab, marker):
                dbg(f"Authorize clicked (attempt {attempt + 1})")
                return AUTHENTICATED
        await evaluate(tab, """(() => {
            const nodes = [...document.querySelectorAll('div')]
                .filter(el => el.scrollHeight > el.clientHeight);
            const target = nodes.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            if (target) target.scrollTop += 400;
            else window.scrollBy(0, 400);
        })()""")
        await asyncio.sleep(1.5)
    return AUTH_INVALID


async def discord_oauth_login(tab: Any, token: str, bot_ids: list[str]) -> str:
    print("  → Setting Discord session for top.gg login...")
    await tab.get(f"https://top.gg/bot/{bot_ids[0]}/vote")
    await asyncio.sleep(2)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state != AUTH_INVALID:
        print("  ✅ Already logged into top.gg" if state == AUTHENTICATED else "  🔒 CAPTCHA blocked top.gg session probe")
        return state

    # ponytail: avoid discord.com/login navigation; token goes into discord.com
    # localStorage via an iframe so top.gg can pick up the Discord session on Login.
    await evaluate(tab, f"""(() => {{
        const token = {json.dumps(token)};
        const frame = document.body.appendChild(document.createElement('iframe'));
        frame.style.display = 'none';
        frame.src = 'https://discord.com';
    }})()""")
    await asyncio.sleep(1.5)
    await evaluate(tab, f"""(() => {{
        const token = {json.dumps(token)};
        const frame = document.querySelector('iframe[src=\"https://discord.com\"]');
        try {{
            if (frame && frame.contentWindow) {{
                frame.contentWindow.localStorage.setItem('token', JSON.stringify(token));
                frame.contentWindow.localStorage.setItem('tokens', JSON.stringify({{"default": token}}));
            }}
        }} catch (_) {{}}
        if (frame) frame.remove();
    }})()""")
    await tab.reload()
    await asyncio.sleep(2)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Session established without OAuth redirect")
        return state
    if state == AUTH_CAPTCHA_REQUIRED:
        return state

    marker = "data-auto-login"
    if not await _mark_exact_element(tab, "a,button", ["Login"], marker):
        print("  ❌ Could not find top.gg Login button")
        return AUTH_INVALID
    if not await _click_marked(tab, marker):
        print("  ❌ Could not click top.gg Login button")
        return AUTH_INVALID
    if not await wait_for_domain(tab, "discord.com", TIMEOUT_OAUTH_SEC):
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return AUTH_CAPTCHA_REQUIRED
        print("  ❌ Discord OAuth page did not open")
        return AUTH_INVALID
    if "/oauth2/authorize" not in urlparse(await current_url(tab)).path:
        print("  ❌ Unexpected Discord redirect")
        return AUTH_INVALID
    oauth_state = await _handle_discord_oauth(tab)
    if oauth_state == AUTH_CAPTCHA_REQUIRED:
        return oauth_state
    if oauth_state != AUTHENTICATED:
        print("  ❌ Could not authorize top.gg")
        return AUTH_INVALID
    if not await wait_for_domain(tab, "top.gg", TIMEOUT_OAUTH_SEC):
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return AUTH_CAPTCHA_REQUIRED
        print("  ❌ OAuth redirect failed")
        return AUTH_INVALID
    await asyncio.sleep(3)
    state = await topgg_auth_state(tab)
    print("  ✅ Logged into top.gg" if state == AUTHENTICATED else "  ❌ top.gg session not established")
    return state


async def is_turnstile_present(tab: Any) -> bool:
    return bool(await evaluate(tab, """(() => {
        const body = document.body ? document.body.innerText.toLowerCase() : '';
        if (body.includes('verify you are human') ||
            body.includes('please solve the captcha to continue') ||
            body.includes('complete the captcha') ||
            body.includes('let us know you are human')) return true;
        if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return true;
        if (document.querySelector('iframe[src*="hcaptcha.com"]')) return true;
        if (document.querySelector('iframe[src*="recaptcha"]')) return true;
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        return Boolean(document.querySelector('.cf-turnstile, .h-captcha, .g-recaptcha'));
    })()"""))


async def is_turnstile_solved(tab: Any) -> bool:
    return bool(await evaluate(tab, """(() => {
        const fields = document.querySelectorAll([
            'input[name="cf-turnstile-response"]',
            'textarea[name="cf-turnstile-response"]',
            'input[name="cf_challenge_response"]',
            'input[name="g-recaptcha-response"]',
            'textarea[name="g-recaptcha-response"]'
        ].join(','));
        if ([...fields].some(field => field.value && field.value.length > 10)) return true;
        const widget = document.querySelector('.cf-turnstile');
        return Boolean(widget && widget.dataset.response && widget.dataset.response.length > 10);
    })()"""))


async def solve_turnstile(tab: Any) -> bool:
    await dismiss_privacy_overlay(tab)
    if await is_turnstile_solved(tab):
        return True
    if not await is_turnstile_present(tab):
        return True
    print("  → Turnstile detected, clicking verification checkbox...")
    try:
        await tab.verify_cf()
        print("  → Turnstile checkbox click dispatched")
    except Exception as exc:
        dbg(f"verify_cf failed: {type(exc).__name__}")
        print(f"  ⚠️  Turnstile checkbox click failed ({type(exc).__name__})")
        return False
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    while asyncio.get_running_loop().time() < deadline:
        if await is_turnstile_solved(tab):
            print("  ✅ Turnstile response received")
            return True
        if not await is_turnstile_present(tab):
            print("  ✅ Turnstile security page cleared")
            return True
        await asyncio.sleep(2)
    print("  ⚠️  Turnstile remained active after checkbox click")
    return False


async def captcha_result(
    tab: Any,
    bot_id: str,
    detail: str,
    account_id: str = "unknown",
) -> dict:
    print(f"  🔒 Interactive CAPTCHA required for {bot_id}")
    result = {"bot_id": bot_id, "status": "captcha_required", "detail": detail}
    path = await browser_screenshot(
        tab,
        f"screenshots/vote_{account_id}_{bot_id}_captcha.png",
        required=True,
    )
    if path:
        result["screenshot_path"] = path
    return result


async def wait_for_ad(tab: Any, bot_id: str) -> dict | None:
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    while asyncio.get_running_loop().time() < deadline:
        text = (await body_text(tab)).lower()
        if "you will be able to vote after this ad" not in text:
            return None
        print("  → Ad playing, waiting for completion...")
        await asyncio.sleep(3)
    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_ad_timeout.png")
    if path:
        await notify_error_screenshot(bot_id, path, "Ad countdown timeout")
    return {"bot_id": bot_id, "status": "error", "detail": "Ad countdown timeout"}


async def mark_vote_button(tab: Any) -> dict:
    return dict(await evaluate(tab, """(() => {
        document.querySelectorAll('[data-auto-vote]').forEach(el => el.removeAttribute('data-auto-vote'));
        const button = [...document.querySelectorAll('button')]
            .find(el => (el.textContent || '').trim() === 'Vote');
        if (!button) return {found: false, disabled: true};
        button.setAttribute('data-auto-vote', '1');
        return {
            found: true,
            disabled: Boolean(button.disabled || button.getAttribute('aria-disabled') === 'true')
        };
    })()""") or {})


async def vote_for_bot(tab: Any, bot_id: str, account_id: str = "unknown") -> dict:
    print(f"  → Voting for bot {bot_id}...")
    await tab.get(f"https://top.gg/bot/{bot_id}/vote")
    await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        dbg("top.gg session not applied yet; reloading once")
        await tab.reload()
        await asyncio.sleep(3)
        await settle_privacy_overlay(tab)
        text = (await body_text(tab)).lower()

    if "could not be found" in text or "404" in str(await evaluate(tab, "document.title")):
        return {"bot_id": bot_id, "status": "error", "detail": "Vote page 404"}
    if "must be logged in" in text or "login to vote" in text:
        return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
    if any(marker in text for marker in ("vote again in", "already voted", "come back", "cooldown")):
        print(f"  ⏳ Already voted for {bot_id} (cooldown)")
        return cooldown_result(bot_id, text)

    if await is_turnstile_present(tab) and not await solve_turnstile(tab):
        return await captcha_result(
            tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
        )

    ad_error = await wait_for_ad(tab, bot_id)
    if ad_error:
        return ad_error

    if await is_turnstile_present(tab) and not await solve_turnstile(tab):
        return await captcha_result(
            tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
        )

    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    state = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await mark_vote_button(tab)
        if state.get("found") and not state.get("disabled"):
            break
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await captcha_result(
                tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
            )
        await asyncio.sleep(2)
    else:
        path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_no_btn.png")
        if path:
            await notify_error_screenshot(bot_id, path, "Vote button unavailable")
        detail = "Vote button disabled" if state.get("found") else "Vote button not found"
        return {"bot_id": bot_id, "status": "error", "detail": detail}

    print("  → Clicking Vote...")
    if not await _click_marked(tab, "data-auto-vote"):
        return {"bot_id": bot_id, "status": "error", "detail": "Vote button click failed"}
    await asyncio.sleep(5)

    text = (await body_text(tab)).lower()
    if "thanks for voting" in text:
        print(f"  ✅ Successfully voted for {bot_id}")
        return successful_vote_result(bot_id)
    if await is_turnstile_present(tab):
        if not await solve_turnstile(tab):
            return await captcha_result(
                tab,
                bot_id,
                "CAPTCHA still required after solver attempt following Vote click",
                account_id,
            )
        await asyncio.sleep(3)
        text = (await body_text(tab)).lower()
        if "thanks for voting" in text:
            print(f"  ✅ Successfully voted for {bot_id}")
            return successful_vote_result(bot_id)

    await tab.reload()
    await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    text = (await body_text(tab)).lower()
    if any(marker in text for marker in (
        "you have already voted", "already voted", "vote again in",
        "can vote again", "thanks for voting", "thank you",
    )):
        print(f"  ✅ Successfully voted for {bot_id}")
        return successful_vote_result(bot_id)
    if await is_turnstile_present(tab):
        if not await solve_turnstile(tab):
            return await captcha_result(
                tab,
                bot_id,
                "CAPTCHA still required after solver attempt during vote verification",
                account_id,
            )
        await asyncio.sleep(3)
        text = (await body_text(tab)).lower()
        if any(marker in text for marker in (
            "you have already voted", "already voted", "vote again in",
            "can vote again", "thanks for voting", "thank you",
        )):
            print(f"  ✅ Successfully voted for {bot_id}")
            return successful_vote_result(bot_id)

    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_uncertain.png")
    if path:
        await notify_error_screenshot(bot_id, path, "Vote result unclear")
    return {"bot_id": bot_id, "status": "uncertain", "detail": "Clicked, result unclear"}


def normalize_diagnostic(value: bytes | str) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return " ".join(text.split())


def redact_diagnostic(value: str, limit: int = DIAGNOSTIC_DETAIL_LIMIT) -> str:
    detail = normalize_diagnostic(value)
    for secret in SENSITIVE_VALUES:
        if secret:
            detail = detail.replace(secret, "***")
    return detail[:limit] or "no detail"


async def read_process_stream_excerpt(stream: Any) -> str:
    if stream is None:
        return ""
    try:
        chunk = await asyncio.wait_for(stream.read(DIAGNOSTIC_DETAIL_LIMIT), timeout=0.5)
    except (TimeoutError, asyncio.TimeoutError, OSError, ValueError):
        return ""
    return redact_diagnostic(chunk)


async def chrome_process_diagnostics(process: Any) -> str:
    if process is None:
        return ""
    parts = []
    returncode = getattr(process, "returncode", None)
    if returncode is not None:
        parts.append(f"exit={returncode}")
    stderr = await read_process_stream_excerpt(getattr(process, "stderr", None))
    stdout = await read_process_stream_excerpt(getattr(process, "stdout", None))
    if stderr and stderr != "no detail":
        parts.append(f"stderr={stderr}")
    if stdout and stdout != "no detail":
        parts.append(f"stdout={stdout}")
    return redact_diagnostic("; ".join(parts)) if parts else ""


async def close_browser(browser: Any) -> None:
    if browser is None:
        return
    process = getattr(browser, "_process", None)
    with suppress(Exception):
        await browser.aclose()
    with suppress(Exception):
        browser.stop()
    if process is not None and getattr(process, "returncode", None) is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.wait()
            raise BrowserCleanupError("Chrome process did not terminate cleanly") from exc
    profile_path = getattr(browser, "_security_profile_path", None)
    if isinstance(profile_path, (str, os.PathLike)):
        try:
            shutil.rmtree(profile_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise BrowserCleanupError("Sensitive browser profile could not be deleted") from exc





async def start_browser() -> Any:
    last_error = None
    last_error_detail = "no detail"
    scrub_browser_environment()
    for attempt in range(1, BROWSER_START_RETRIES + 1):
        profile_path = tempfile.mkdtemp(prefix="auto-vote-topgg-")
        config = uc.Config(
            user_data_dir=profile_path,
            headless=False,
            sandbox=True,
            browser_executable_path=os.environ.get("CHROME_BIN") or None,
            browser_args=[
                "--window-size=1280,720",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        browser = uc.Browser(config)
        browser._security_profile_path = profile_path
        try:
            await browser.start()
            await browser.get("about:blank")
            return browser
        except Exception as exc:
            last_error = exc
            process = getattr(browser, "_process", None)
            diagnostic = await chrome_process_diagnostics(process)
            await close_browser(browser)
            last_error_detail = f"{type(exc).__name__}: {safe_exception_detail(exc)}"
            if diagnostic:
                last_error_detail = f"{last_error_detail}; chrome {diagnostic}"
            dbg(f"Browser startup {attempt}/{BROWSER_START_RETRIES} failed: {type(exc).__name__}")
            if attempt < BROWSER_START_RETRIES:
                await asyncio.sleep(BROWSER_START_RETRY_SEC)
    raise BrowserStartupError(last_error_detail) from last_error


async def _run_account(
    token: str,
    bot_ids: list[str],
    account_id: str,
    account_cookies: list[dict] | None = None,
    *,
    capture_auth_failure: bool = False,
) -> list[dict]:
    browser = await start_browser()
    tab = next(iter(browser))
    results = []
    try:
        auth_state = AUTH_INVALID
        if account_cookies:
            auth_state = await login_with_cookies(tab, account_cookies, bot_ids)
            if auth_state == AUTH_INVALID:
                print("  → Cookie auth failed, falling back to Discord OAuth...")
                await browser.cookies.clear()
        if auth_state == AUTH_INVALID:
            auth_state = await discord_oauth_login(tab, token, bot_ids)
        if auth_state == AUTH_CAPTCHA_REQUIRED:
            result = {
                "bot_id": "all",
                "status": "captcha_required",
                "detail": "CAPTCHA blocked authentication",
                "account_id": account_id,
            }
            path = await browser_screenshot(
                tab,
                f"screenshots/auth_{account_id}_captcha.png",
                required=True,
            )
            if path:
                result["screenshot_path"] = path
            return [result]
        if auth_state != AUTHENTICATED:
            result = {
                "bot_id": "all",
                "status": "auth_failed",
                "detail": "Top.gg authentication failed",
                "account_id": account_id,
            }
            if capture_auth_failure:
                path = await browser_screenshot(
                    tab,
                    f"screenshots/auth_{account_id}_failed.png",
                    required=True,
                )
                if path:
                    result["screenshot_path"] = path
            return [result]

        for position, bot_id in enumerate(bot_ids):
            result = await vote_for_bot(tab, bot_id, account_id)
            result["account_id"] = account_id
            if is_captcha_related_result(result) and not result.get("screenshot_path"):
                path = await browser_screenshot(
                    tab,
                    f"screenshots/vote_{account_id}_{bot_id}_captcha.png",
                    required=True,
                )
                if path:
                    result["screenshot_path"] = path
            results.append(result)
            if position < len(bot_ids) - 1:
                await asyncio.sleep(DELAY_BETWEEN_BOTS_SEC)
        return results
    finally:
        await close_browser(browser)
        await asyncio.sleep(1)


async def process_account(
    token: str,
    bot_ids: list[str],
    index: int,
    total: int,
    account_cookies: list[dict] | None = None,
) -> list[dict]:
    prefix = f"[{index}/{total}]"
    account_id = account_fingerprint(token)
    pending = list(bot_ids)
    results_by_bot: dict[str, dict] = {}
    last_account_error: dict | None = None
    print(f"\n{'─' * 45}")
    print(f"{prefix} Processing account...")

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"{prefix} ↺ Retry {attempt}/{MAX_RETRIES} (waiting {RETRY_DELAY_SEC}s)...")
            await asyncio.sleep(RETRY_DELAY_SEC)
        try:
            attempt_results = await _run_account(
                token,
                pending,
                account_id,
                account_cookies,
                capture_auth_failure=attempt == MAX_RETRIES,
            )
        except Exception as exc:
            if isinstance(exc, BrowserStartupError):
                detail = f"Browser startup failed: {safe_exception_detail(exc)}"
            elif isinstance(exc, TypeError):
                detail = f"TypeError: {safe_exception_detail(exc)}"
            else:
                detail = f"{type(exc).__name__}: transient browser failure"
            last_account_error = {
                "bot_id": "all", "status": "error",
                "detail": detail, "account_id": account_id,
            }
            dbg(f"Account attempt failed: {type(exc).__name__}")
            print(f"{prefix} ❌ Attempt {attempt} failed: {detail}")
            if isinstance(exc, BrowserStartupError):
                break
            continue

        if attempt_results and attempt_results[0].get("bot_id") == "all":
            last_account_error = attempt_results[0]
            if not is_retryable_result(last_account_error):
                print(f"{prefix} 🔒 Authentication requires manual CAPTCHA")
                return attempt_results
            print(f"{prefix} ❌ Authentication attempt {attempt} failed")
            continue
        for result in attempt_results:
            results_by_bot[str(result["bot_id"])] = result
        pending = retryable_bot_ids(attempt_results)
        if not pending:
            return [results_by_bot[bot_id] for bot_id in bot_ids]

    print(f"{prefix} ❌ All {MAX_RETRIES} attempts exhausted")
    if results_by_bot:
        return [results_by_bot[bot_id] for bot_id in bot_ids if bot_id in results_by_bot]
    return [last_account_error or {
        "bot_id": "all", "status": "error",
        "detail": f"Failed after {MAX_RETRIES} retries", "account_id": account_id,
    }]


def build_notification(all_results: list[list[dict]], now: str) -> str:
    lines = ["🗳️ <b>Top.gg Auto Vote Report</b>", f"⏱️ {now}", ""]
    for account_results in all_results:
        if not account_results:
            continue
        account_id = escape(str(account_results[0].get("account_id", "?")))
        lines.append(f"👤 <b>Account {account_id}</b>")
        for result in account_results:
            bot_id = escape(str(result.get("bot_id", "?")))
            status = result.get("status", "?")
            detail = escape(str(result.get("detail", "")))
            icon = {
                "success": "✅", "cooldown": "⏳", "uncertain": "⚠️",
                "captcha_required": "🔒",
            }.get(status, "❌")
            lines.append(f"  {icon} {bot_id}: {detail}")
        lines.append("")
    return "\n".join(lines).strip()


def has_business_failure(all_results: list[list[dict]]) -> bool:
    return not all_results or any(
        not account_results
        or any(result.get("status") not in COMPLETED_STATUSES for result in account_results)
        for account_results in all_results
    )


async def main() -> int:
    global TG_BOT_TOKEN, TG_CHAT_ID, SENSITIVE_VALUES
    tokens_raw = consume_secret("TOKENS")
    cookies_raw = consume_secret("TOPGG_COOKIES_JSON")
    TG_BOT_TOKEN = consume_secret("TG_BOT_TOKEN").strip()
    TG_CHAT_ID = consume_secret("TG_CHAT_ID").strip()
    tokens = load_tokens(tokens_raw)
    SENSITIVE_VALUES = [
        tokens_raw,
        cookies_raw,
        TG_BOT_TOKEN,
        TG_CHAT_ID,
        *tokens,
    ]
    scrub_browser_environment()
    if not tokens:
        print("❌ No tokens found.\n   Set TOKENS secret (one Discord user token per line).")
        return 1

    bot_ids = load_bot_ids()
    all_cookies = load_topgg_cookies(len(tokens), cookies_raw)
    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    total = len(tokens)
    print("🚀 auto-vote-dcbot starting")
    print(f"   Tokens  : {total}")
    print(f"   Cookies : {sum(bool(cookies) for cookies in all_cookies)}/{total} account(s)")
    print(f"   Bots    : {len(bot_ids)}")
    print(f"   Time    : {now}")

    all_results = []
    for index, token in enumerate(tokens, 1):
        cookies = all_cookies[index - 1] if index <= len(all_cookies) else []
        results = await process_account(token, bot_ids, index, total, cookies)
        all_results.append(results)
        if index < total:
            await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS_SEC)

    print(f"\n{'=' * 45}")
    print(f"📊 Done — {total} account(s) processed")
    retry_at = write_next_vote_state(all_results)
    if retry_at is not None:
        print(f"⏰ Next scheduled vote: {format_retry_at(retry_at)}")
    if write_browser_startup_retry_state(all_results):
        print("↺ Browser startup fresh-run retry requested")
    report = build_notification(all_results, now)
    send_notification(report)
    await send_captcha_screenshots(all_results)
    await send_auth_failure_screenshots(all_results)
    return 1 if has_business_failure(all_results) else 0


if __name__ == "__main__":
    raise SystemExit(uc.loop().run_until_complete(main()))
