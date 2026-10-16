"""Shared browser lifecycle, native controls, notifications and scheduling for AniGame."""


import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
import nodriver as uc
from nodriver.core.connection import ProtocolException
import requests
import cloudflare_click
import ui_click
import browser_environment
import flaresolverr_browser
from page_signals import CHALLENGE_JS
from vote_controls import VOTE_CONTROL_JS


def is_expired_interception(exc: Exception) -> bool:
    """Chrome can discard a paused request when navigation or an abort cancels it."""
    return (isinstance(exc, ProtocolException) and exc.code == -32602
            and exc.message in {"Invalid InterceptionId.", "Invalid InterceptionId"})


DISCORD_LOGIN_URL = "https://discord.com/login"
BROWSER_COMMAND_TIMEOUT_SEC = 8
TIMEOUT_VOTE_SEC = 30
DOCUMENT_READY_JS = "Boolean(document.body && document.body.hasChildNodes()) && ['interactive', 'complete'].includes(document.readyState)"
COMPLETED_STATUSES = frozenset({"success", "cooldown"})
TRANSIENT_STATUSES = frozenset({"error", "auth_failed", "uncertain"})
BROWSER_START_RETRIES = 7
BROWSER_START_RETRY_SEC = 2
BROWSER_START_CALL_TIMEOUT_SEC = 20
BROWSER_INITIAL_PAGE_TIMEOUT_SEC = 10
BROWSER_CLOSE_TIMEOUT_SEC = 5
BROWSER_LATE_ATTACH_TIMEOUT_SEC = 12
BROWSER_LATE_ATTACH_POLL_SEC = 0.5
BROWSER_LATE_ATTACH_PROBE_TIMEOUT_SEC = 2
BROWSER_LATE_ATTACH_STEP_TIMEOUT_SEC = 4
TELEGRAM_MESSAGE_LIMIT = 3500
DIAGNOSTIC_DETAIL_LIMIT = 600
BROWSER_RETRY_REASON = "browser_startup_failed"
PROTECTION_RETRY_REASON = "protection_blocked"
BROWSER_STARTUP_DETAIL_PREFIX = "Browser startup failed:"
SUCCESS_NEXT_VOTE_DELAY_SEC = 12 * 60 * 60 + 30
TRANSIENT_RETRY_DELAY_SEC = 20 * 60
BLOCKED_RETRY_DELAY_SEC = 30 * 60
CAPTCHA_RETRY_DELAY_SEC = 60 * 60


class BrowserStartupError(RuntimeError):
    """Credential-free nodriver startup failure safe for workflow logs."""


class BrowserCleanupError(RuntimeError):
    """Browser process or sensitive profile cleanup failed."""


TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
SENSITIVE_VALUES: list[str] = []
PRIVACY_DISMISS_REPORTED = False
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
    for name in ("TOKENS", "TOPGG_COOKIES_JSON", "ANIGAME_COOKIES_JSON", "TG_BOT_TOKEN", "TG_CHAT_ID"):
        os.environ.pop(name, None)
        os.environ.pop(f"{name}_FILE", None)
    # Preserve a valid session bus (for example from dbus-run-session) but
    # remove malformed runner values that Chrome cannot parse.
    dbus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    if dbus_address and not dbus_address.startswith(("unix:", "tcp:")):
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)


