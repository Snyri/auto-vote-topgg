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

import cloudflare_click
import request_diagnostics
import ui_click
import browser_environment
import native_mouse
import flaresolverr_browser
from page_signals import CHALLENGE_JS
from vote_controls import VOTE_CONTROL_JS
from recovery_state import SubmissionJournal

WIB = timezone(timedelta(hours=7))
DISCORD_LOGIN_URL = "https://discord.com/login"
BROWSER_COMMAND_TIMEOUT_SEC = 8
TIMEOUT_OAUTH_SEC = 25
TIMEOUT_VOTE_SEC = 30
SESSION_PROBE_TIMEOUT_SEC = 12
RECOVERY_OBSERVE_TIMEOUT_SEC = 90
AUTH_PAGE_SETTLE_POLLS = 4
AUTH_PAGE_SETTLE_DELAY_SEC = 2
AUTH_RECOVERY_POLLS = 4
AUTH_RECOVERY_DELAY_SEC = 0.5
DOCUMENT_READY_JS = "Boolean(document.body && document.body.hasChildNodes()) && ['interactive', 'complete'].includes(document.readyState)"
DELAY_BETWEEN_BOTS_SEC = 3
DELAY_BETWEEN_ACCOUNTS_SEC = 5
MAX_RETRIES = 5
MAX_BLOCKED_ATTEMPTS = 5
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
VOTE_NETWORK_PREFLIGHT_POLLS = 6
VOTE_NETWORK_PREFLIGHT_DELAY_SEC = 0.5
POST_VOTE_CHALLENGE_SETTLE_POLLS = 2
POST_VOTE_CHALLENGE_SETTLE_DELAY_SEC = 2
VOTE_TARGET_STABLE_MS = 500
VOTE_TARGET_POLL_SEC = 0.25
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


class VoteClickNotReady(RuntimeError):
    """No Vote mouse press was sent; the control never became actionable."""


class VoteAPIBlocked(VoteClickNotReady):
    """No Vote mouse press was sent because a recognized API remains denied."""


