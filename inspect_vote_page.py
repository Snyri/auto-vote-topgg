#!/usr/bin/env python3
"""Inspect one cookie session without voting, OAuth, CAPTCHA solving or reports."""

import asyncio
import json
import time

import vote


PATH_KINDS = {"same_bot_vote", "same_bot_page", "login", "other_topgg", "external"}
HREF_KINDS = PATH_KINDS | {"none", "discord_oauth", "fragment"}
FLAGS = ("strong_success", "cooldown", "login_required", "ad", "challenge")


def enum(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


def sanitize_snapshot(raw):
    """Never forward arbitrary browser text, attribute values or extra keys."""
    if not isinstance(raw, dict):
        return {"observed": False}
    result = {
        "observed": True,
        "title_class": enum(raw.get("title_class"), {"normal", "challenge"}),
        "current_path": enum(raw.get("current_path"), PATH_KINDS),
        "ready_state": enum(raw.get("ready_state"), {"loading", "interactive", "complete"}),
    }
    for key in FLAGS:
        result[key] = raw.get(key) if isinstance(raw.get(key), bool) else None
    seconds = raw.get("cooldown_seconds")
    result["cooldown_seconds"] = seconds if type(seconds) is int and 0 <= seconds <= 86400 else None
    result["controls"] = []
    controls = raw.get("controls")
    for control in (controls[:12] if isinstance(controls, list) else []):
        if not isinstance(control, dict):
            continue
        result["controls"].append({
            "label": enum(control.get("label"), {"vote", "login"}),
            "tag": enum(control.get("tag"), {"button", "a", "input", "other"}),
            "role_button": control.get("role_button") if isinstance(control.get("role_button"), bool) else None,
            "disabled": control.get("disabled") if isinstance(control.get("disabled"), bool) else None,
            "href_kind": enum(control.get("href_kind"), HREF_KINDS),
        })
    return result


async def page_snapshot(tab, bot_id):
    script = r"""(() => {
        const bot = __BOT_ID__;
        const body = (document.body ? document.body.innerText : '').toLowerCase();
        const title = (document.title || '').toLowerCase();
        const visible = node => {
            if (!(node.getClientRects().length || node.offsetWidth || node.offsetHeight)) return false;
            const style = getComputedStyle(node);
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                style.visibility !== 'collapse' && style.opacity !== '0';
        };
        const pathKind = url => {
            if (url.protocol !== 'https:' || !['top.gg', 'www.top.gg'].includes(url.hostname)) return 'external';
            const path = url.pathname.replace(/\/+$/, '');
            if (path === '/bot/' + bot + '/vote') return 'same_bot_vote';
            if (path === '/bot/' + bot) return 'same_bot_page';
            if (/^\/(?:login|signin|api\/auth)(?:\/|$)/.test(path)) return 'login';
            return 'other_topgg';
        };
        const hrefKind = node => {
            const href = node.getAttribute('href');
            if (!href) return 'none';
            if (href.startsWith('#')) return 'fragment';
            try {
                const url = new URL(href, location.href);
                if (url.protocol === 'https:' && ['discord.com', 'www.discord.com'].includes(url.hostname)
                    && url.pathname.startsWith('/oauth2/')) return 'discord_oauth';
                return pathKind(url);
            } catch (_) { return 'external'; }
        };
        const controls = [...document.querySelectorAll('button, a, input, [role="button"]')]
            .filter(visible).map(node => ({node, label: (node.textContent || node.value || '').trim().toLowerCase()}))
            .filter(item => ['vote', 'login', 'log in', 'sign in'].includes(item.label)).slice(0, 12)
            .map(({node, label}) => ({
                label: label === 'vote' ? 'vote' : 'login',
                tag: ['button', 'a', 'input'].includes(node.tagName.toLowerCase()) ? node.tagName.toLowerCase() : 'other',
                role_button: node.getAttribute('role') === 'button',
                disabled: Boolean(node.disabled || node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true'),
                href_kind: hrefKind(node),
            }));
        const titleChallenge = title.startsWith('just a moment') || title.startsWith('attention required');
        const challengeNodes = [...document.querySelectorAll('#challenge-form, #challenge-running, #challenge-stage, iframe[src*="challenges.cloudflare.com"], iframe[src*="hcaptcha.com"], iframe[src*="recaptcha"], .cf-turnstile, .h-captcha, .g-recaptcha')];
        const duration = body.match(/(?:you\s+)?can\s+vote\s+again\s+in\s+(?:about\s+|approximately\s+)?((?:(?:\d+(?:\.\d+)?|a|an|one)\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\s*(?:,\s*|and\s+|\s+)?){1,4})/i);
        let cooldownSeconds = null;
        if (duration) {
            let total = 0;
            for (const part of duration[1].matchAll(/(\d+(?:\.\d+)?|a|an|one)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)/gi)) {
                const amount = ['a', 'an', 'one'].includes(part[1]) ? 1 : Number(part[1]);
                const unit = part[2][0];
                total += amount * ({s: 1, m: 60, h: 3600, d: 86400}[unit]);
            }
            if (total > 0 && total <= 86400) cooldownSeconds = Math.ceil(total);
        }
        return {
            title_class: titleChallenge ? 'challenge' : 'normal',
            current_path: pathKind(new URL(location.href)), ready_state: document.readyState,
            strong_success: body.includes('thanks for voting') || body.includes('you have already voted'),
            cooldown: cooldownSeconds !== null || body.includes('already voted'), cooldown_seconds: cooldownSeconds,
            login_required: body.includes('must be logged in') || body.includes('login to vote') || body.includes('log in to vote'),
            ad: body.includes('you will be able to vote after this ad'),
            challenge: titleChallenge || challengeNodes.some(visible) || body.includes('verify you are human') ||
                body.includes('performing security verification') || body.includes('needs to review the security of your connection') ||
                body.includes('please solve the captcha') || body.includes('complete the captcha'),
            controls,
        };
    })()""".replace("__BOT_ID__", json.dumps(bot_id))
    try:
        return sanitize_snapshot(await asyncio.wait_for(vote.evaluate(tab, script), timeout=2))
    except Exception as exc:
        print("Page snapshot unavailable: " + type(exc).__name__)
        return {"observed": False}


async def main():
    browser = None
    vote.DEBUG = False
    vote.SEND_ERROR_SCREENSHOTS = False
    vote.TG_BOT_TOKEN = vote.TG_CHAT_ID = ""
    # Consent dismissal may otherwise invoke its screenshot/report fallback.
    vote.PRIVACY_DISMISS_REPORTED = True
    try:
        try:
            raw = vote.consume_secret("TOPGG_COOKIES_JSON")
        finally:
            vote.scrub_browser_environment()
        vote.SENSITIVE_VALUES = [raw]
        all_cookies = vote.load_topgg_cookies(raw=raw)
        cookies = next((item for item in all_cookies if item), None)
        if not cookies:
            raise ValueError("A configured cookie session is required")
        vote.SENSITIVE_VALUES.extend(str(cookie["value"]) for cookie in cookies)
        print("Cookie inspection: " + json.dumps({
            "cookie_count": min(len(cookies), 100),
            "expired_cookie_present": any(
                isinstance(cookie.get("expires"), (int, float))
                and not isinstance(cookie.get("expires"), bool)
                and 0 < cookie["expires"] <= time.time()
                for cookie in cookies
            ),
        }, sort_keys=True))
        bot_id = vote.load_bot_ids()[0]
        browser = await vote.start_browser()
        tab = next(iter(browser))
        await vote.inject_topgg_cookies(browser, cookies)
        await asyncio.wait_for(tab.get(f"https://top.gg/bot/{bot_id}/vote"), timeout=30)
        for observation, delay in enumerate((3, 10, 20), 1):
            await asyncio.sleep(delay)
            try:
                await asyncio.wait_for(vote.settle_privacy_overlay(tab), timeout=6)
            except Exception as exc:
                print("Consent observation unavailable: " + type(exc).__name__)
            snapshot = await page_snapshot(tab, bot_id)
            try:
                hint = await asyncio.wait_for(vote.topgg_page_auth_hint(tab), timeout=2)
            except Exception:
                hint = "unknown"
            try:
                confirmation = await vote.vote_page_confirmation(tab, bot_id)
            except Exception:
                confirmation = {}
            confirmation = confirmation if isinstance(confirmation, dict) else {}
            snapshot.update({
                "observation": observation,
                "auth_hint": enum(hint, {vote.AUTHENTICATED, vote.AUTH_INVALID}),
                "page_confirmed": confirmation.get("confirmed") is True,
                "page_evidence": enum(confirmation.get("evidence"), {"thanks for voting", "you have already voted", "bounded cooldown"}, None),
            })
            print("Page inspection: " + json.dumps(snapshot, sort_keys=True))
        try:
            probe = await vote.topgg_session_probe(tab)
            probe = probe if isinstance(probe, dict) else {}
            status = probe.get("status")
            print("Session inspection: " + json.dumps({
                "authenticated": probe.get("authenticated") is True,
                "status": status if type(status) is int and 0 <= status <= 599 else None,
                "json_ok": probe.get("json_ok") is True,
            }, sort_keys=True))
        except Exception as exc:
            print("Session inspection unavailable: " + type(exc).__name__)
        return 0
    except Exception as exc:
        print("Inspection failed: " + type(exc).__name__)
        return 1
    finally:
        if browser is not None:
            try:
                await vote.close_browser(browser)
            except Exception as exc:
                print("Inspection cleanup failed: " + type(exc).__name__)
        vote.SENSITIVE_VALUES = []


if __name__ == "__main__":
    raise SystemExit(vote.uc.loop().run_until_complete(main()))
