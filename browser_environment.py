"""Normal Chrome configuration, native foreground activation and safe facts."""

import asyncio
import json

import nodriver as uc


class ChromeConfig(uc.Config):
    """Keep Chrome's site isolation rather than nodriver's compatibility override."""

    def __call__(self):
        result = []
        for arg in super().__call__():
            if arg.startswith("--disable-features="):
                features = [name for name in arg.split("=", 1)[1].split(",")
                            if name not in {"IsolateOrigins", "site-per-process"}]
                if features:
                    result.append("--disable-features=" + ",".join(features))
            else:
                result.append(arg)
        return result


async def foreground(tab):
    """Ask Chrome to activate the real tab; inability to activate is not a gate."""
    try:
        await asyncio.wait_for(tab.send(uc.cdp.page.bring_to_front()), timeout=2)
    except Exception:
        pass


FACTS_SCRIPT = """(() => {
    const context = type => {
        try { return Boolean(document.createElement('canvas').getContext(type)); }
        catch (_) { return false; }
    };
    return {focused: document.hasFocus(), visibility: document.visibilityState,
        canvas: context('2d'), webgl: context('webgl'), webgl2: context('webgl2')};
})()"""


async def log_facts(tab, evaluate, phase):
    """Observation only: no browser overrides, raw text, URLs or credential values."""
    safe_phase = phase if phase in {"startup", "challenge_detected", "challenge_timeout", "inspection"} else "unknown"
    try:
        raw = await asyncio.wait_for(evaluate(tab, FACTS_SCRIPT), timeout=2)
        raw = raw if isinstance(raw, dict) else {}
        result = {"phase": safe_phase}
        for key in ("focused", "canvas", "webgl", "webgl2"):
            result[key] = raw.get(key) if type(raw.get(key)) is bool else None
        result["visibility"] = raw.get("visibility") if raw.get("visibility") in {"visible", "hidden"} else "unknown"
        print("  Browser environment: " + json.dumps(result, sort_keys=True))
    except Exception:
        print("  Browser environment unavailable (phase=" + safe_phase + ")")
