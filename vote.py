#!/usr/bin/env python3
"""Automated top.gg voting via nodriver, cookie-first auth, and Discord OAuth fallback."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
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
DISCORD_LOGIN_URL = "https://discord.com/login"
TIMEOUT_OAUTH_SEC = 25
TIMEOUT_VOTE_SEC = 30
SESSION_PROBE_TIMEOUT_SEC = 12
AUTH_PAGE_SETTLE_POLLS = 4
AUTH_PAGE_SETTLE_DELAY_SEC = 2
DELAY_BETWEEN_BOTS_SEC = 3
DELAY_BETWEEN_ACCOUNTS_SEC = 5
MAX_RETRIES = 3
MAX_BLOCKED_ATTEMPTS = 2
MAX_TURNSTILE_CYCLES_PER_PHASE = 3
RETRY_DELAY_SEC = 10
FINAL_STATUSES = frozenset({"success", "cooldown", "captcha_required", "blocked"})
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
AUTHENTICATED = "authenticated"
AUTH_INVALID = "invalid"
AUTH_BLOCKED = "blocked"
AUTH_CAPTCHA_REQUIRED = "captcha_required"
TELEGRAM_MESSAGE_LIMIT = 3500
DIAGNOSTIC_DETAIL_LIMIT = 600
BROWSER_RETRY_REASON = "browser_startup_failed"
PROTECTION_RETRY_REASON = "protection_blocked"
BROWSER_STARTUP_DETAIL_PREFIX = "Browser startup failed:"
COOLDOWN_SAFETY_BUFFER_SEC = 5 * 60
SUCCESS_NEXT_VOTE_DELAY_SEC = 12 * 60 * 60 + 30
TRANSIENT_RETRY_DELAY_SEC = 20 * 60
BLOCKED_RETRY_DELAY_SEC = 30 * 60
CAPTCHA_RETRY_DELAY_SEC = 60 * 60
MIN_COOLDOWN_SEC = 60
MAX_COOLDOWN_SEC = 24 * 60 * 60
COOLDOWN_UNITS_SEC = {
    "second": 1,
    "minute": 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
}
POST_VOTE_STRONG_MARKERS = (
    "thanks for voting",
    "you have already voted",
)
POST_VOTE_VERIFY_ATTEMPTS = 2
POST_VOTE_VERIFY_DELAY_SEC = 3
POST_VOTE_CHALLENGE_SETTLE_POLLS = 2
POST_VOTE_CHALLENGE_SETTLE_DELAY_SEC = 2
COOLDOWN_PATTERN = re.compile(
    r"(?:you\s+)?can\s+vote\s+again\s+in\s+"
    r"(?:about\s+|approximately\s+)?"
    r"(?P<duration>"
    r"(?:(?:\d+(?:\.\d+)?|a|an|one)\s*"
    r"(?:seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d)"
    r"\s*(?:,\s*|and\s+|\s+)?){1,4}"
    r")",
    re.IGNORECASE,
)
COOLDOWN_COMPONENT_PATTERN = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?|a|an|one)\s*"
    r"(?P<unit>seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d)\b",
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
    caption = f"❌ Vote failed for {escape(str(bot_id))}\n{escape(str(detail))}"
    try:
        if not TG_BOT_TOKEN or not TG_CHAT_ID:
            return
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
            if result.get("status") not in {"auth_failed", "blocked"}:
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
    if not raw:
        raise ValueError("BOT_IDS is required; configure at least one Discord bot ID")
    ids = [line.strip() for line in raw.splitlines() if line.strip()]
    invalid = [bot_id for bot_id in ids if not re.fullmatch(r"[0-9]{17,20}", bot_id)]
    if invalid:
        raise ValueError(f"Invalid BOT_IDS value: {invalid[0]!r}; expected a 17-20 digit Discord ID")
    return list(dict.fromkeys(ids))


def account_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def is_captcha_related_result(result: dict) -> bool:
    return (
        result.get("status") == "captcha_required"
        or "captcha" in str(result.get("detail", "")).casefold()
    )


def is_retryable_result(result: dict) -> bool:
    return (
        result.get("vote_submitted") is not True
        and result.get("status") in TRANSIENT_STATUSES
    )


def retryable_bot_ids(results: list[dict]) -> list[str]:
    return [
        str(result["bot_id"])
        for result in results
        if result.get("bot_id") not in {None, "all"} and is_retryable_result(result)
    ]


def parse_cooldown_seconds(text: str) -> int | None:
    """Parse bounded, possibly compound top.gg relative cooldown text."""
    normalized = " ".join(text.split())
    match = COOLDOWN_PATTERN.search(normalized)
    if not match:
        return None

    total = 0.0
    components = list(COOLDOWN_COMPONENT_PATTERN.finditer(match.group("duration")))
    if not components:
        return None

    for component in components:
        amount_text = component.group("amount").lower()
        amount = 1.0 if amount_text in {"a", "an", "one"} else float(amount_text)
        raw_unit = component.group("unit").lower()
        if raw_unit.startswith(("s",)):
            unit = "second"
        elif raw_unit.startswith(("m",)):
            unit = "minute"
        elif raw_unit.startswith(("h",)):
            unit = "hour"
        elif raw_unit.startswith(("d",)):
            unit = "day"
        else:
            return None
        total += amount * COOLDOWN_UNITS_SEC[unit]

    seconds = round(total)
    return seconds if MIN_COOLDOWN_SEC <= seconds <= MAX_COOLDOWN_SEC else None


def cooldown_retry_at(text: str, now: datetime | None = None) -> int | None:
    seconds = parse_cooldown_seconds(text)
    if seconds is None:
        return None
    current = now or datetime.now(timezone.utc)
    return int(current.timestamp()) + seconds + COOLDOWN_SAFETY_BUFFER_SEC


def page_indicates_cooldown(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return (
        parse_cooldown_seconds(normalized) is not None
        or "you have already voted" in normalized
    )


def cooldown_result(bot_id: str, text: str, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    retry_at = cooldown_retry_at(text, current)
    result = {"bot_id": bot_id, "status": "cooldown", "detail": "Cooldown active"}
    if retry_at is None:
        # We know a vote is unavailable, but not how much of the cooldown remains.
        # Never invent a new 12-hour window from observation time.
        retry_at = int(current.timestamp()) + TRANSIENT_RETRY_DELAY_SEC
        result["detail"] = (
            f"Cooldown active; remaining time unavailable; "
            f"recheck after {format_retry_at(retry_at)}"
        )
    else:
        result["detail"] = f"Cooldown active; retry after {format_retry_at(retry_at)}"
    result["retry_at"] = retry_at
    return result

def vote_success_evidence(text: str) -> str | None:
    """Return a strong post-vote signal; reject generic instructional text."""
    normalized = " ".join(text.casefold().split())
    for marker in POST_VOTE_STRONG_MARKERS:
        if marker in normalized:
            return marker
    cooldown_seconds = parse_cooldown_seconds(normalized)
    if cooldown_seconds is not None and (
        "vote again in" in normalized or "can vote again in" in normalized
    ):
        return "bounded cooldown"
    return None


def vote_text_confirms_success(text: str) -> bool:
    return vote_success_evidence(text) is not None


async def persisted_vote_confirmation(tab: Any, bot_id: str) -> dict:
    """Confirm server-persisted state for the exact bot after a fresh page load."""
    url = await current_url(tab)
    text = (await body_text(tab)).lower()
    button = await mark_vote_button(tab)
    vote_enabled = bool(button.get("found") and not button.get("disabled"))
    evidence = vote_success_evidence(text)
    login_required = any(
        marker in text
        for marker in ("must be logged in", "login to vote", "log in to vote")
    )
    exact_vote_page = is_topgg_vote_url(url, bot_id)
    return {
        "confirmed": (
            exact_vote_page
            and not login_required
            and bool(evidence)
            and not vote_enabled
        ),
        "evidence": evidence,
        "vote_enabled": vote_enabled,
        "exact_vote_page": exact_vote_page,
        "login_required": login_required,
    }


async def vote_page_confirmation(tab: Any, bot_id: str) -> dict:
    """Observe application acknowledgement without navigation or extra requests."""
    unknown = {"observed": False, "confirmed": False, "evidence": None}
    script = """(() => {
        const text = document.body ? document.body.innerText : '';
        const body = text.toLowerCase();
        const title = (document.title || '').trim().toLowerCase();
        const visible = node => {
            if (!node || !(node.getClientRects().length || node.offsetWidth || node.offsetHeight)) return false;
            const style = getComputedStyle(node);
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                style.visibility !== 'collapse' && style.opacity !== '0';
        };
        const controls = [...document.querySelectorAll('button, [role="button"]')];
        const gates = [...document.querySelectorAll(
            '#challenge-running, #challenge-stage, #challenge-form'
        )];
        const widgets = [...document.querySelectorAll([
            'iframe[src*="challenges.cloudflare.com"]', 'iframe[src*="hcaptcha.com"]',
            'iframe[src*="recaptcha"]', '.cf-turnstile', '.h-captcha', '.g-recaptcha'
        ].join(','))];
        return {
            text: text.slice(0, 250000),
            exact_vote_page: location.protocol === 'https:' &&
                ['top.gg', 'www.top.gg'].includes(location.hostname) &&
                location.pathname.replace(/\\/+$/, '') === '/bot/' + __BOT_ID__ + '/vote',
            ready: document.readyState === 'complete' || document.readyState === 'interactive',
            vote_enabled: controls.some(node => visible(node) &&
                (node.textContent || '').trim().toLowerCase() === 'vote' &&
                !node.disabled && !node.hasAttribute('disabled') &&
                node.getAttribute('aria-disabled') !== 'true'),
            challenge: title.startsWith('just a moment') || title.startsWith('attention required') ||
                body.includes('performing security verification') ||
                body.includes('needs to review the security of your connection') ||
                body.includes('verify you are human') || body.includes('complete the captcha') ||
                body.includes('please solve the captcha to continue') ||
                widgets.some(visible) || gates.some(visible),
            login_required: body.includes('must be logged in') ||
                body.includes('login to vote') || body.includes('log in to vote'),
            error_present: body.includes('failed to vote') || body.includes('vote failed') ||
                body.includes('something went wrong') || body.includes('please try again')
        };
    })()""".replace("__BOT_ID__", json.dumps(bot_id))
    try:
        result = await asyncio.wait_for(evaluate(tab, script), timeout=2)
    except Exception:
        return unknown
    flags = ("exact_vote_page", "ready", "vote_enabled", "challenge", "login_required", "error_present")
    if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
            or not all(isinstance(result.get(key), bool) for key in flags)):
        return unknown
    evidence = vote_success_evidence(result["text"])
    return {
        "observed": result["exact_vote_page"] and result["ready"],
        "evidence": evidence,
        "confirmed": bool(evidence) and result["exact_vote_page"] and result["ready"]
        and not any(result[key] for key in ("vote_enabled", "challenge", "login_required", "error_present")),
    }


async def confirm_vote_without_reload(tab: Any, bot_id: str, before: dict) -> bool:
    """Require a new, stable acknowledgement after the specific Vote click."""
    if before.get("observed") is not True or before.get("evidence") is not None:
        return False
    previous_evidence = None
    for attempt in range(4):
        snapshot = await vote_page_confirmation(tab, bot_id)
        evidence = snapshot.get("evidence") if snapshot.get("confirmed") is True else None
        if evidence is not None and evidence == previous_evidence:
            print(f"  ✅ Vote acknowledged on the current page for {bot_id} ({evidence})")
            return True
        previous_evidence = evidence
        if attempt < 3:
            await asyncio.sleep(2)
    return False


def successful_vote_result(bot_id: str) -> dict:
    confirmed_at = int(datetime.now(timezone.utc).timestamp())
    return {
        "bot_id": bot_id,
        "status": "success",
        "detail": "Vote successful",
        "retry_at": confirmed_at + SUCCESS_NEXT_VOTE_DELAY_SEC,
    }


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


def should_request_protection_retry(all_results: list[list[dict]]) -> bool:
    results = [result for account_results in all_results for result in account_results]
    # A fresh workflow processes every configured account/bot. Without a
    # cross-run submission ledger it must not repeat an unconfirmed submission,
    # even when another bot was blocked before its own Vote click.
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


def is_topgg_vote_url(url: str, bot_id: str) -> bool:
    parsed = urlparse(url)
    return (
        url_has_domain(url, "top.gg")
        and parsed.path.rstrip("/") == f"/bot/{bot_id}/vote"
    )


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
        const wanted = new Set({json.dumps(texts)}.map(text => text.trim().toLowerCase()));
        const nodes = [...document.querySelectorAll({json.dumps(selector)})];
        const element = nodes.find(node =>
            wanted.has((node.textContent || '').trim().toLowerCase())
        );
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


def topgg_cookie_param(cookie: dict) -> Any:
    same_site = uc.cdp.network.CookieSameSite(cookie["sameSite"])
    expires = (
        uc.cdp.network.TimeSinceEpoch(cookie["expires"])
        if cookie.get("expires")
        else None
    )
    kwargs = {
        "name": cookie["name"],
        "value": cookie["value"],
        "path": cookie["path"],
        "secure": cookie["secure"],
        "http_only": cookie["httpOnly"],
        "same_site": same_site,
        "expires": expires,
    }
    if cookie["name"].startswith("__Host-"):
        # __Host- cookies must be host-only: Secure, Path=/, and no Domain.
        kwargs["url"] = "https://top.gg/"
        kwargs["path"] = "/"
    else:
        kwargs["domain"] = cookie["domain"]
    return uc.cdp.network.CookieParam(**kwargs)


async def inject_topgg_cookies(browser: Any, cookies: list[dict]) -> None:
    params = [topgg_cookie_param(cookie) for cookie in cookies]
    if params:
        await browser.cookies.set_all(params)


async def topgg_session_probe(tab: Any) -> dict:
    """Return a credential-free Auth.js probe result for diagnostics and decisions."""
    script = """(async () => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), __PROBE_TIMEOUT_MS__);
        try {
            const response = await fetch('/api/auth/session', {
                credentials: 'include',
                cache: 'no-store',
                signal: controller.signal,
            });
            const probe = {
                ok: Boolean(response.ok),
                status: Number(response.status || 0),
                contentType: String(response.headers.get('content-type') || '')
                    .split(';', 1)[0]
                    .slice(0, 80),
                jsonOk: false,
                userPresent: false,
                error: null,
                cfMitigated: String(response.headers.get('cf-mitigated') || '')
                    .slice(0, 40),
                // Only the non-secret Cloudflare request correlation ID.
                cfRay: String(response.headers.get('cf-ray') || '')
                    .replace(/[^a-zA-Z0-9-]/g, '')
                    .slice(0, 64),
                server: String(response.headers.get('server') || '')
                    .slice(0, 40),
            };
            if (!response.ok) return probe;
            try {
                const session = await response.json();
                probe.jsonOk = true;
                probe.userPresent = Boolean(session && session.user);
                return probe;
            } catch (error) {
                probe.error = 'json:' + (error && error.name ? error.name : 'Error');
                return probe;
            }
        } catch (error) {
            return {
                ok: false,
                status: 0,
                contentType: '',
                jsonOk: false,
                userPresent: false,
                error: 'fetch:' + (error && error.name ? error.name : 'Error'),
                cfMitigated: '',
                cfRay: '',
                server: '',
            };
        } finally {
            clearTimeout(timer);
        }
    })()""".replace("__PROBE_TIMEOUT_MS__", str(int(SESSION_PROBE_TIMEOUT_SEC * 1000)))
    try:
        result = await asyncio.wait_for(
            evaluate(tab, script), timeout=SESSION_PROBE_TIMEOUT_SEC + 2
        )
    except (TimeoutError, asyncio.TimeoutError):
        result = {"status": 0, "error": "session-probe-timeout"}

    if not isinstance(result, dict):
        result = {
            "ok": False,
            "status": None,
            "contentType": "",
            "jsonOk": False,
            "userPresent": False,
            "error": "invalid-probe-result",
            "cfMitigated": "",
            "cfRay": "",
            "server": "",
        }

    raw_status = result.get("status")
    status = int(raw_status) if isinstance(raw_status, (int, float)) else None
    content_type = str(result.get("contentType") or "").lower()[:80]
    error = str(result.get("error") or "")[:80]
    mitigated = str(result.get("cfMitigated") or "").lower()[:40]
    cf_ray = re.sub(r"[^a-zA-Z0-9-]", "", str(result.get("cfRay") or ""))[:64]
    server = str(result.get("server") or "").lower()[:40]
    json_ok = result.get("jsonOk") is True
    authenticated = (
        status == 200
        and result.get("ok") is True
        and json_ok
        and result.get("userPresent") is True
        and mitigated != "challenge"
    )

    if status in {403, 429}:
        if mitigated == "challenge":
            protection_reason = "cloudflare-challenge"
        elif server == "cloudflare":
            protection_reason = "cloudflare-server; actual blocking rule unknown"
        else:
            protection_reason = "unknown; could be application or edge"
        # Never log response HTML, cookies, session data, or request headers.
        print(
            f"  🔍 Access denial diagnostic: status={status}, "
            f"origin={protection_reason}, "
            f"cf-ray={cf_ray or 'not-exposed'}"
        )

    status_text = str(status) if status is not None else "?"
    content_text = content_type or "unknown"
    if error:
        print(
            f"  🔎 top.gg session probe: HTTP {status_text}, "
            f"content-type={content_text}, error={error}"
        )
    elif authenticated:
        print(
            f"  🔎 top.gg session probe: HTTP {status_text}, "
            f"content-type={content_text}, session-user=present"
        )
    elif json_ok:
        print(
            f"  🔎 top.gg session probe: HTTP {status_text}, "
            f"content-type={content_text}, session-user=absent"
        )
    else:
        print(
            f"  🔎 top.gg session probe: HTTP {status_text}, "
            f"content-type={content_text}, response-not-usable"
        )

    return {
        "authenticated": authenticated,
        "status": status,
        "content_type": content_type,
        "json_ok": json_ok,
        "error": error,
        "cf_mitigated": mitigated,
        "cf_ray": cf_ray,
        "server": server,
    }


async def is_topgg_authenticated(tab: Any) -> bool:
    return bool((await topgg_session_probe(tab)).get("authenticated"))


async def topgg_page_auth_hint(tab: Any) -> str:
    """Infer auth only from strong vote-page UI signals; otherwise return unknown."""
    result = await evaluate(tab, """(() => {
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        if (location.protocol !== 'https:' ||
            !['top.gg', 'www.top.gg'].includes(location.hostname) ||
            document.readyState === 'loading') {
            return 'unknown';
        }
        const controls = [...document.querySelectorAll('button, a, [role="button"]')];
        const voteButtons = [...document.querySelectorAll('button, [role="button"]')];
        const exactText = (node) => (node.textContent || '').trim().toLowerCase();
        const isVisible = (node) => Boolean(
            node && (node.getClientRects().length || node.offsetWidth || node.offsetHeight)
        );
        const hasVoteButton = voteButtons.some(node =>
            exactText(node) === 'vote' &&
            !node.disabled &&
            node.getAttribute('aria-disabled') !== 'true' &&
            isVisible(node)
        );
        const hasLoginButton = controls.some(node =>
            ['login', 'log in', 'sign in'].includes(exactText(node)) &&
            isVisible(node)
        );
        const loginRequired =
            body.includes('must be logged in') ||
            body.includes('login to vote') ||
            body.includes('log in to vote');
        const hasVoteSurface =
            hasVoteButton ||
            body.includes('you will be able to vote after this ad') ||
            body.includes('vote again in') ||
            body.includes('already voted') ||
            body.includes('can vote again');

        if (loginRequired || (hasLoginButton && !hasVoteSurface)) return 'invalid';
        if (hasVoteSurface) return 'authenticated';
        return 'unknown';
    })()""")
    return result if result in {AUTHENTICATED, AUTH_INVALID} else "unknown"


def probe_looks_blocked(probe: dict) -> bool:
    status = probe.get("status")
    content_type = str(probe.get("content_type") or "")
    return (
        probe.get("cf_mitigated") == "challenge"
        or status in {403, 429}
        or (isinstance(status, int) and status >= 500)
        or content_type == "text/html"
        or bool(probe.get("error"))
    )


async def topgg_auth_state(tab: Any) -> str:
    await dismiss_privacy_overlay(tab)

    page_hint = await topgg_page_auth_hint(tab)
    if page_hint == AUTHENTICATED:
        print("  ✅ top.gg vote page shows an authenticated voting surface")
        return AUTHENTICATED

    # Do not call the Auth.js session endpoint through an active protection
    # challenge. That request is expected to produce a 403 and adds no useful
    # authentication signal. Clear the page challenge first, then probe only
    # if the vote surface is still ambiguous.
    if await is_turnstile_present(tab):
        print("  → top.gg protection is active; clearing it before session validation")
        if not await solve_turnstile(tab):
            return await unresolved_challenge_auth_state(tab)
        await asyncio.sleep(2)
        await settle_privacy_overlay(tab)

        # Prefer an application control if it appears while the page settles.
        # An unknown DOM hint is not an authentication failure: preserve the
        # bounded session-probe fallback used before the audit.
        for settle_attempt in range(AUTH_PAGE_SETTLE_POLLS):
            page_hint = await topgg_page_auth_hint(tab)
            if page_hint in {AUTHENTICATED, AUTH_INVALID}:
                break
            if settle_attempt < AUTH_PAGE_SETTLE_POLLS - 1:
                await asyncio.sleep(AUTH_PAGE_SETTLE_DELAY_SEC)
        if page_hint == AUTHENTICATED:
            print("  ✅ top.gg vote page became usable after verification")
            return AUTHENTICATED

    probe = await topgg_session_probe(tab)
    if probe.get("authenticated"):
        return AUTHENTICATED

    if probe_looks_blocked(probe):
        return AUTH_BLOCKED
    if probe.get("status") == 200 and probe.get("json_ok"):
        return AUTH_INVALID
    if page_hint == AUTH_INVALID:
        return AUTH_INVALID
    return AUTH_BLOCKED


async def login_with_cookies(tab: Any, cookies: list[dict], bot_ids: list[str]) -> str:
    if not cookies:
        return AUTH_INVALID

    print("  → Injecting top.gg Auth.js cookies...")
    await inject_topgg_cookies(tab.browser, cookies)

    vote_url = f"https://top.gg/bot/{bot_ids[0]}/vote"
    await tab.get(vote_url)
    await asyncio.sleep(3)

    retry_delays = (0, 6)
    last_state = AUTH_BLOCKED

    for attempt, delay in enumerate(retry_delays, 1):
        if attempt > 1:
            print(
                f"  ↺ Rechecking top.gg cookie session on the same verified page "
                f"({attempt}/{len(retry_delays)})..."
            )
            await asyncio.sleep(delay)

        await settle_privacy_overlay(tab)
        state = await topgg_auth_state(tab)
        last_state = state

        if state == AUTHENTICATED:
            print("  ✅ Authenticated via top.gg cookies")
            return AUTHENTICATED
        if state == AUTH_CAPTCHA_REQUIRED:
            print("  🔒 CAPTCHA blocked top.gg cookie authentication")
            return AUTH_CAPTCHA_REQUIRED
        if state == AUTH_INVALID:
            print("  ⚠️  top.gg cookie session is explicitly unauthenticated")
            return AUTH_INVALID

    print("  ⏳ top.gg session validation is temporarily blocked; defer retry")
    return last_state


async def _handle_discord_oauth(tab: Any) -> str:
    print("  → Handling Discord OAuth dialog...")
    for attempt in range(12):
        if url_has_domain(await current_url(tab), "top.gg"):
            return AUTHENTICATED
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await unresolved_challenge_auth_state(tab)
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
    print("  → Preparing Discord session for top.gg login...")
    vote_url = f"https://top.gg/bot/{bot_ids[0]}/vote"

    await tab.get(vote_url)
    await asyncio.sleep(2)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Already logged into top.gg")
        return state
    if state in {AUTH_CAPTCHA_REQUIRED, AUTH_BLOCKED}:
        return state

    print("  → Establishing Discord browser session...")
    await tab.get(DISCORD_LOGIN_URL)
    await asyncio.sleep(2)
    if not url_has_domain(await current_url(tab), "discord.com"):
        print("  ❌ Discord login page did not open")
        return AUTH_INVALID

    await evaluate(tab, f"""(() => {{
        const token = {json.dumps(token)};
        localStorage.setItem('token', JSON.stringify(token));
        localStorage.setItem('tokens', JSON.stringify({{"default": token}}));
    }})()""")
    await tab.reload()
    await asyncio.sleep(3)

    print("  → Navigating to top.gg to initiate OAuth...")
    await tab.get(vote_url)
    await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Session established before OAuth redirect")
        return state
    if state in {AUTH_CAPTCHA_REQUIRED, AUTH_BLOCKED}:
        return state

    marker = "data-auto-login"
    if not await _mark_exact_element(
        tab,
        "a,button,[role=\"button\"]",
        ["Login", "Log in", "Sign in"],
        marker,
    ):
        print("  ❌ Could not find top.gg Login button")
        return AUTH_INVALID
    if not await _click_marked(tab, marker):
        print("  ❌ Could not click top.gg Login button")
        return AUTH_INVALID
    if not await wait_for_domain(tab, "discord.com", TIMEOUT_OAUTH_SEC):
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await unresolved_challenge_auth_state(tab)
        print("  ❌ Discord OAuth page did not open")
        return AUTH_INVALID
    if "/oauth2/authorize" not in urlparse(await current_url(tab)).path:
        print("  ❌ Unexpected Discord redirect")
        return AUTH_INVALID

    oauth_state = await _handle_discord_oauth(tab)
    if oauth_state in {AUTH_CAPTCHA_REQUIRED, AUTH_BLOCKED}:
        return oauth_state
    if oauth_state != AUTHENTICATED:
        print("  ❌ Could not authorize top.gg")
        return AUTH_INVALID
    if not await wait_for_domain(tab, "top.gg", TIMEOUT_OAUTH_SEC):
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await unresolved_challenge_auth_state(tab)
        print("  ❌ OAuth redirect failed")
        return AUTH_INVALID

    await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    print("  ✅ Logged into top.gg" if state == AUTHENTICATED else "  ❌ top.gg session not established")
    return state


async def is_cloudflare_challenge_page(tab: Any) -> bool:
    """Recognize a full-page access gate, independently of embedded widgets."""
    result = await evaluate(tab, """(() => {
        const body = document.body ? document.body.innerText.toLowerCase() : '';
        const title = (document.title || '').trim().toLowerCase();
        return Boolean(
            title.startsWith('just a moment') ||
            title.startsWith('attention required') ||
            document.querySelector('#challenge-running, #challenge-stage, #challenge-form') ||
            body.includes('performing security verification') ||
            body.includes('needs to review the security of your connection')
        );
    })()""")
    return result is True


async def unresolved_challenge_auth_state(tab: Any) -> str:
    # The broad challenge detector also recognizes Cloudflare interstitials.
    # A timeout there does not establish that a manual CAPTCHA is required.
    if await is_cloudflare_challenge_page(tab):
        print("  ⏳ Cloudflare challenge page remains active; access is blocked")
        return AUTH_BLOCKED
    return AUTH_CAPTCHA_REQUIRED


async def is_turnstile_present(tab: Any) -> bool:
    # Keep widget detection separate from the full-page error classifier.
    # A residual title/container must not keep this click/wait loop active.
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
            .filter(node => (node.textContent || '').trim().toLowerCase() === 'vote');
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
                ['login', 'log in', 'sign in'].includes((node.textContent || '').trim().toLowerCase()))
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


async def solve_turnstile(tab: Any) -> bool:
    await dismiss_privacy_overlay(tab)
    if await is_turnstile_solved(tab):
        return True
    if not await is_turnstile_present(tab):
        return True
    await log_challenge_diagnostic(tab, "detected")
    print("  → Challenge detected; attempting library verification click...")
    try:
        await tab.verify_cf()
        print("  → Verification click dispatched; target match and acceptance unconfirmed")
    except Exception as exc:
        dbg(f"verify_cf failed: {type(exc).__name__}")
        print(f"  ⚠️  Turnstile checkbox click failed ({type(exc).__name__})")
        await log_challenge_diagnostic(tab, "click_error")
        return False
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    while asyncio.get_running_loop().time() < deadline:
        if await is_turnstile_solved(tab):
            print("  ✅ Turnstile response received")
            return True
        if not await is_turnstile_present(tab):
            print("  → Challenge widget disappeared; application access still needs verification")
            return True
        await asyncio.sleep(2)
    await log_challenge_diagnostic(tab, "timeout")
    print("  ⚠️  Challenge signals remained active after verification attempt")
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


async def unresolved_challenge_result(
    tab: Any,
    bot_id: str,
    detail: str,
    account_id: str = "unknown",
    *,
    after_vote: bool = False,
) -> dict:
    """Keep persistent interstitials separate from standalone CAPTCHA outcomes."""
    if await unresolved_challenge_auth_state(tab) == AUTH_BLOCKED:
        blocked_detail = (
            "Cloudflare security challenge persisted; vote outcome remains unverified"
            if after_vote
            else "Cloudflare security challenge persisted; access denied before Vote"
        )
        print(f"  ⏳ {blocked_detail} for {bot_id}")
        return {"bot_id": bot_id, "status": "blocked", "detail": blocked_detail}
    return await captcha_result(tab, bot_id, detail, account_id)


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
        document.querySelectorAll('[data-auto-vote]').forEach(
            el => el.removeAttribute('data-auto-vote')
        );
        const visible = (el) => Boolean(
            el && (el.getClientRects().length || el.offsetWidth || el.offsetHeight)
        );
        const controls = [...document.querySelectorAll('button, [role="button"], a')];
        const button = controls.find(el =>
            visible(el) &&
            (el.textContent || '').trim().toLowerCase() === 'vote'
        );
        if (!button) return {found: false, disabled: true, visible: false};
        const disabled = Boolean(
            button.disabled ||
            button.getAttribute('aria-disabled') === 'true' ||
            button.hasAttribute('disabled')
        );
        button.setAttribute('data-auto-vote', '1');
        return {found: true, disabled, visible: true};
    })()""") or {})


