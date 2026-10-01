"""One native mouse interaction with a visible, stable application control."""

import asyncio
import json
from contextlib import suppress

from nodriver import cdp
import browser_environment
import native_mouse
from vote_controls import VOTE_CONTROL_JS


TARGET_SCRIPT = "(() => {" + VOTE_CONTROL_JS + """
    const state = window.__autoUiPointer || (window.__autoUiPointer = {});
    const blocked = reason => { state.since = null; return {ready: false, reason}; };
    if (document.readyState === 'loading') return blocked('loading');
    const el = document.querySelector(__SELECTOR__);
    if (!el || !el.isConnected) return blocked('missing');
    if (voteControl.disabled(el)) return blocked('disabled');
    const position = voteControl.position(el, true);
    if (!position.ready) return blocked(position.reason);
    const {x, y, rect} = position;
    const geometry = [rect.left, rect.top, rect.width, rect.height];
    if (state.element !== el || !state.geometry || state.since == null ||
        geometry.some((value, i) => Math.abs(value - state.geometry[i]) > 1)) state.since = performance.now();
    state.element = el; state.geometry = geometry;
    if (performance.now() - state.since < 500) return {ready: false, reason: 'settling'};
    if (__ARM__) {
        state.receipt = {pressed: false, released: false, clicked: false};
        state.listeners = ['pointerdown', 'pointerup', 'click'].map(type => {
            const handler = event => {
                if (event.isTrusted && event.composedPath().includes(el)) {
                    state.receipt[{pointerdown: 'pressed', pointerup: 'released', click: 'clicked'}[type]] = true;
                }
            };
            window.addEventListener(type, handler, true);
            return [type, handler];
        });
    }
    return {ready: true, x, y};
})()"""

CLEAN_SCRIPT = """(() => {
    const state = window.__autoUiPointer;
    for (const [type, handler] of state?.listeners || []) window.removeEventListener(type, handler, true);
    delete window.__autoUiPointer;
})()"""


async def click_control(tab, evaluate, selector: str, *, kind="control", timeout=8, reacquire=None) -> dict:
    """Input receipt is separate from the caller's redirect/dismissal checks."""
    kind = kind if kind in {"login", "oauth", "consent"} else "control"
    sent = False
    flags = {key: None for key in ("pressed", "released", "clicked")}
    reason = "unavailable"
    deadline = asyncio.get_running_loop().time() + timeout

    async def target(arm=False):
        try:
            if reacquire is not None and not await reacquire():
                return {"ready": False, "reason": "missing"}
            script = TARGET_SCRIPT.replace("__SELECTOR__", json.dumps(selector)).replace("__ARM__", json.dumps(arm))
            result = await asyncio.wait_for(evaluate(tab, script), timeout=2)
            return result if isinstance(result, dict) else {"ready": False, "reason": "unavailable"}
        except Exception:
            return {"ready": False, "reason": "unavailable"}

    async def send(command):
        return await asyncio.wait_for(tab.send(command), timeout=2)

    async def prepare():
        nonlocal reason
        activated = False
        while asyncio.get_running_loop().time() < deadline:
            position = await target()
            if position.get("ready") is True:
                if not activated:
                    await browser_environment.foreground(tab)
                    activated = True
                await send(cdp.input_.dispatch_mouse_event("mouseMoved", x=position["x"], y=position["y"], buttons=0))
                await asyncio.sleep(0.25)
                position = await target(arm=True)
                if position.get("ready") is True:
                    return position
            reason = position.get("reason", "unavailable")
            await asyncio.sleep(0.25)
        return None

    try:
        position = await asyncio.wait_for(prepare(), timeout=timeout)
        if position is None:
            return {"input_sent": False, "clicked": False}
        sent = True
        await native_mouse.press_and_release(tab, position["x"], position["y"])
        with suppress(Exception):
            receipt = await asyncio.wait_for(evaluate(tab, "window.__autoUiPointer?.receipt || null"), timeout=2)
            if isinstance(receipt, dict):
                flags = {key: receipt.get(key) is True for key in flags}
    except Exception:
        # Navigation can destroy the event observer after a successful press.
        # The caller must still observe the expected application outcome.
        pass
    finally:
        with suppress(Exception):
            await asyncio.wait_for(evaluate(tab, CLEAN_SCRIPT), timeout=2)
        safe_reason = reason if reason in {
            "loading", "missing", "disabled", "hidden", "offscreen", "covered", "settling",
        } else "unavailable"
        print("  → Application mouse input: " + json.dumps({
            "kind": kind, "input_sent": sent, "trusted_events": flags,
            "reason": "sent" if sent else safe_reason,
        }, sort_keys=True))
    return {"input_sent": sent, "clicked": flags["clicked"] if sent else False}