async def browser_screenshot(tab: Any, path: str, *, required: bool = False) -> str | None:
    if not required and not SEND_ERROR_SCREENSHOTS:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await asyncio.wait_for(tab.save_screenshot(filename=path, format="png"), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
        return path
    except Exception as exc:
        dbg(f"Browser screenshot failed: {type(exc).__name__}")
        if required:
            print("  ⚠️  Could not capture CAPTCHA browser screenshot")
        return None


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


def load_tokens(raw: str | None = None) -> list[str]:
    if raw is None:
        raw = os.environ.get("TOKENS", "")
    raw = raw.strip()
    return [line.strip() for line in raw.splitlines() if line.strip()] if raw else []


def account_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def retry_at_for_result(result: dict, now: datetime | None = None) -> int | None:
    retry_at = result.get("retry_at")
    if isinstance(retry_at, int):
        return retry_at

    status = str(result.get("status", ""))
    current = now or datetime.now(timezone.utc)
    base = int(current.timestamp())

    if status in {"success", "cooldown"}:
        return base + SUCCESS_NEXT_VOTE_DELAY_SEC
    if status == "blocked":
        return base + BLOCKED_RETRY_DELAY_SEC
    if status == "captcha_required":
        return base + CAPTCHA_RETRY_DELAY_SEC
    if status in TRANSIENT_STATUSES or status:
        return base + TRANSIENT_RETRY_DELAY_SEC
    return None


def earliest_retry_at(
    all_results: list[list[dict]],
    now: datetime | None = None,
) -> int | None:
    retry_times = [
        retry_at
        for account_results in all_results
        for result in account_results
        if (retry_at := retry_at_for_result(result, now)) is not None
    ]
    return min(retry_times) if retry_times else None


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


def should_request_protection_retry(all_results: list[list[dict]]) -> bool:
    results = [result for account_results in all_results for result in account_results]
    # Preserve the legacy marker's meaning: a protection failure without any
    # outstanding submission. It is diagnostic only; workflow failure recovery
    # now runs independently of these markers, with no chain limit.
    if any(
        result.get("vote_submitted") is True
        and result.get("status") not in COMPLETED_STATUSES
        for result in results
    ):
        return False
    return any(result.get("status") == "blocked" for result in results)


def write_protection_retry_state(
    all_results: list[list[dict]], path_value: str | None = None
) -> bool:
    path_text = (
        path_value
        if path_value is not None
        else os.environ.get("PROTECTION_RETRY_STATE_FILE", "")
    )
    if not path_text.strip() or not should_request_protection_retry(all_results):
        return False
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"reason": PROTECTION_RETRY_REASON}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return True


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


async def evaluate(tab: Any, expression: str, *, timeout: float | None = None) -> Any:
    remote_object, exception = await asyncio.wait_for(tab.send(uc.cdp.runtime.evaluate(
        expression=expression,
        user_gesture=True,
        await_promise=True,
        return_by_value=True,
        allow_unsafe_eval_blocked_by_csp=True,
    )), timeout=BROWSER_COMMAND_TIMEOUT_SEC if timeout is None else timeout)
    if exception:
        raise RuntimeError("JavaScript evaluation failed")
    return remote_object.value if remote_object else None


async def current_url(tab: Any) -> str:
    return str(await evaluate(tab, "location.href") or "")