async def vote_for_bot(tab: Any, bot_id: str, account_id: str = "unknown") -> dict:
    print(f"  → Voting for bot {bot_id}...")
    vote_url = f"https://top.gg/bot/{bot_id}/vote"
    if is_topgg_vote_url(await current_url(tab), bot_id):
        print("  → Reusing current top.gg vote page to preserve verified browser state")
    else:
        await tab.get(vote_url)
        await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        dbg("top.gg session not applied yet; reloading once")
        await tab.reload()
        await asyncio.sleep(3)
        await settle_privacy_overlay(tab)
        text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
    if page_indicates_cooldown(text):
        print(f"  ⏳ Already voted for {bot_id} (cooldown)")
        return cooldown_result(bot_id, text)
    if "could not be found" in text or "404" in str(await evaluate(tab, "document.title")):
        return {"bot_id": bot_id, "status": "error", "detail": "Vote page 404"}

    turnstile_cycles = 0
    if await is_turnstile_present(tab):
        turnstile_cycles += 1
        if not await solve_turnstile(tab):
            return await unresolved_challenge_result(
                tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
            )
        await asyncio.sleep(2)

    ad_error = await wait_for_ad(tab, bot_id)
    if ad_error:
        return ad_error

    if await is_turnstile_present(tab):
        turnstile_cycles += 1
        if turnstile_cycles >= MAX_TURNSTILE_CYCLES_PER_PHASE:
            print(
                f"  ⏳ Repeated protection challenge before Vote became available "
                f"for {bot_id}"
            )
            return {
                "bot_id": bot_id,
                "status": "blocked",
                "detail": "Repeated protection challenge before Vote became available",
            }
        if not await solve_turnstile(tab):
            return await unresolved_challenge_result(
                tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
            )
        await asyncio.sleep(2)

    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    state = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await mark_vote_button(tab)
        if state.get("found") and not state.get("disabled"):
            break
        if await is_turnstile_present(tab):
            turnstile_cycles += 1
            if turnstile_cycles >= MAX_TURNSTILE_CYCLES_PER_PHASE:
                print(
                    f"  ⏳ Repeated protection challenge before Vote became available "
                    f"for {bot_id}"
                )
                return {
                    "bot_id": bot_id,
                    "status": "blocked",
                    "detail": "Repeated protection challenge before Vote became available",
                }
            if not await solve_turnstile(tab):
                return await unresolved_challenge_result(
                    tab, bot_id, "Interactive CAPTCHA requires manual completion", account_id
                )
            await asyncio.sleep(2)
        else:
            await asyncio.sleep(2)
    else:
        text = (await body_text(tab)).lower()
        if page_indicates_cooldown(text):
            print(f"  ⏳ Already voted for {bot_id} (cooldown)")
            return cooldown_result(bot_id, text)
        if "must be logged in" in text or "login to vote" in text or "log in to vote" in text:
            return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
        path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_no_btn.png")
        if path:
            await notify_error_screenshot(bot_id, path, "Vote button unavailable")
        detail = "Vote button disabled" if state.get("found") else "Vote button not found"
        return {"bot_id": bot_id, "status": "error", "detail": detail}

    before_click = await vote_page_confirmation(tab, bot_id)
    print("  → Clicking Vote...")
    try:
        if not await _click_marked(tab, "data-auto-vote"):
            return {
                "bot_id": bot_id, "status": "uncertain", "vote_submitted": True,
                "detail": "Vote click outcome unavailable; automatic resubmission suppressed",
            }
        result = await verify_submitted_vote(tab, bot_id, account_id, before_click)
    except Exception as exc:
        # The click may have reached the server even if its browser command or
        # the subsequent confirmation failed. Never blindly submit it again.
        dbg(f"Post-click confirmation failed: {type(exc).__name__}")
        result = {
            "bot_id": bot_id, "status": "uncertain",
            "detail": "Vote submitted; browser confirmation unavailable",
        }
    if result.get("status") not in COMPLETED_STATUSES:
        result["vote_submitted"] = True
    return result