TG_BOT_TOKEN = ""
TG_CHAT_ID = ""
SENSITIVE_VALUES: list[str] = []
PRIVACY_DISMISS_REPORTED = False
RECOVERY_JOURNAL = None


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
        await asyncio.wait_for(tab.save_screenshot(filename=path, format="png"), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
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
    """Use the same error/challenge rules after an independent document load."""
    return await vote_page_confirmation(tab, bot_id)


async def confirmed_cooldown(tab: Any, bot_id: str, text: str) -> dict | None:
    """Record cooldown only when the actual vote page agrees, twice."""
    if not page_indicates_cooldown(text):
        return None
    first = await vote_page_confirmation(tab, bot_id)
    if first.get("confirmed") is not True:
        return None
    await asyncio.sleep(0.5)
    second = await vote_page_confirmation(tab, bot_id)
    if (second.get("confirmed") is not True
            or second.get("evidence") != first.get("evidence")):
        return None
    print(f"  ⏳ Already voted for {bot_id} (verified cooldown)")
    return cooldown_result(bot_id, text)

async def vote_page_confirmation(tab: Any, bot_id: str) -> dict:
    """Observe a ready, exact vote page without leaking text or challenge tokens."""
    unknown = {"observed": False, "confirmed": False, "evidence": None,
               "exact_vote_page": False, "ready": False, "vote_enabled": False,
               "challenge": False, "login_required": False, "error_present": False, "ad_pending": False}
    script = "(() => {" + CHALLENGE_JS + r"""
        const text = document.body?.innerText || '';
        const body = text.toLowerCase();
        return {
            text: text.slice(0, 250000),
            exact_vote_page: location.protocol === 'https:' &&
                ['top.gg', 'www.top.gg'].includes(location.hostname) &&
                (!location.port || location.port === '443') &&
                location.pathname.replace(/\/+$/, '') === '/bot/' + __BOT_ID__ + '/vote',
            ready: ['complete', 'interactive'].includes(document.readyState),
            vote_enabled: voteControl.candidates().some(voteControl.enabled),
            challenge: challengeState.present,
            login_required: ['must be logged in', 'login to vote', 'log in to vote'].some(marker => body.includes(marker)),
            error_present: ['failed to vote', 'vote failed', 'something went wrong', 'please try again'].some(marker => body.includes(marker))
        };
    })()""".replace("__BOT_ID__", json.dumps(bot_id))
    try:
        result = await asyncio.wait_for(evaluate(tab, script), timeout=2)
    except Exception:
        return unknown
    flags = ("exact_vote_page", "ready", "vote_enabled", "challenge", "login_required", "error_present")
    if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
            or not all(type(result.get(key)) is bool for key in flags)):
        return unknown
    evidence = vote_success_evidence(result["text"])
    ad_pending = "you will be able to vote after this ad" in result["text"].lower()
    return {
        **{key: result[key] for key in flags},
        "observed": result["exact_vote_page"] and result["ready"],
        "evidence": evidence,
        "ad_pending": ad_pending,
        "confirmed": bool(evidence) and result["exact_vote_page"] and result["ready"]
        and not ad_pending and not any(result[key] for key in ("vote_enabled", "challenge", "login_required", "error_present")),
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
            print(f"  → Stable page acknowledgement observed for {bot_id} ({evidence}); checking final network state")
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


async def body_text(tab: Any) -> str:
    return str(await evaluate(tab, "document.body ? document.body.innerText : ''") or "")


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


def is_topgg_vote_url(url: str, bot_id: str) -> bool:
    return request_diagnostics.bot_vote_page(url, bot_id)

def url_has_domain(url: str, domain: str) -> bool:
    """Match exact hostname or its subdomain, never URL query/path text."""
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


async def wait_for_domain(tab: Any, domain: str, timeout: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if url_has_domain(await asyncio.wait_for(current_url(tab), timeout=max(0.001, deadline - asyncio.get_running_loop().time())), domain):
            return True
        await asyncio.sleep(1)
    return False


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
    if marker == "data-auto-vote":
        return await _click_vote_control(tab)
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


async def _vote_pointer_target(tab: Any, *, arm: bool = False) -> dict:
    """Wait for a stable, unobstructed control and observe trusted target events."""
    script = "(() => {" + CHALLENGE_JS + """
        const state = window.__autoVotePointer || (window.__autoVotePointer = {});
        const blocked = reason => { state.since = null; return {ready: false, reason}; };
        const el = document.querySelector('[data-auto-vote="1"]');
        if (!el || !el.isConnected) return blocked('missing');
        const body = (document.body?.innerText || '').toLowerCase();
        const title = (document.title || '').trim().toLowerCase();
        if (body.includes('you will be able to vote after this ad')) return blocked('ad_active');
        if (challengeState.present) return blocked('protection_active');
        if (!voteControl.action(el)) return blocked('changed');
        if (voteControl.disabled(el)) return blocked('disabled');
        const position = voteControl.position(el, true);
        if (!position.ready) return blocked(position.reason);
        const {x, y, rect} = position;
        const geometry = [rect.left, rect.top, rect.width, rect.height];
        const same = state.element === el && state.geometry &&
            geometry.every((value, i) => Math.abs(value - state.geometry[i]) <= 1);
        if (!same || state.since === null || state.since === undefined) {
            state.since = performance.now();
        }
        state.element = el;
        state.geometry = geometry;
        if (performance.now() - state.since < __STABLE_MS__) return {ready: false, reason: 'settling'};
        if (__ARM__) {
            state.receipt = {pressed: false, released: false, clicked: false};
            state.listeners = ['pointerdown', 'pointerup', 'click'].map(type => {
                const handler = event => {
                    if (event.isTrusted && event.composedPath().includes(el)) {
                        const key = {pointerdown: 'pressed', pointerup: 'released', click: 'clicked'}[type];
                        state.receipt[key] = true;
                        if (type === 'pointerdown') {
                            state.receipt.pressed_at = (performance.timeOrigin + event.timeStamp) / 1000;
                        }
                    }
                };
                window.addEventListener(type, handler, true);
                return [type, handler];
            });
        }
        return {ready: true, x, y};
    })()""".replace("__STABLE_MS__", str(VOTE_TARGET_STABLE_MS)).replace("__ARM__", json.dumps(arm))
    result = await asyncio.wait_for(evaluate(tab, script), timeout=2)
    return result if isinstance(result, dict) else {"ready": False, "reason": "unavailable"}


async def _wait_for_vote_api(tab: Any) -> bool:
    """Observe passive recovery briefly; return whether the caller had to wait."""
    network = request_diagnostics.vote_state(tab)
    if network is None or not network.protection_pending():
        return False
    print("  → Vote API is still challenged; waiting before mouse input")
    for _ in range(VOTE_NETWORK_PREFLIGHT_POLLS):
        await asyncio.sleep(VOTE_NETWORK_PREFLIGHT_DELAY_SEC)
        if not network.protection_pending():
            return True
    raise VoteAPIBlocked("api_protection_active")


async def _click_vote_control(tab: Any) -> bool:
    """Send one native mouse click; a target event still does not prove a vote."""
    pressed = False
    last_reason = None
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    try:
        async with asyncio.timeout(TIMEOUT_VOTE_SEC):
            await browser_environment.foreground(tab)
            while asyncio.get_running_loop().time() < deadline:
                await _wait_for_vote_api(tab)
                await dismiss_privacy_overlay(tab)
                # Reacquire after the ad/React transition; do not retain an old node.
                await mark_vote_button(tab)
                target = await _vote_pointer_target(tab)
                if target.get("ready") is True:
                    await asyncio.wait_for(tab.send(uc.cdp.input_.dispatch_mouse_event(
                        "mouseMoved", x=target["x"], y=target["y"], buttons=0,
                    )), timeout=2)
                    await asyncio.sleep(VOTE_TARGET_POLL_SEC)
                    # A denial can arrive after page preflight or during hover.
                    # If recovery takes time, reacquire the page/control before input.
                    if await _wait_for_vote_api(tab):
                        continue
                    # Hover can change layout or reveal an overlay. Check again
                    # immediately before pressing and attach a target event observer.
                    target = await _vote_pointer_target(tab, arm=True)
                    if target.get("ready") is True:
                        break
                reason = target.get("reason")
                if reason not in {
                    "missing", "ad_active", "protection_active", "changed", "disabled",
                    "hidden", "offscreen", "covered", "settling", "unavailable",
                }:
                    reason = "unavailable"
                if reason != last_reason:
                    print(f"  → Waiting for actionable Vote control: {reason}")
                    last_reason = reason
                await asyncio.sleep(VOTE_TARGET_POLL_SEC)
            else:
                raise VoteClickNotReady(last_reason or "unavailable")

        # No awaited work between this last health check and arming the press.
        network = request_diagnostics.vote_state(tab)
        if network is not None and network.protection_pending():
            raise VoteAPIBlocked("api_protection_active")
        print("  → Sending native mouse press/release to Vote...")
        request_diagnostics.begin_vote_input(tab)
        # From this point a submission may have happened, even if CDP times out.
        if RECOVERY_JOURNAL is not None:
            RECOVERY_JOURNAL.before_press()
        pressed = True
        await native_mouse.press_and_release(tab, target["x"], target["y"])
        receipt = await asyncio.wait_for(evaluate(tab,
            "(() => window.__autoVotePointer?.receipt || {})()"), timeout=2)
        receipt = receipt if isinstance(receipt, dict) else {}
        request_diagnostics.record_vote_receipt(tab, receipt)
        flags = {key: receipt.get(key) is True for key in ("pressed", "released", "clicked")}
        print("  → Vote target received trusted events: " + json.dumps(flags, sort_keys=True))
        return flags["clicked"]
    except Exception as exc:
        if not pressed and not isinstance(exc, VoteClickNotReady):
            reason = last_reason if isinstance(exc, TimeoutError) and last_reason else "browser_unavailable"
            raise VoteClickNotReady(reason) from exc
        raise
    finally:
        with suppress(Exception):
            await asyncio.wait_for(evaluate(tab, """(() => {
                const state = window.__autoVotePointer;
                for (const [type, handler] of state?.listeners || []) {
                    window.removeEventListener(type, handler, true);
                }
                delete window.__autoVotePointer;
            })()"""), timeout=2)


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
        await asyncio.wait_for(browser.cookies.set_all(params), timeout=BROWSER_COMMAND_TIMEOUT_SEC)


async def clear_topgg_auth_cookies(browser: Any) -> None:
    """Remove invalid Auth.js state, preserving clearance and other origins."""
    cookies = await asyncio.wait_for(browser.cookies.get_all(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
    for cookie in cookies:
        domain = str(cookie.domain).lower().lstrip('.').rstrip('.')
        name = str(cookie.name)
        bare = re.sub(r"^__(?:Secure|Host)-", "", name)
        if domain not in {"top.gg", "www.top.gg"} or not bare.startswith(("authjs.", "next-auth.")):
            continue
        tab = next(iter(browser))
        await asyncio.wait_for(tab.send(uc.cdp.network.delete_cookies(name=name, domain=cookie.domain, path=cookie.path)), timeout=BROWSER_COMMAND_TIMEOUT_SEC)


async def topgg_session_probe(tab: Any) -> dict:
    """Return a credential-free Auth.js probe result for diagnostics and decisions."""
    script = """(async () => {
        const skipped = error => ({ok: false, status: 0, contentType: '', jsonOk: false,
            userPresent: false, error, cfMitigated: '', cfRay: '', server: ''});
        if (location.protocol !== 'https:' || !['top.gg', 'www.top.gg'].includes(location.hostname))
            return skipped('unexpected-session-origin');
        if (!(__DOCUMENT_READY__)) return skipped('document-loading');
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
    })()""".replace("__PROBE_TIMEOUT_MS__", str(int(SESSION_PROBE_TIMEOUT_SEC * 1000))).replace("__DOCUMENT_READY__", DOCUMENT_READY_JS)
    try:
        result = await asyncio.wait_for(
            evaluate(tab, script, timeout=SESSION_PROBE_TIMEOUT_SEC + 2),
            timeout=SESSION_PROBE_TIMEOUT_SEC + 2,
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
    result = await evaluate(tab, "(() => {" + VOTE_CONTROL_JS + r"""
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        if (location.protocol !== 'https:' ||
            !['top.gg', 'www.top.gg'].includes(location.hostname) ||
            document.readyState === 'loading') {
            return 'unknown';
        }
        const controls = [...document.querySelectorAll('button, a, [role="button"]')];
        const exactText = (node) => (node.textContent || '').trim().toLowerCase();
        const isVisible = voteControl.visible;
        const hasVoteButton = voteControl.candidates().some(voteControl.enabled);
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

        if (loginRequired) return 'invalid';
        if (hasVoteSurface) return 'authenticated';
        if (body.includes('thanks for voting') && /^\/bot\/[0-9]+\/vote\/?$/.test(location.pathname))
            return 'acknowledgement';
        if (hasLoginButton) return 'invalid';
        return 'unknown';
    })()""")
    if result == "acknowledgement":
        path = urlparse(await current_url(tab)).path
        match = re.fullmatch(r"/bot/([0-9]+)/vote/?", path)
        if match and (await vote_page_confirmation(tab, match.group(1))).get("confirmed") is True:
            return AUTHENTICATED
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


async def public_topgg_login_ready(tab: Any) -> bool:
    """A usable public Login can start OAuth; it never establishes a session."""
    async def observe():
        script = "(() => {" + VOTE_CONTROL_JS + r"""
            if (location.protocol !== 'https:' || !['top.gg', 'www.top.gg'].includes(location.hostname) ||
                location.port || !/^\/bot\/[0-9]+\/vote\/?$/.test(location.pathname) ||
                !document.body || document.readyState === 'loading') return false;
            const body = document.body.innerText.toLowerCase();
            const loggedOut = ['must be logged in', 'login to vote', 'log in to vote'].some(text => body.includes(text));
            if (!loggedOut) return false;
            return [...document.querySelectorAll('a,button,[role="button"]')].some(node =>
                ['login', 'log in', 'sign in'].includes((node.textContent || '').trim().toLowerCase()) &&
                !voteControl.disabled(node) && voteControl.position(node).ready);
        })()"""
        if await evaluate(tab, script) is not True:
            return False
        return not await is_turnstile_present(tab)
    try:
        return await asyncio.wait_for(observe(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
    except Exception:
        return False


async def topgg_auth_state(
    tab: Any, *, allow_session_recovery: bool = True, allow_challenge_input: bool = True,
) -> str:
    await dismiss_privacy_overlay(tab)
    challenge_handled = False

    page_hint = await topgg_page_auth_hint(tab)
    if page_hint == AUTHENTICATED:
        print("  ✅ top.gg vote page shows an authenticated voting surface")
        return AUTHENTICATED

    # Do not call the Auth.js session endpoint through an active protection
    # challenge. That request is expected to produce a 403 and adds no useful
    # authentication signal. Clear the page challenge first, then probe only
    # if the vote surface is still ambiguous.
    if await is_turnstile_present(tab):
        if not allow_challenge_input:
            # The first cookie check already waited for a target/clearance on
            # this document. A recheck observes progress without repeating the
            # same solver attempt or another protected session fetch.
            for poll in range(AUTH_PAGE_SETTLE_POLLS):
                if not await is_turnstile_present(tab) and await document_ready(tab):
                    break
                if poll < AUTH_PAGE_SETTLE_POLLS - 1:
                    await asyncio.sleep(AUTH_PAGE_SETTLE_DELAY_SEC)
            else:
                return await unresolved_challenge_auth_state(tab)
        else:
            print("  → top.gg protection is active; clearing it before session validation")
            if not await solve_turnstile(tab):
                return await unresolved_challenge_auth_state(tab)
        challenge_handled = True
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
        if await public_topgg_login_ready(tab):
            # The protected fetch is not an authentication verdict. Preserve
            # this ordinary page for one real Login/OAuth attempt instead of
            # replacing it with a new navigation challenge.
            print("  → Session API blocked, but the public Login control is usable")
            return AUTH_BLOCKED
        if allow_session_recovery:
            # An API's HTML challenge is not rendered as an interactive page.
            # First observe the existing application, preserving its cookies.
            for recovery_poll in range(AUTH_RECOVERY_POLLS):
                page_hint = await topgg_page_auth_hint(tab)
                if page_hint == AUTHENTICATED:
                    print("  ✅ top.gg page became usable after the blocked session response")
                    return AUTHENTICATED
                if page_hint == AUTH_INVALID and await public_topgg_login_ready(tab):
                    return AUTH_BLOCKED
                if not challenge_handled and await is_turnstile_present(tab):
                    if not allow_challenge_input or not await solve_turnstile(tab):
                        return await unresolved_challenge_auth_state(tab)
                    return await topgg_auth_state(tab, allow_session_recovery=False)
                if probe.get("error") == "document-loading" and await document_ready(tab):
                    return await topgg_auth_state(tab, allow_session_recovery=False)
                if recovery_poll < AUTH_RECOVERY_POLLS - 1:
                    await asyncio.sleep(AUTH_RECOVERY_DELAY_SEC)
            # If only the fetch was challenged, make at most one ordinary page
            # navigation so the browser can render verification normally. Never
            # inject the response HTML or open an API/callback URL as a page.
            if probe.get("cf_mitigated") == "challenge":
                try:
                    url = urlparse(await current_url(tab))
                    if (url.scheme == "https" and url.hostname in {"top.gg", "www.top.gg"}
                            and re.fullmatch(r"/bot/[0-9]+/vote/?", url.path)
                            and await document_ready(tab) and not await is_turnstile_present(tab)):
                        print("  → Session fetch was challenged; reopening the current vote page once")
                        await asyncio.wait_for(tab.reload(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
                        await asyncio.sleep(2)
                        return await topgg_auth_state(tab, allow_session_recovery=False)
                except Exception:
                    # Diagnostic/recovery failure cannot turn denial into logout
                    # or expose exception details from an OAuth URL.
                    pass
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
    await navigate_page(tab, vote_url)
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
        state = await topgg_auth_state(tab) if attempt == 1 else await topgg_auth_state(
            tab, allow_session_recovery=False, allow_challenge_input=False)
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
            if await _click_exact_element(tab, "button", ["Authorize", "Authorise"], marker):
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

    public_page = await public_topgg_login_ready(tab)
    if not public_page:
        await navigate_page(tab, vote_url)
        await asyncio.sleep(2)
        await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Already logged into top.gg")
        return state
    if state == AUTH_CAPTCHA_REQUIRED or (state == AUTH_BLOCKED and not await public_topgg_login_ready(tab)):
        return state

    public_page = public_page or await public_topgg_login_ready(tab)
    print("  → Establishing Discord browser session...")
    discord_tab = tab
    if public_page:
        # Preserve the usable application document and its verification cookies
        # while the same profile establishes its Discord-origin session.
        with suppress(Exception):
            candidate = await asyncio.wait_for(tab.browser.get("about:blank", new_tab=True),
                timeout=BROWSER_COMMAND_TIMEOUT_SEC)
            if candidate is not None:
                discord_tab = candidate
    try:
        await navigate_page(discord_tab, DISCORD_LOGIN_URL)
        await asyncio.sleep(2)
        if not url_has_domain(await current_url(discord_tab), "discord.com"):
            print("  ❌ Discord login page did not open")
            return AUTH_INVALID

        await evaluate(discord_tab, f"""(() => {{
            const token = {json.dumps(token)};
            localStorage.setItem('token', JSON.stringify(token));
            localStorage.setItem('tokens', JSON.stringify({{"default": token}}));
        }})()""")
        await asyncio.wait_for(discord_tab.reload(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
        await asyncio.sleep(3)
    finally:
        if discord_tab is not tab:
            with suppress(Exception):
                await asyncio.wait_for(discord_tab.close(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)

    print("  → Navigating to top.gg to initiate OAuth...")
    if discord_tab is tab or not await public_topgg_login_ready(tab):
        await navigate_page(tab, vote_url)
        await asyncio.sleep(3)
    else:
        print("  → Continuing OAuth from the preserved public Login page")
    await settle_privacy_overlay(tab)
    state = await topgg_auth_state(tab)
    if state == AUTHENTICATED:
        print("  ✅ Session established before OAuth redirect")
        return state
    if state == AUTH_CAPTCHA_REQUIRED or (state == AUTH_BLOCKED and not await public_topgg_login_ready(tab)):
        return state

    marker = "data-auto-login"
    before = await evaluate(tab, "({url: location.href, epoch: performance.timeOrigin})")
    if not await _click_exact_element(tab, 'a,button,[role="button"]', ["Login", "Log in", "Sign in"], marker):
        print("  ❌ Could not click top.gg Login button")
        return AUTH_INVALID
    destination = await wait_for_oauth_start(tab, before)
    if destination is None:
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await unresolved_challenge_auth_state(tab)
        print("  ❌ Discord OAuth page did not open")
        return AUTH_INVALID
    destination_url = await current_url(tab)
    if destination == "topgg" or url_has_domain(destination_url, "top.gg"):
        # Existing Discord grants can pass through authorize faster than a
        # browser poll. The returned application still has to validate auth.
        await asyncio.sleep(3)
        await settle_privacy_overlay(tab)
        return await topgg_auth_state(tab)
    if "/oauth2/authorize" not in urlparse(destination_url).path:
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


async def wait_for_oauth_start(tab: Any, before: dict) -> str | None:
    """Observe either the Discord dialog or an already completed return."""
    deadline = asyncio.get_running_loop().time() + TIMEOUT_OAUTH_SEC
    before_epoch = request_diagnostics.number(before.get("epoch")) if isinstance(before, dict) else None

    async def observe():
        while asyncio.get_running_loop().time() < deadline:
            try:
                snapshot = await asyncio.wait_for(evaluate(tab, "({url: location.href, epoch: performance.timeOrigin})"),
                    timeout=min(2, max(0.001, deadline - asyncio.get_running_loop().time())))
                url = snapshot.get("url") if isinstance(snapshot, dict) else None
                parsed = urlparse(url) if isinstance(url, str) else None
                if (parsed is not None and parsed.scheme == "https" and parsed.port in {None, 443}
                        and not parsed.username and not parsed.password):
                    if parsed.hostname in {"discord.com", "www.discord.com"}:
                        return "discord"
                    if parsed.hostname in {"top.gg", "www.top.gg"} and not parsed.path.startswith("/api/auth/"):
                        epoch = request_diagnostics.number(snapshot.get("epoch"))
                        if (before_epoch is not None and epoch is not None and epoch > before_epoch
                                or await topgg_page_auth_hint(tab) == AUTHENTICATED):
                            return "topgg"
            except Exception:
                # Execution contexts can disappear during an ordinary redirect.
                pass
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        return None

    try:
        return await asyncio.wait_for(observe(), timeout=TIMEOUT_OAUTH_SEC)
    except asyncio.TimeoutError:
        return None


async def is_cloudflare_challenge_page(tab: Any) -> bool:
    tracker = getattr(tab, "_topgg_diagnostics", None)
    if getattr(tracker, "document_challenged", None) is True:
        return True
    result = await evaluate(tab, "(() => {" + CHALLENGE_JS + "return challengeState.managed; })()")
    return result is True

async def unresolved_challenge_auth_state(tab: Any) -> str:
    # The broad challenge detector also recognizes Cloudflare interstitials.
    # A timeout there does not establish that a manual CAPTCHA is required.
    if await is_cloudflare_challenge_page(tab):
        print("  ⏳ Cloudflare challenge page remains active; access is blocked")
        return AUTH_BLOCKED
    return AUTH_CAPTCHA_REQUIRED


async def is_turnstile_present(tab: Any) -> bool:
    tracker = getattr(tab, "_topgg_diagnostics", None)
    provider = "true" if getattr(tracker, "document_challenged", None) is True else "false"
    state = await evaluate(tab, "(() => {const providerMarked = " + provider + ";" + CHALLENGE_JS + "return challengeState.present; })()")
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
        try:
            text = (await asyncio.wait_for(body_text(tab), timeout=max(0.001, deadline - asyncio.get_running_loop().time()))).lower()
        except TimeoutError:
            break
        if "you will be able to vote after this ad" not in text:
            return None
        print("  → Ad playing, waiting for completion...")
        await asyncio.sleep(min(3, max(0, deadline - asyncio.get_running_loop().time())))
    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_ad_timeout.png")
    if path:
        await notify_error_screenshot(bot_id, path, "Ad countdown timeout")
    return {"bot_id": bot_id, "status": "error", "detail": "Ad countdown timeout"}


async def mark_vote_button(tab: Any) -> dict:
    return dict(await evaluate(tab, "(() => {" + VOTE_CONTROL_JS + """
        document.querySelectorAll('[data-auto-vote]').forEach(
            el => el.removeAttribute('data-auto-vote')
        );
        const candidates = voteControl.candidates();
        // Inspect every candidate using the same rules as the pointer. Retain
        // a fallback only to observe an unavailable control becoming ready.
        const button = candidates.find(el => voteControl.enabled(el) && voteControl.position(el, true).ready) ||
            candidates.find(voteControl.enabled) || candidates[0];
        if (!button) return {
            found: false, disabled: true, visible: false,
            target_kind: 'unknown', candidate_count: 0,
        };
        const disabled = voteControl.disabled(button);
        button.setAttribute('data-auto-vote', '1');
        return {
            found: true, disabled, visible: true,
            target_kind: button.tagName.toLowerCase() === 'button' ? 'native_button' : 'role_button',
            candidate_count: Math.min(candidates.length, 20),
        };
    })()""") or {})


def vote_target_diagnostic(state: dict) -> str:
    """Expose only fixed control categories and a bounded count from the DOM."""
    target_kind = state.get("target_kind")
    if not isinstance(target_kind, str) or target_kind not in {"native_button", "role_button"}:
        target_kind = "unknown"
    count = state.get("candidate_count")
    count = min(max(count, 0), 20) if type(count) is int else "unknown"
    return f"Vote target selected: kind={target_kind}, candidates={count}"


def vote_api_blocked_result(bot_id: str) -> dict:
    print("  ⏳ Vote API challenge persists; no Vote input sent")
    return {"bot_id": bot_id, "status": "blocked", "vote_submitted": False,
            "detail": "Vote API protection remained active before mouse input"}


async def vote_for_bot(
    tab: Any, bot_id: str, account_id: str = "unknown", *, allow_api_recovery: bool = True,
) -> dict:
    request_diagnostics.select_vote_bot(tab, bot_id)
    request_diagnostics.set_phase(tab, "vote_page")
    print(f"  → Voting for bot {bot_id}...")
    vote_url = f"https://top.gg/bot/{bot_id}/vote"

    async def blocked_before_input():
        network = request_diagnostics.vote_state(tab)
        if (allow_api_recovery and network is not None and not network.armed
                and is_topgg_vote_url(await current_url(tab), bot_id)):
            # A passive latch cannot recover if the app never repeats its read.
            # Reopen the ordinary page once, before any input, so its prerequisites
            # can run again. A committed document gets its own network context.
            print("  → Vote API preflight blocked; reopening the vote page once before input")
            await asyncio.wait_for(tab.reload(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
            await asyncio.sleep(3)
            return await vote_for_bot(tab, bot_id, account_id, allow_api_recovery=False)
        return vote_api_blocked_result(bot_id)

    if is_topgg_vote_url(await current_url(tab), bot_id):
        print("  → Reusing current top.gg vote page to preserve verified browser state")
    else:
        await navigate_page(tab, vote_url)
        await asyncio.sleep(3)
    await settle_privacy_overlay(tab)
    text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        dbg("top.gg session not applied yet; reloading once")
        await asyncio.wait_for(tab.reload(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
        await asyncio.sleep(3)
        await settle_privacy_overlay(tab)
        text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
    cooldown = await confirmed_cooldown(tab, bot_id, text)
    if cooldown is not None:
        return cooldown
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

    try:
        waited_for_api = await _wait_for_vote_api(tab)
    except VoteAPIBlocked:
        return await blocked_before_input()
    if waited_for_api:
        # Waiting can reveal an already registered vote or change page state.
        if not is_topgg_vote_url(await current_url(tab), bot_id):
            return {"bot_id": bot_id, "status": "error", "vote_submitted": False,
                    "detail": "Vote page changed while awaiting API readiness"}
        text = (await body_text(tab)).lower()
        cooldown = await confirmed_cooldown(tab, bot_id, text)
        if cooldown is not None:
            return cooldown

    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    state = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.wait_for(mark_vote_button(tab), timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
        if state.get("found") and not state.get("disabled"):
            break
        cooldown = await confirmed_cooldown(tab, bot_id, await body_text(tab))
        if cooldown is not None:
            return cooldown
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
        cooldown = await confirmed_cooldown(tab, bot_id, text)
        if cooldown is not None:
            return cooldown
        if "must be logged in" in text or "login to vote" in text or "log in to vote" in text:
            return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
        path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_no_btn.png")
        if path:
            await notify_error_screenshot(bot_id, path, "Vote button unavailable")
        detail = "Vote button disabled" if state.get("found") else "Vote button not found"
        return {"bot_id": bot_id, "status": "error", "detail": detail}

    before_click = await vote_page_confirmation(tab, bot_id)
    print(f"  → {vote_target_diagnostic(state)}")
    print("  → Preparing Vote mouse interaction...")
    request_diagnostics.set_phase(tab, "vote_input")
    try:
        if not await _click_marked(tab, "data-auto-vote"):
            return {
                "bot_id": bot_id, "status": "uncertain", "vote_submitted": True,
                "detail": "Vote mouse input sent; target click unconfirmed; automatic resubmission suppressed",
            }
        result = await verify_submitted_vote(tab, bot_id, account_id, before_click)
    except VoteAPIBlocked:
        return await blocked_before_input()
    except VoteClickNotReady as exc:
        print(f"  ⚠️ Vote control not actionable; no mouse press sent ({exc})")
        return {
            "bot_id": bot_id, "status": "error", "vote_submitted": False,
            "detail": "Vote control not actionable; no mouse press sent",
        }
    except Exception as exc:
        # The click may have reached the server even if its browser command or
        # the subsequent confirmation failed. Never blindly submit it again.
        dbg(f"Post-click confirmation failed: {type(exc).__name__}")
        result = {
            "bot_id": bot_id, "status": "uncertain",
            "detail": "Vote submitted; browser confirmation unavailable",
        }
    if result.get("status") not in COMPLETED_STATUSES and result.get("submission_rejected") is not True:
        result["vote_submitted"] = True
    return result


async def fresh_vote_document(tab: Any, bot_id: str, *, navigate: bool = False) -> bool:
    """Require a committed new document; old DOM after Page.reload is insufficient."""
    script = "({epoch: performance.timeOrigin, ready: ['interactive', 'complete'].includes(document.readyState), url: location.href})"
    before = await evaluate(tab, script)
    if not isinstance(before, dict) or request_diagnostics.number(before.get("epoch")) is None:
        return False
    if navigate:
        await navigate_page(tab, f"https://top.gg/bot/{bot_id}/vote")
    else:
        await asyncio.wait_for(tab.reload(), timeout=BROWSER_COMMAND_TIMEOUT_SEC)
    deadline = asyncio.get_running_loop().time() + BROWSER_COMMAND_TIMEOUT_SEC
    while asyncio.get_running_loop().time() < deadline:
        after = await asyncio.wait_for(evaluate(tab, script), timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
        if (isinstance(after, dict) and request_diagnostics.number(after.get("epoch")) is not None
                and after["epoch"] > before["epoch"] and after.get("ready") is True
                and is_topgg_vote_url(after.get("url", ""), bot_id)):
            return True
        await asyncio.sleep(0.25)
    return False


async def recover_prior_submission(tab: Any, bot_id: str, account_id: str) -> dict:
    print("  → Earlier Action sent possible Vote input; verifying before any new input")
    request_diagnostics.select_vote_bot(tab, bot_id)
    eligible_count, previous_evidence, challenge_cycles = 0, None, 0
    can_resubmit = False
    deadline = asyncio.get_running_loop().time() + RECOVERY_OBSERVE_TIMEOUT_SEC
    try:
        async with asyncio.timeout(RECOVERY_OBSERVE_TIMEOUT_SEC):
            if not await fresh_vote_document(tab, bot_id, navigate=True):
                return {"bot_id": bot_id, "status": "uncertain", "vote_submitted": True,
                        "detail": "Earlier Vote input retained; new document could not be verified"}
            await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)
            await settle_privacy_overlay(tab)
            # Keep this independent document alive while its own prerequisites
            # settle. Reopening it on each poll would restart the ad countdown.
            while asyncio.get_running_loop().time() < deadline:
                if await is_turnstile_present(tab):
                    eligible_count, previous_evidence = 0, None
                    challenge_cycles += 1
                    if (challenge_cycles > MAX_TURNSTILE_CYCLES_PER_PHASE
                            or not await solve_turnstile(tab)):
                        break
                    await settle_privacy_overlay(tab)
                if await wait_for_ad(tab, bot_id):
                    break
                snapshot = await persisted_vote_confirmation(tab, bot_id)
                evidence = snapshot.get("evidence") if snapshot.get("confirmed") is True else None
                if evidence is not None and evidence == previous_evidence:
                    text = await body_text(tab)
                    if page_indicates_cooldown(text):
                        return cooldown_result(bot_id, text)
                    return successful_vote_result(bot_id)
                previous_evidence = evidence
                network = request_diagnostics.vote_state(tab)
                eligible = (
                    snapshot.get("observed") is True and snapshot.get("vote_enabled") is True
                    and not any(snapshot.get(key) for key in ("challenge", "login_required", "error_present", "ad_pending"))
                    and (network is None or not network.protection_pending())
                )
                eligible_count = eligible_count + 1 if eligible else 0
                if eligible_count >= 2:
                    can_resubmit = True
                    break
                await asyncio.sleep(2)
    except TimeoutError:
        print("  → Earlier Vote verification window expired; retaining pending handoff")
    if can_resubmit:
        if RECOVERY_JOURNAL is not None:
            RECOVERY_JOURNAL.clear_current()
        # The observation deadline must not cancel an ordinary submission or
        # its confirmation after eligibility has been established.
        return await vote_for_bot(tab, bot_id, account_id)
    return {"bot_id": bot_id, "status": "uncertain", "vote_submitted": True,
            "detail": "Earlier Vote input remains unconfirmed; verification only, no new input"}


async def verify_submitted_vote(tab: Any, bot_id: str, account_id: str, before_click: dict) -> dict:
    request_diagnostics.set_phase(tab, "vote_confirmation")
    network = request_diagnostics.vote_state(tab)
    if network is not None and network.armed:
        # Give ordinary and late/CORS response events a short observation window.
        # Keep the existing five-second wait on healthy or ambiguous submissions.
        await asyncio.sleep(1)
        if network.definitely_rejected():
            return rejected_vote_result(bot_id)
        await asyncio.sleep(4)
    else:
        await asyncio.sleep(5)

    if network is not None and network.definitely_rejected():
        return rejected_vote_result(bot_id)

    async def acknowledged_with_coverage():
        current = request_diagnostics.vote_state(tab)
        if current is None or not current.confirmation_covered():
            return False
        if not await confirm_vote_without_reload(tab, bot_id, before_click):
            return False
        current = request_diagnostics.vote_state(tab)
        return current is not None and current.confirmation_covered() and not current.definitely_rejected()

    outcome = network.submission_outcome() if network is not None else None
    application_error = outcome in {"captcha_required", "unauthenticated", "error", "invalid", "protection_rejected"}
    if application_error:
        print(f"  ⚠️ GraphQL vote response reported {outcome}; a 200 is not vote confirmation")
    if not application_error and await acknowledged_with_coverage():
        result = successful_vote_result(bot_id)
        result["detail"] = "Vote acknowledged on page"
        return result

    outcome = network.submission_outcome() if network is not None else None
    challenged = await is_turnstile_present(tab)
    if outcome == "captcha_required" and not challenged:
        for _ in range(2):
            await asyncio.sleep(2)
            challenged = await is_turnstile_present(tab)
            if challenged:
                break
        if not challenged:
            return await captcha_result(tab, bot_id,
                "Vote API requires CAPTCHA; no interactive control became available", account_id)
    if challenged:
        if not await solve_turnstile(tab):
            return await unresolved_challenge_result(
                tab,
                bot_id,
                "CAPTCHA still required after solver attempt following Vote click",
                account_id,
                after_vote=True,
            )
        await asyncio.sleep(POST_VOTE_VERIFY_DELAY_SEC)

        outcome = network.submission_outcome() if network is not None else None
        if outcome not in {"captcha_required", "unauthenticated", "error", "invalid", "protection_rejected"} and await acknowledged_with_coverage():
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
        if not await fresh_vote_document(tab, bot_id):
            print("  ⚠️ New verification document was not observed")
            break
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


def rejected_vote_result(bot_id: str) -> dict:
    print("  ⏳ Exact Vote submission rejected by Cloudflare; fresh-browser retry is eligible")
    return {"bot_id": bot_id, "status": "blocked", "vote_submitted": False,
            "submission_rejected": True,
            "detail": "Exact vote request rejected by a Cloudflare challenge"}


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
        profile_path = tempfile.mkdtemp(prefix="auto-vote-topgg-")
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


class AccountBrowserSession:
    """A verified profile belongs to one account and one process_account call."""

    def __init__(self):
        self.browser = None
        self.tab = None
        self.authenticated = False
        self.diagnostics = None

    async def acquire(self, *, initial_url=None):
        if self.browser is None:
            self.browser = await start_browser(initial_url=initial_url) if initial_url else await start_browser()
            self.tab = next(iter(self.browser))
            self.diagnostics = request_diagnostics.RequestDiagnostics(self.tab)
            if not await self.diagnostics.start():
                await asyncio.sleep(0.25)
                await self.diagnostics.start()
        request_diagnostics.set_phase(self.tab, "authentication")
        return self.browser, self.tab

    async def reusable(self):
        if not self.authenticated or self.tab is None:
            return False
        try:
            for observation in range(2):
                url = urlparse(await current_url(self.tab))
                if (url.scheme != "https" or url.hostname not in {"top.gg", "www.top.gg"}
                        or not re.fullmatch(r"/bot/[0-9]+/vote/?", url.path)
                        or not await document_ready(self.tab)
                        or await topgg_page_auth_hint(self.tab) != AUTHENTICATED
                        or (await is_turnstile_present(self.tab) and not await is_turnstile_solved(self.tab))):
                    return False
                if observation == 0: await asyncio.sleep(0.5)
            return True
        except Exception:
            return False

    async def close(self):
        browser, self.browser = self.browser, None
        self.tab, self.authenticated = None, False
        if self.diagnostics:
            self.diagnostics.stop()
            self.diagnostics = None
        if browser is not None:
            await close_browser_safely(browser, "account attempt")
            await asyncio.sleep(1)


async def _run_account(
    token: str,
    bot_ids: list[str],
    account_id: str,
    account_cookies: list[dict] | None = None,
    *,
    capture_auth_failure: bool = False,
    session: AccountBrowserSession | None = None,
) -> list[dict]:
    owned_session = session is None
    session = session if session is not None else AccountBrowserSession()
    results = []
    try:
        browser, tab = await session.acquire(initial_url=f"https://top.gg/bot/{bot_ids[0]}/vote" if bot_ids else None)
        if bot_ids:
            request_diagnostics.select_vote_bot(tab, bot_ids[0])
        auth_state = AUTH_INVALID
        was_authenticated = session.authenticated
        session.authenticated = False
        if was_authenticated:
            print("  → Reusing authenticated browser for pending bots")
            auth_state = await topgg_auth_state(tab)
        elif account_cookies:
            auth_state = await login_with_cookies(tab, account_cookies, bot_ids)
        public_login = auth_state == AUTH_BLOCKED and await public_topgg_login_ready(tab)
        if auth_state == AUTH_INVALID and account_cookies:
            print("  → Cookie auth is invalid; falling back to Discord OAuth...")
            await clear_topgg_auth_cookies(browser)
        elif auth_state == AUTH_BLOCKED:
            if public_login:
                print("  → Public Login remains usable; trying ordinary Discord OAuth once")
            else:
                print("  ⏳ top.gg is blocking this browser; skipping OAuth on the same session")
        if auth_state == AUTH_INVALID or public_login:
            auth_state = await discord_oauth_login(tab, token, bot_ids)
        session.authenticated = auth_state == AUTHENTICATED
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
                record = RECOVERY_JOURNAL.select(token, bot_id) if RECOVERY_JOURNAL is not None else None
                if record and record["kind"] == "complete":
                    result = {"bot_id": bot_id, "status": "cooldown", "retry_at": record["until"],
                              "detail": "Confirmed vote retained from an earlier Action"}
                elif record and record["kind"] == "pending":
                    result = await recover_prior_submission(tab, bot_id, account_id)
                else:
                    result = await vote_for_bot(tab, bot_id, account_id)
                if RECOVERY_JOURNAL is not None:
                    RECOVERY_JOURNAL.record_result(result)
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
        failed_state = any(result.get("status") in {"blocked", "captcha_required", "auth_failed"}
                           or result.get("vote_submitted") is True for result in results)
        if owned_session or failed_state or not await session.reusable():
            await session.close()


async def _process_account_attempts(
    token: str,
    bot_ids: list[str],
    index: int,
    total: int,
    account_cookies: list[dict] | None = None,
    *,
    session: AccountBrowserSession,
) -> list[dict]:
    prefix = f"[{index}/{total}]"
    account_id = account_fingerprint(token)
    pending = list(bot_ids)
    results_by_bot: dict[str, dict] = {}
    if RECOVERY_JOURNAL is not None:
        for bot_id in bot_ids:
            record = RECOVERY_JOURNAL.select(token, bot_id)
            if record and record["kind"] == "complete":
                results_by_bot[bot_id] = {"bot_id": bot_id, "account_id": account_id, "status": "cooldown",
                                          "retry_at": record["until"], "detail": "Confirmed vote retained from an earlier Action"}
        pending = [bot_id for bot_id in pending if bot_id not in results_by_bot]
        if not pending:
            return [results_by_bot[bot_id] for bot_id in bot_ids]
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
                session=session,
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
                    print(f"{prefix} ↺ Protection block detected; trying the next browser ({attempt + 1}/{MAX_RETRIES})")
                    continue
                print(f"{prefix} ⏳ Protection block persists; ending this run")
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
                print(f"{prefix} ↺ Vote-page protection block; trying the next browser ({attempt + 1}/{MAX_RETRIES})")
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


async def process_account(
    token: str, bot_ids: list[str], index: int, total: int,
    account_cookies: list[dict] | None = None,
) -> list[dict]:
    session = AccountBrowserSession()
    try:
        return await _process_account_attempts(
            token, bot_ids, index, total, account_cookies, session=session,
        )
    finally:
        await session.close()


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


def publish_run_summary(all_results: list[list[dict]]) -> None:
    """GitHub fallback reporting with aggregate categories only, never account data."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    statuses = ("success", "cooldown", "blocked", "captcha_required", "uncertain", "auth_failed", "error", "unknown")
    counts = dict.fromkeys(statuses, 0)
    for results in all_results:
        for result in results:
            status = result.get("status")
            counts[status if isinstance(status, str) and status in counts else "unknown"] += 1
    lines = ["### Recorded vote outcomes", "", "| Outcome | Count |", "|---|---:|"]
    lines.extend(f"| {status} | {count} |" for status, count in counts.items() if count)
    lines.extend(["", "Success requires an observed page acknowledgement or persisted vote state. "
                  "A mouse click or HTTP 200 alone is not confirmation.",
                  "HTTP/GraphQL and challenge diagnostics are in the vote log. "
                  "The result-check job validates the script exit status; it does not contact Top.gg.", ""])
    try:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines))
    except OSError:
        # Reporting must never replace a completed vote with a retryable failure.
        print("  GitHub result summary unavailable; vote outcome unchanged")


async def main() -> int:
    global TG_BOT_TOKEN, TG_CHAT_ID, SENSITIVE_VALUES, RECOVERY_JOURNAL
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
    recovery_path = os.environ.get("RECOVERY_STATE_FILE", "")
    RECOVERY_JOURNAL = SubmissionJournal(recovery_path, tokens, bot_ids) if recovery_path else None
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
        print(f"   Legacy recovery depth: {run_recovery_depth} (no chain limit)")
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
        print("↺ Browser startup failure marker recorded")
    if write_protection_retry_state(all_results):
        print("↺ Protection-block failure marker recorded")
    report = build_notification(all_results, now)
    publish_run_summary(all_results)
    send_notification(report)
    await send_captcha_screenshots(all_results)
    await send_auth_failure_screenshots(all_results)
    return 1 if has_business_failure(all_results) else 0


if __name__ == "__main__":
    raise SystemExit(uc.loop().run_until_complete(main()))