async def navigate_page(tab: Any, url: str) -> None:
    """Keep the CDP session that owns Network/Page observers during navigation.

    nodriver 0.50.3 Tab.get() calls attach() again after navigation; the new
    session does not own responses observed by the previous Network.enable.
    """
    result = await asyncio.wait_for(tab.send(uc.cdp.page.navigate(url)), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
    if isinstance(result, (tuple, list)) and len(result) > 2 and result[2]:
        raise RuntimeError("Page navigation failed")


async def _mark_exact_element(tab: Any, selector: str, texts: list[str], marker: str) -> bool:
    script = "(() => {" + VOTE_CONTROL_JS + f"""
        const wanted = new Set({json.dumps(texts)}.map(text => text.trim().toLowerCase()));
        document.querySelectorAll('[' + {json.dumps(marker)} + ']').forEach(
            node => node.removeAttribute({json.dumps(marker)})
        );
        const nodes = [...document.querySelectorAll({json.dumps(selector)})];
        const candidates = nodes.filter(node => wanted.has((node.textContent || '').trim().toLowerCase()) &&
            !voteControl.disabled(node) && voteControl.visible(node));
        const element = candidates.find(node => voteControl.position(node, true).ready) || candidates[0];
        if (!element) return false;
        element.setAttribute({json.dumps(marker)}, '1');
        return true;
    }})()"""
    return bool(await evaluate(tab, script))


async def _click_marked(tab: Any, marker: str, *, reacquire=None) -> bool:
    await dismiss_privacy_overlay(tab)
    kind = "oauth" if marker == "data-auto-oauth" else "login"
    receipt = await ui_click.click_control(tab, evaluate, f'[{marker}="1"]', kind=kind, reacquire=reacquire)
    # A navigation can destroy the observer. The OAuth caller still checks its
    # destination and authenticated application state; input alone is not login.
    return receipt["input_sent"] and receipt["clicked"] is not False


async def _click_exact_element(tab: Any, selector: str, texts: list[str], marker: str) -> bool:
    async def reacquire():
        return await _mark_exact_element(tab, selector, texts, marker)
    # Reacquire throughout hydration/remounts and after hover, including when
    # the first observation has not yet exposed the control.
    return await _click_marked(tab, marker, reacquire=reacquire)


async def dismiss_privacy_overlay(tab: Any) -> bool:
    script = "(() => {" + VOTE_CONTROL_JS + """
        const body = document.body ? document.body.innerText.toLowerCase() : '';
        const present = body.includes('we value your privacy') ||
            body.includes('partners store and/or access information') ||
            body.includes('personalised ads and content');
        document.querySelectorAll('[data-auto-consent]').forEach(el => el.removeAttribute('data-auto-consent'));
        if (!present) return {present: false};
        const labels = new Set(['agree', 'accept', 'accept all', 'allow all', 'i agree']);
        const controls = [...document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')];
        const direct = document.querySelector('#accept-btn');
        const candidates = direct ? [direct, ...controls.filter(el => el !== direct)] : controls;
        const eligible = candidates.filter(el => {
            if (voteControl.disabled(el) || !voteControl.visible(el)) return false;
            const text = [el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.id]
                .filter(Boolean).join(' ').trim().toLowerCase();
            return el === direct || labels.has(text) || text.includes('agree') || text.includes('accept');
        });
        const target = eligible.find(el => voteControl.position(el, true).ready) || eligible[0];
        if (target) target.setAttribute('data-auto-consent', '1');
        return {present: true, button_found: Boolean(target), reason: 'consent_button_not_found'};
    })()"""
    try:
        result = await evaluate(tab, script)
        if not isinstance(result, dict) or not result.get("present"):
            return False
        if result.get("button_found"):
            async def reacquire():
                observed = await evaluate(tab, script)
                return isinstance(observed, dict) and observed.get("present") is True and observed.get("button_found") is True
            receipt = await ui_click.click_control(tab, evaluate, '[data-auto-consent="1"]', kind="consent", reacquire=reacquire)
            if receipt["input_sent"]:
                for _ in range(4):
                    await asyncio.sleep(0.25)
                    observed = await evaluate(tab, script)
                    if isinstance(observed, dict) and observed.get("present") is False:
                        return True
            result = {"present": True, "reason": "consent_dismissal_unconfirmed"}
    except Exception as exc:
        detail = f"JavaScript check failed: {type(exc).__name__}: {safe_exception_detail(exc)}"
        dbg(f"Privacy overlay dismiss skipped: {type(exc).__name__}")
        await report_privacy_dismiss_failure(tab, detail)
        return False
    await report_privacy_dismiss_failure(tab, str(result.get("reason", "unknown dismiss failure")))
    return False


async def is_turnstile_present(tab: Any) -> bool:
    state = await evaluate(tab, "(() => {" + CHALLENGE_JS + "return challengeState.present; })()")
    if state is True:
        return True
    shadow = await asyncio.wait_for(cloudflare_click.widget_state(tab), timeout=3)
    return shadow["present"] is True and shadow["solved"] is not True


async def is_turnstile_solved(tab: Any) -> bool:
    state = await evaluate(tab, "(() => {" + CHALLENGE_JS + "return challengeState.solved; })()")
    if state is True:
        return True
    return (await asyncio.wait_for(cloudflare_click.widget_state(tab), timeout=3))["solved"] is True


async def challenge_diagnostic(tab: Any) -> dict:
    """Read fixed page signals without returning page text, URLs or tokens."""
    return await evaluate(tab, """(() => {
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        const title = (document.title || '').trim().toLowerCase();
        const visible = node => {
            if (!node || !(node.getClientRects().length || node.offsetWidth || node.offsetHeight)) {
                return false;
            }
            const style = getComputedStyle(node);
            return style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
                style.display !== 'none' && style.opacity !== '0';
        };
        const gates = [...document.querySelectorAll(
            '#challenge-running, #challenge-stage, #challenge-form'
        )];
        const widgets = [...document.querySelectorAll([
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="hcaptcha.com"]', 'iframe[src*="recaptcha"]',
            '.cf-turnstile', '.h-captcha', '.g-recaptcha'
        ].join(','))];
        const fields = [...document.querySelectorAll([
            'input[name="cf-turnstile-response"]',
            'textarea[name="cf-turnstile-response"]',
            'input[name="cf_challenge_response"]',
            'input[name="g-recaptcha-response"]',
            'textarea[name="g-recaptcha-response"]'
        ].join(','))];
        const controls = [...document.querySelectorAll('button, a, [role="button"]')];
        const voteButtons = [...document.querySelectorAll('button, [role="button"]')]
            .filter(node => ['vote', 'vote for anigame'].includes((node.textContent || '').trim().toLowerCase()));
        return {
            ready_state: ['loading', 'interactive', 'complete'].includes(document.readyState)
                ? document.readyState : 'unknown',
            title_just_moment: title.startsWith('just a moment'),
            title_attention: title.startsWith('attention required'),
            body_security: body.includes('performing security verification') ||
                body.includes('needs to review the security of your connection'),
            body_human: body.includes('verify you are human') ||
                body.includes('please solve the captcha to continue') ||
                body.includes('complete the captcha'),
            gate_present: gates.length > 0,
            gate_visible: gates.some(visible),
            widget_present: widgets.length > 0,
            response_present: fields.some(field => Boolean(field.value && field.value.length > 10)) ||
                widgets.some(node => Boolean(node.dataset && node.dataset.response &&
                    node.dataset.response.length > 10)),
            vote_visible: voteButtons.some(visible),
            vote_enabled: voteButtons.some(node => visible(node) && !node.disabled &&
                !node.hasAttribute('disabled') && node.getAttribute('aria-disabled') !== 'true'),
            login_visible: controls.some(node => visible(node) &&
                ['login', 'log in', 'sign in', 'sign in with discord', 'login with discord']
                    .includes((node.textContent || '').trim().toLowerCase()))
        };
    })()""")


async def log_challenge_diagnostic(tab: Any, phase: str) -> None:
    """Best-effort, bounded diagnostics safe for public Actions logs."""
    safe_phase = phase if phase in ("detected", "click_error", "timeout") else "unknown"
    try:
        result = await asyncio.wait_for(challenge_diagnostic(tab), timeout=2)
        if not isinstance(result, dict):
            raise ValueError("invalid diagnostic")
        ready = result.get("ready_state")
        diagnostic = {
            "phase": safe_phase,
            "ready_state": ready if isinstance(ready, str) and ready in (
                "loading", "interactive", "complete"
            ) else "unknown",
        }
        for key in (
            "title_just_moment", "title_attention", "body_security", "body_human",
            "gate_present", "gate_visible", "widget_present", "response_present",
            "vote_visible", "vote_enabled", "login_visible",
        ):
            value = result.get(key)
            diagnostic[key] = value if isinstance(value, bool) else None
        print("  Challenge diagnostic: " + json.dumps(diagnostic, sort_keys=True))
    except Exception:
        # A disconnected browser must not change the original outcome; never
        # print exception details or an unexpected browser response here.
        print(f"  Challenge diagnostic unavailable (phase={safe_phase})")


async def _click_cloudflare_checkbox(tab: Any) -> str:
    async def cleared() -> bool:
        await dismiss_privacy_overlay(tab)
        return await stable_challenge_clearance(tab)

    return await cloudflare_click.click_cloudflare_checkbox(tab, evaluate, cleared)


async def document_ready(tab: Any) -> bool:
    try:
        return await asyncio.wait_for(evaluate(tab, DOCUMENT_READY_JS), timeout=2) is True
    except Exception:
        return False


async def stable_challenge_clearance(tab: Any) -> bool:
    async def clear_signal():
        return await document_ready(tab) and (
            await is_turnstile_solved(tab) or not await is_turnstile_present(tab)
        )
    if not await clear_signal(): return False
    await asyncio.sleep(0.5)
    return await clear_signal()


async def solve_turnstile(tab: Any) -> bool:
    await dismiss_privacy_overlay(tab)
    if await is_turnstile_solved(tab) or not await is_turnstile_present(tab):
        if await stable_challenge_clearance(tab):
            return True
    await log_challenge_diagnostic(tab, "detected")
    await browser_environment.log_facts(tab, evaluate, "challenge_detected")
    print("  → Challenge detected; waiting for a verified Cloudflare checkbox target...")
    try:
        click = await _click_cloudflare_checkbox(tab)
    except Exception as exc:
        dbg(f"Cloudflare mouse interaction failed: {type(exc).__name__}")
        print(f"  ⚠️  Turnstile checkbox click failed ({type(exc).__name__})")
        await log_challenge_diagnostic(tab, "click_error")
        return False
    if click == "cleared":
        print("  → Challenge cleared while waiting; no checkbox click needed")
        return True
    if click != "sent":
        await log_challenge_diagnostic(tab, "click_error")
        return False
    print("  → Cloudflare mouse input sent; waiting for verification acceptance")
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    async def accepted():
        if await is_turnstile_solved(tab) or not await is_turnstile_present(tab):
            return await stable_challenge_clearance(tab)
        return False
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await asyncio.wait_for(accepted(), timeout=max(0.001, deadline - asyncio.get_running_loop().time())):
                print("  ✅ Challenge cleared on a stable document; application access still needs verification")
                return True
        except TimeoutError:
            break
        await asyncio.sleep(min(2, max(0, deadline - asyncio.get_running_loop().time())))
    await log_challenge_diagnostic(tab, "timeout")
    await browser_environment.log_facts(tab, evaluate, "challenge_timeout")
    print("  ⚠️  Challenge signals remained active after verification attempt")
    return False


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
        return "process=unavailable"
    parts = []
    pid = getattr(process, "pid", None)
    if isinstance(pid, int):
        parts.append(f"pid={pid}")
    returncode = getattr(process, "returncode", None)
    if returncode is None:
        parts.append("state=running")
    else:
        parts.append(f"exit={returncode}")
    stderr = await read_process_stream_excerpt(getattr(process, "stderr", None))
    stdout = await read_process_stream_excerpt(getattr(process, "stdout", None))
    if stderr and stderr != "no detail":
        parts.append(f"stderr={stderr}")
    if stdout and stdout != "no detail":
        parts.append(f"stdout={stdout}")
    return redact_diagnostic("; ".join(parts))


def browser_startup_environment_diagnostics() -> str:
    """Return bounded, credential-free runner facts useful for Chrome startup failures."""
    parts = []

    getuid = getattr(os, "geteuid", None)
    if callable(getuid):
        with suppress(Exception):
            parts.append(f"uid={getuid()}")

    display = os.environ.get("DISPLAY", "").strip()
    parts.append(f"display={display or 'unset'}")
    if display.startswith(":"):
        display_number = display[1:].split(".", 1)[0]
        if display_number.isdigit():
            x11_socket = Path("/tmp/.X11-unix") / f"X{display_number}"
            parts.append(f"x11_socket={'present' if x11_socket.exists() else 'missing'}")

    chrome_bin = os.environ.get("CHROME_BIN", "").strip()
    if chrome_bin:
        parts.append(f"chrome={Path(chrome_bin).name}")
        try:
            completed = subprocess.run(
                [chrome_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            version = (completed.stdout or completed.stderr or "").strip()
            if version:
                parts.append(f"chrome_version={redact_diagnostic(version, 120)}")
            parts.append(f"chrome_version_exit={completed.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            parts.append(f"chrome_version_error={type(exc).__name__}")
    else:
        parts.append("chrome=unset")

    for label, path in (("shm", "/dev/shm"), ("tmp", tempfile.gettempdir())):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            parts.append(f"{label}=unavailable")
            continue
        parts.append(
            f"{label}_free_mb={usage.free // (1024 * 1024)}"
            f"/{usage.total // (1024 * 1024)}"
        )

    return redact_diagnostic("; ".join(parts))


async def recover_slow_browser_start(browser: Any) -> bool:
    """Attach to Chrome when nodriver's initial DevTools polling window expires too early."""
    process = getattr(browser, "_process", None)
    http = getattr(browser, "_http", None)
    if (
        process is None
        or getattr(process, "returncode", None) is not None
        or http is None
    ):
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + BROWSER_LATE_ATTACH_TIMEOUT_SEC
    while loop.time() < deadline:
        try:
            info = await asyncio.wait_for(
                http.get("version"),
                timeout=BROWSER_LATE_ATTACH_PROBE_TIMEOUT_SEC,
            )
        except Exception as exc:
            dbg(f"Late Chrome DevTools probe not ready: {type(exc).__name__}")
            await asyncio.sleep(BROWSER_LATE_ATTACH_POLL_SEC)
            continue

        websocket_url = (
            info.get("webSocketDebuggerUrl")
            if isinstance(info, dict)
            else getattr(info, "webSocketDebuggerUrl", None)
        )
        if not websocket_url:
            await asyncio.sleep(BROWSER_LATE_ATTACH_POLL_SEC)
            continue

        try:
            browser.info = info
            browser.websocket_url = str(websocket_url)
            await asyncio.wait_for(
                browser.attach(),
                timeout=BROWSER_LATE_ATTACH_STEP_TIMEOUT_SEC,
            )
            await asyncio.wait_for(
                browser.update_targets(),
                timeout=BROWSER_LATE_ATTACH_STEP_TIMEOUT_SEC,
            )
            await asyncio.wait_for(
                browser.get("about:blank"),
                timeout=BROWSER_LATE_ATTACH_STEP_TIMEOUT_SEC,
            )
        except Exception as exc:
            dbg(f"Late Chrome attach failed: {type(exc).__name__}")
            return False

        print("  ✅ Recovered slowly-starting Chrome without restarting it")
        return True
    return False


async def close_browser(browser: Any) -> None:
    if browser is None:
        return
    process = getattr(browser, "_process", None)
    with suppress(Exception):
        await asyncio.wait_for(
            browser.aclose(),
            timeout=BROWSER_CLOSE_TIMEOUT_SEC,
        )
    with suppress(Exception):
        browser.stop()
    forced_shutdown = False
    if process is not None and getattr(process, "returncode", None) is None:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=BROWSER_CLOSE_TIMEOUT_SEC,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            forced_shutdown = True
            with suppress(Exception):
                process.kill()
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=BROWSER_CLOSE_TIMEOUT_SEC,
                )
            except (TimeoutError, asyncio.TimeoutError):
                raise BrowserCleanupError("Chrome process did not terminate cleanly") from exc
    profile_path = getattr(browser, "_security_profile_path", None)
    if isinstance(profile_path, (str, os.PathLike)):
        try:
            shutil.rmtree(profile_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise BrowserCleanupError("Sensitive browser profile could not be deleted") from exc
    if forced_shutdown:
        raise BrowserCleanupError("Chrome required forced termination; profile deleted")


async def close_browser_safely(browser: Any, context: str) -> bool:
    """Best-effort cleanup that never turns a completed vote into a duplicate retry."""
    try:
        await close_browser(browser)
        return True
    except BrowserCleanupError as exc:
        print(
            f"  ⚠️  Browser cleanup warning after {context}: "
            f"{safe_exception_detail(exc)}"
        )
        return False
    except Exception as exc:
        print(f"  ⚠️  Browser cleanup warning after {context}: {type(exc).__name__}")
        return False


async def start_browser(*, initial_url: str | None = None) -> Any:
    last_error = None
    last_error_detail = "no detail"
    scrub_browser_environment()
    if os.environ.get("FLARESOLVERR_URL", "").strip():
        try:
            browser = await flaresolverr_browser.start(initial_url)
            await browser_environment.log_facts(next(iter(browser)), evaluate, "startup")
            return browser
        except Exception as exc:
            print(f"  ⚠️  Free FlareSolverr unavailable ({type(exc).__name__}); starting ordinary Chrome")
    for attempt in range(1, BROWSER_START_RETRIES + 1):
        profile_path = tempfile.mkdtemp(prefix="anigame-vote-")
        config = browser_environment.ChromeConfig(
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
            await asyncio.wait_for(
                browser.start(),
                timeout=BROWSER_START_CALL_TIMEOUT_SEC,
            )
            await asyncio.wait_for(
                browser.get("about:blank"),
                timeout=BROWSER_INITIAL_PAGE_TIMEOUT_SEC,
            )
            await browser_environment.log_facts(next(iter(browser)), evaluate, "startup")
            return browser
        except Exception as exc:
            last_error = exc
            process = getattr(browser, "_process", None)
            if (
                process is not None
                and getattr(process, "returncode", None) is None
                and await recover_slow_browser_start(browser)
            ):
                return browser

            process_diagnostic = await chrome_process_diagnostics(process)
            runner_diagnostic = browser_startup_environment_diagnostics()
            await close_browser_safely(browser, "failed startup")
            last_error_detail = (
                f"{type(exc).__name__}: {safe_exception_detail(exc)}; "
                f"chrome {process_diagnostic}; runner {runner_diagnostic}"
            )
            last_error_detail = redact_diagnostic(last_error_detail)
            print(
                f"  ⚠️  Browser startup {attempt}/{BROWSER_START_RETRIES} failed; "
                f"{last_error_detail}"
            )
            dbg(f"Browser startup {attempt}/{BROWSER_START_RETRIES} failed: {type(exc).__name__}")
            if attempt < BROWSER_START_RETRIES:
                await asyncio.sleep(BROWSER_START_RETRY_SEC)
    raise BrowserStartupError(last_error_detail) from last_error


def has_business_failure(all_results: list[list[dict]]) -> bool:
    return not all_results or any(
        not account_results
        or any(result.get("status") not in COMPLETED_STATUSES for result in account_results)
        for account_results in all_results
    )
