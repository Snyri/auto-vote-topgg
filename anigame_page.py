"""AniGame's observed vote UI and public, read-only session endpoints."""

import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from page_signals import CHALLENGE_JS
from vote_controls import VOTE_CONTROL_JS


VOTE_URL = "https://www.anigame.gg/vote"
BOT_ID = "571027211407196161"
RECOVERY_NAMESPACE = VOTE_URL + ":v1"
HOSTS = frozenset({"anigame.gg", "www.anigame.gg"})
SAFETY_SECONDS = 30
MAX_COOLDOWN = 24 * 3600


def page_kind(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in HOSTS
                or parsed.port not in {None, 443} or parsed.username or parsed.password):
            return "external"
        return {"/vote": "vote", "/vote/confirmation": "confirmation"}.get(parsed.path.rstrip("/"), "other")
    except (TypeError, ValueError):
        return "external"


def cooldown_seconds(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    value = value.strip().lower()
    parts = re.findall(r"(\d+)\s*(h|m|s)\b", value)
    remainder = re.sub(r"(\d+)\s*(h|m|s)\b", "", value).strip()
    if not parts or remainder or len({unit for _, unit in parts}) != len(parts):
        return None
    seconds = sum(int(amount) * {"h": 3600, "m": 60, "s": 1}[unit] for amount, unit in parts)
    return seconds if 0 < seconds <= MAX_COOLDOWN else None


def eligible_at(value, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        epoch = parsed.timestamp()
        if not math.isfinite(epoch) or not now < epoch <= now + MAX_COOLDOWN:
            return None
        return math.ceil(epoch) + SAFETY_SECONDS
    except (ValueError, OverflowError, OSError):
        return None


def availability(raw, now=None):
    if not isinstance(raw, dict) or raw.get("status") != 200:
        return {"available": None, "retry_at": None}
    vote = raw.get("vote")
    if not isinstance(vote, dict) or type(vote.get("available")) is not bool:
        return {"available": None, "retry_at": None}
    return {"available": vote["available"],
            "retry_at": eligible_at(vote.get("nextEligibleAt"), now) if not vote["available"] else None}


SNAPSHOT_JS = "(() => {" + CHALLENGE_JS + r"""
    const text = node => (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
    const root = document.querySelector('main') || document.body;
    if (!root) return {url:location.href, ready:false};
    const body = text(root);
    const controls = [...document.querySelectorAll('button, a[role="button"], input[type="submit"], [role="button"]')];
    const freeVote = el => [el.getAttribute('aria-label'), text(el), el.value].some(value =>
        /^vote for anigame$/i.test((value || '').trim().replace(/\s*[\u2192\u2197\u203a\u00bb]+$/, '').trim()));
    const matches = controls.filter(el => freeVote(el) && voteControl.visible(el));
    const candidate = matches.find(el => !voteControl.disabled(el)) || matches[0];
    const headings = [...root.querySelectorAll('h1,h2,h3')].filter(voteControl.visible).map(el => text(el).toLowerCase());
    const login = controls.some(el => /^(?:sign in with discord|login with discord)$/i.test(text(el)) && voteControl.visible(el));
    const waiting = body.match(/please wait or explore this page while we register your vote:\s*(\d+)s/i);
    const cooldown = body.match(/your next free vote is\s+((?:\d+\s*[hms]\s*){1,3})\.?/i);
    const alerts = [...root.querySelectorAll('[role="alert"]')].filter(voteControl.visible);
    return {
        url: location.href, ready: ['interactive','complete'].includes(document.readyState),
        visible: document.visibilityState === 'visible',
        login, challenge: challengeState.present, managed: challengeState.managed,
        vote_present: !!candidate, vote_enabled: !!candidate && !voteControl.disabled(candidate),
        registering: !!waiting || controls.some(el => /^registering vote[.\u2026]*$/i.test(text(el))),
        countdown: waiting ? Number(waiting[1]) : null,
        cooldown: cooldown ? cooldown[1].trim() : null,
        confirmed: headings.includes('vote confirmed') && headings.includes('your vote rewards') &&
            /your rewards have been added to your game account/i.test(body),
        error: alerts.some(el => /failed|error|unable|could not|try again|forbidden|unavailable/i.test(text(el)))
    };
})()"""


MARK_VOTE_JS = "(() => {" + VOTE_CONTROL_JS + r"""
    document.querySelectorAll('[data-anigame-vote]').forEach(el => el.removeAttribute('data-anigame-vote'));
    if (!['anigame.gg','www.anigame.gg'].includes(location.hostname) ||
        location.protocol !== 'https:' || location.pathname.replace(/\/+$/, '') !== '/vote') return false;
    const controls = [...document.querySelectorAll('button,a[role="button"],input[type="submit"],[role="button"]')];
    const freeVote = el => [el.getAttribute('aria-label'), el.innerText, el.textContent, el.value].some(value =>
        /^vote for anigame$/i.test((value || '').replace(/\s+/g,' ').trim().replace(/\s*[\u2192\u2197\u203a\u00bb]+$/, '').trim()));
    const candidates = controls.filter(el => freeVote(el) && voteControl.visible(el) && !voteControl.disabled(el));
    const selected = candidates.find(el => voteControl.position(el, true).ready) || candidates[0];
    if (!selected) return false;
    selected.setAttribute('data-anigame-vote', '1');
    return true;
})()"""


async def snapshot(tab, evaluate):
    raw = await evaluate(tab, SNAPSHOT_JS)
    if not isinstance(raw, dict):
        return {"kind": "unknown", "ready": False}
    return {
        "kind": page_kind(raw.get("url", "")),
        **{key: raw.get(key) is True for key in (
            "ready", "visible", "login", "challenge", "managed", "vote_present",
            "vote_enabled", "registering", "confirmed", "error")},
        "countdown": raw.get("countdown") if type(raw.get("countdown")) is int and 0 <= raw["countdown"] <= 120 else None,
        "cooldown_seconds": cooldown_seconds(raw.get("cooldown")),
    }


async def read_endpoint(tab, evaluate, endpoint):
    if endpoint not in {"/api/auth/me", "/api/vote/availability"}:
        raise ValueError("Unsupported AniGame read endpoint")
    # Return only the fields used by this client; never return cookies, bearer
    # credentials, profile balances, email, raw response bodies or error text.
    script = r"""(async () => {
        if (location.protocol !== 'https:' || !['anigame.gg','www.anigame.gg'].includes(location.hostname))
            return {status: null};
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(__ENDPOINT__, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
            const type = response.headers.get('content-type') || '';
            if (!type.includes('json')) return {status:response.status};
            const data = await response.json();
            if (__ENDPOINT__ === '/api/auth/me') {
                const id = data?.user?.id;
                return {status:response.status, authenticated: !!data?.user && id !== undefined && id !== null,
                    user_id: typeof id === 'string' ? id : null};
            }
            return {status:response.status, vote: data?.vote ? {
                available:data.vote.available, nextEligibleAt:data.vote.nextEligibleAt
            } : null};
        } catch (_) { return {status:null}; }
        finally { clearTimeout(timer); }
    })()""".replace("__ENDPOINT__", json.dumps(endpoint))
    result = await evaluate(tab, script, timeout=12)
    return result if isinstance(result, dict) else {"status": None}