async def verify_submitted_vote(tab: Any, bot_id: str, account_id: str, before_click: dict) -> dict:
    await asyncio.sleep(5)

    if await confirm_vote_without_reload(tab, bot_id, before_click):
        result = successful_vote_result(bot_id)
        result["detail"] = "Vote acknowledged on page"
        return result

    if await is_turnstile_present(tab):
        if not await solve_turnstile(tab):
            return await unresolved_challenge_result(
                tab,
                bot_id,
                "CAPTCHA still required after solver attempt following Vote click",
                account_id,
                after_vote=True,
            )
        await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)

        if await confirm_vote_without_reload(tab, bot_id, before_click):
            result = successful_vote_result(bot_id)
            result["detail"] = "Vote acknowledged on page after verification"
            return result

    # No fresh, stable acknowledgement was observed. Keep independent page
    # verification as a fallback; a click or generic success text alone cannot
    # establish completion. Runs #144/#145 show why reload must not be mandatory
    # after the application has already acknowledged the vote.
    last_confirmation = {"confirmed": False, "evidence": None, "vote_enabled": False}
    for verification_attempt in range(1, POST_VOTE_VERIFY_ATTEMPTS + 1):
        print(
            f"  → Verifying persisted vote state "
            f"({verification_attempt}/{POST_VOTE_VERIFY_ATTEMPTS})..."
        )
        await tab.reload()
        await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)
        await settle_privacy_overlay(tab)

        verification_challenged = await is_turnstile_present(tab)
        if verification_challenged:
            if not await solve_turnstile(tab):
                return await unresolved_challenge_result(
                    tab,
                    bot_id,
                    "CAPTCHA still required after solver attempt during vote verification",
                    account_id,
                    after_vote=True,
                )
            await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)
            await settle_privacy_overlay(tab)

        last_confirmation = await persisted_vote_confirmation(tab, bot_id)
        if last_confirmation.get("confirmed"):
            evidence = str(last_confirmation.get("evidence") or "server state")
            print(
                f"  ✅ Successfully voted for {bot_id} "
                f"(persisted confirmation: {evidence})"
            )
            return successful_vote_result(bot_id)

        if verification_challenged:
            # Both #136 and #138 encountered a new challenge after each
            # verification reload. Repeated navigation through protection
            # creates more challenges without establishing whether the click
            # persisted. Observe the *already reloaded* page briefly instead.
            for settle_attempt in range(1, POST_VOTE_CHALLENGE_SETTLE_POLLS + 1):
                print(
                    f"  → Rechecking the verified page without another reload "
                    f"({settle_attempt}/{POST_VOTE_CHALLENGE_SETTLE_POLLS})..."
                )
                await asyncio.sleep(POST_VOTE_CHALLENGE_SETTLE_DELAY_SEC)
                await settle_privacy_overlay(tab)
                if await is_turnstile_present(tab):
                    print("  ⏳ Protection returned during vote verification")
                    break
                last_confirmation = await persisted_vote_confirmation(tab, bot_id)
                if last_confirmation.get("confirmed"):
                    evidence = str(last_confirmation.get("evidence") or "server state")
                    print(
                        f"  ✅ Successfully voted for {bot_id} "
                        f"(persisted confirmation: {evidence})"
                    )
                    return successful_vote_result(bot_id)

            # Do not trust the same DOM as the Vote click. The first reload
            # already supplied independent evidence; if it remains ambiguous
            # after a challenge, retain the unconfirmed submission for a later
            # scheduled/manual run instead of immediately submitting again.
            print(
                "  ⏳ Challenged verification remains inconclusive; "
                "avoiding a second protection-triggering reload"
            )
            break

        if last_confirmation.get("vote_enabled"):
            print(
                f"  ⚠️  Vote is still available after verification "
                f"for {bot_id}; treating click as unconfirmed"
            )
            break
        if not last_confirmation.get("exact_vote_page", True):
            print(
                f"  ⚠️  Vote verification left the expected bot page "
                f"for {bot_id}; treating click as unconfirmed"
            )
            break
        if last_confirmation.get("login_required"):
            print(
                f"  ⚠️  Vote verification lost authenticated state "
                f"for {bot_id}; treating click as unconfirmed"
            )
            break

        # Only spend a second reload on an ambiguous first result. A strong
        # confirmation after one fresh server round-trip is sufficient; an
        # unconditional second reload just increases Cloudflare exposure.
        if verification_attempt < POST_VOTE_VERIFY_ATTEMPTS:
            await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)

    path = await browser_screenshot(
        tab,
        f"screenshots/vote_{account_id}_{bot_id}_unconfirmed.png",
        required=True,
    )
    if path:
        await notify_error_screenshot(
            bot_id,
            path,
            "Vote outcome unconfirmed after independent page verification",
        )
    detail = (
        "Vote still available after post-click verification"
        if last_confirmation.get("vote_enabled")
        else "Clicked, but server-side vote confirmation was not observed"
    )
    return {"bot_id": bot_id, "status": "uncertain", "detail": detail}


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
            await asyncio.wait_for(
                browser.start(),
                timeout=BROWSER_START_CALL_TIMEOUT_SEC,
            )
            await asyncio.wait_for(
                browser.get("about:blank"),
                timeout=BROWSER_INITIAL_PAGE_TIMEOUT_SEC,
            )
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


async def _run_account(
    token: str,
    bot_ids: list[str],
    account_id: str,
    account_cookies: list[dict] | None = None,
    *,
    capture_auth_failure: bool = False,
) -> list[dict]:
    browser = await start_browser()
    results = []
    try:
        tab = next(iter(browser))
        auth_state = AUTH_INVALID
        if account_cookies:
            auth_state = await login_with_cookies(tab, account_cookies, bot_ids)
            if auth_state == AUTH_INVALID:
                print("  → Cookie auth is invalid; falling back to Discord OAuth...")
                await browser.cookies.clear()
            elif auth_state == AUTH_BLOCKED:
                print("  ⏳ top.gg is blocking this browser; skipping OAuth on the same session")
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
            blocked = auth_state == AUTH_BLOCKED
            result = {
                "bot_id": "all",
                "status": "blocked" if blocked else "auth_failed",
                "detail": (
                    "top.gg temporarily blocked session validation"
                    if blocked
                    else "Top.gg authentication failed"
                ),
                "account_id": account_id,
            }
            if capture_auth_failure or blocked:
                path = await browser_screenshot(
                    tab,
                    f"screenshots/auth_{account_id}_failed.png",
                    required=True,
                )
                if path:
                    result["screenshot_path"] = path
            return [result]

        for position, bot_id in enumerate(bot_ids):
            try:
                result = await vote_for_bot(tab, bot_id, account_id)
            except Exception as exc:
                # A later browser/navigation failure must not discard earlier
                # votes and cause the next attempt to submit them again.
                detail = f"{type(exc).__name__}: transient browser failure"
                results.extend({
                    "bot_id": remaining, "status": "error",
                    "detail": detail, "account_id": account_id,
                } for remaining in bot_ids[position:])
                return results
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
        await close_browser_safely(browser, "account attempt")
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
    blocked_attempts = 0
    print(f"\n{'─' * 45}")
    print(f"{prefix} Processing account...")

    def apply_account_error(error: dict) -> list[dict]:
        if not results_by_bot:
            return [error]
        for bot_id in pending:
            results_by_bot[bot_id] = {**error, "bot_id": bot_id}
        return [results_by_bot[bot_id] for bot_id in bot_ids]

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
            if results_by_bot:
                apply_account_error(last_account_error)
            dbg(f"Account attempt failed: {type(exc).__name__}")
            print(f"{prefix} ❌ Attempt {attempt} failed: {detail}")
            if isinstance(exc, BrowserStartupError):
                break
            continue

        if attempt_results and attempt_results[0].get("bot_id") == "all":
            last_account_error = attempt_results[0]
            status = last_account_error.get("status")
            if status == "blocked":
                blocked_attempts += 1
                if blocked_attempts < MAX_BLOCKED_ATTEMPTS and attempt < MAX_RETRIES:
                    print(f"{prefix} ↺ Protection block detected; trying one fresh browser")
                    continue
                print(f"{prefix} ⏳ Protection block persists; deferring to scheduled retry")
                return apply_account_error(last_account_error)
            if not is_retryable_result(last_account_error):
                print(f"{prefix} 🔒 Authentication requires manual CAPTCHA")
                return apply_account_error(last_account_error)
            print(f"{prefix} ❌ Authentication attempt {attempt} failed")
            if results_by_bot:
                apply_account_error(last_account_error)
            continue
        last_account_error = None
        # An incomplete browser response is not successful completion. Keep a
        # result for every requested bot, including any missing from a response.
        returned_ids = {str(result.get("bot_id")) for result in attempt_results}
        attempt_results = list(attempt_results) + [{
            "bot_id": bot_id, "status": "error",
            "detail": "Browser returned no result for this bot", "account_id": account_id,
        } for bot_id in pending if bot_id not in returned_ids]
        for result in attempt_results:
            if str(result["bot_id"]) in pending:
                results_by_bot[str(result["bot_id"])] = result

        blocked_bot_ids = [
            str(result["bot_id"])
            for result in attempt_results
            if result.get("status") == "blocked"
            and result.get("vote_submitted") is not True
            and result.get("bot_id") not in {None, "all"}
        ]
        transient_bot_ids = retryable_bot_ids(attempt_results)

        if blocked_bot_ids:
            blocked_attempts += 1
            if blocked_attempts < MAX_BLOCKED_ATTEMPTS and attempt < MAX_RETRIES:
                pending = list(dict.fromkeys(blocked_bot_ids + transient_bot_ids))
                print(f"{prefix} ↺ Vote-page protection block; trying one fresh browser")
                continue
            print(f"{prefix} ⏳ Vote-page protection block persists; deferring")
            return [results_by_bot[bot_id] for bot_id in bot_ids if bot_id in results_by_bot]

        pending = transient_bot_ids
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
                "captcha_required": "🔒", "blocked": "⏳",
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
    SENSITIVE_VALUES.extend(
        str(cookie.get("value", ""))
        for account_cookies in all_cookies
        for cookie in account_cookies
        if cookie.get("value")
    )
    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    total = len(tokens)
    print("🚀 auto-vote-dcbot starting")
    run_source = os.environ.get("RUN_SOURCE", "").strip()
    run_origin_id = os.environ.get("RUN_ORIGIN_ID", "").strip()
    run_recovery_depth = os.environ.get("RUN_RECOVERY_DEPTH", "").strip()
    if run_source:
        print(f"   Source  : {run_source}")
    if run_origin_id:
        print(f"   Origin  : {run_origin_id}")
    if run_recovery_depth:
        print(f"   Recovery: {run_recovery_depth}/2")
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
    if write_protection_retry_state(all_results):
        print("↺ Protection-block fresh-run retry requested")
    report = build_notification(all_results, now)
    send_notification(report)
    await send_captcha_screenshots(all_results)
    await send_auth_failure_screenshots(all_results)
    return 1 if has_business_failure(all_results) else 0


if __name__ == "__main__":
    raise SystemExit(uc.loop().run_until_complete(main()))
