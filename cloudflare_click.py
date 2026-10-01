"""Conservative Cloudflare checkbox targeting; input is separate from clearance."""

import asyncio
import base64
import json
import math
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from functools import lru_cache
from urllib.parse import urlparse

from nodriver import cdp
from nodriver.core.util import get_cf_template
from nodriver.core.connection import Connection


MATCH_THRESHOLD = 0.85
TARGET_WAIT_SEC = 15
TARGET_STABLE_SEC = 0.5
POLL_SEC = 0.25


@lru_cache(maxsize=1)
def _template_gray():
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(get_cf_template(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def match_checkbox(image_png: bytes, width: float, height: float) -> dict:
    """Reject weak matches and map screenshot pixels to viewport CSS pixels."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(image_png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or not (0 < width <= 4096 and 0 < height <= 4096):
        return {"ready": False, "reason": "invalid_image"}
    pixel_height, pixel_width = image.shape[:2]
    scale_x, scale_y = pixel_width / width, pixel_height / height
    if not (0.5 <= scale_x <= 4 and abs(scale_x - scale_y) <= 0.02):
        return {"ready": False, "reason": "viewport_changed"}
    template = _template_gray()
    if template is None:
        return {"ready": False, "reason": "invalid_image"}
    # Screenshots can be device pixels while Input.dispatchMouseEvent uses CSS pixels.
    template = cv2.resize(template, None, fx=scale_x, fy=scale_y, interpolation=cv2.INTER_LINEAR)
    th, tw = template.shape[:2]
    if th > pixel_height or tw > pixel_width:
        return {"ready": False, "reason": "missing"}
    scores = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (left, top) = cv2.minMaxLoc(scores)
    if not math.isfinite(score) or score < MATCH_THRESHOLD:
        return {"ready": False, "reason": "weak_match"}
    return {
        "ready": True, "score": round(score, 3),
        "x": (left + tw / 2) / scale_x, "y": (top + th / 2) / scale_y,
    }


def _cloudflare_frame(node) -> bool:
    if node.node_name != "IFRAME":
        return False
    attrs = dict(zip((node.attributes or [])[::2], (node.attributes or [])[1::2]))
    parsed = urlparse(attrs.get("src", ""))
    return parsed.scheme == "https" and parsed.hostname == "challenges.cloudflare.com"


async def _send(tab, command, timeout=2):
    return await asyncio.wait_for(tab.send(command), timeout=timeout)


async def _on_node(tab, object_id, function):
    result, exception = await _send(tab, cdp.runtime.call_function_on(
        function, object_id=object_id, return_by_value=True,
    ))
    if exception:
        raise RuntimeError("Checkbox observation unavailable")
    return result.value if result else None


@asynccontextmanager
async def _provider_session(tab, frame_id):
    """An OOPIF has its own CDP session; never reattach the observed main tab."""
    owner, _ = await _send(tab, cdp.dom.get_frame_owner(frame_id))
    node = await _send(tab, cdp.dom.describe_node(backend_node_id=owner, depth=0))
    if not _cloudflare_frame(node):
        raise RuntimeError("Provider frame unavailable")
    infos = await _send(tab, cdp.target.get_targets())
    providers = [info for info in infos if info.type_ == "iframe"
        and urlparse(info.url).scheme == "https"
        and urlparse(info.url).hostname == "challenges.cloudflare.com"]
    if len(providers) > 4:
        raise RuntimeError("Ambiguous provider frames")
    for info in providers:
        connection = Connection(target=info, parent=tab, auto_attach=False)
        try:
            await asyncio.wait_for(connection.attach(), timeout=2)
            tree = await _send(connection, cdp.page.get_frame_tree())
            # Frame IDs and DevTools Target IDs are different identifiers.
            # Match the actual root frame exposed by the attached session.
            if tree.frame.id_ != frame_id:
                continue
            yield connection, owner
            return
        finally:
            with suppress(Exception):
                await asyncio.wait_for(connection.aclose(), timeout=2)
    raise RuntimeError("Provider frame unavailable")


async def interstitial_context(tab) -> bool:
    """A provider-marked current document also covers localized challenge titles."""
    tracker = getattr(tab, "_topgg_diagnostics", None)
    marked = getattr(tracker, "document_challenged", None) is True
    expression = """(() => {
        const managed = document.title.toLowerCase().startsWith('just a moment') ||
            Boolean(document.querySelector('#challenge-stage, #challenge-running, #challenge-form')) || __MARKED__;
        const usable = [...document.querySelectorAll('button, [role="button"]')].some(el =>
            el.textContent.trim().toLowerCase() === 'vote' && !el.disabled &&
            el.getAttribute('aria-disabled') !== 'true' && el.getClientRects().length);
        const body = (document.body?.innerText || '').toLowerCase();
        const app = body.includes('must be logged in') || body.includes('thanks for voting') ||
            body.includes('you will be able to vote after this ad') || body.includes('vote again in') ||
            body.includes('already voted');
        return managed && !usable && !app;
    })()""".replace("__MARKED__", "true" if marked else "false")
    title, error = await _send(tab, cdp.runtime.evaluate(expression=expression, return_by_value=True))
    return not error and title is not None and title.value is True


async def widget_state(tab) -> dict:
    """Observe provider widgets and response booleans in closed shadow DOM too."""
    state = {"present": False, "solved": False}
    try:
        document = await _send(tab, cdp.dom.get_document(depth=-1, pierce=True))
        if not isinstance(document, cdp.dom.Node):
            return state
        pending, visited = [document], 0
        while pending:
            node = pending.pop()
            visited += 1
            if visited > 4096:
                return state
            attrs = dict(zip((node.attributes or [])[::2], (node.attributes or [])[1::2]))
            if _cloudflare_frame(node):
                remote = None
                with suppress(Exception):
                    model = await _send(tab, cdp.dom.get_box_model(backend_node_id=node.backend_node_id))
                    if model.width > 0 and model.height > 0:
                        try:
                            remote = await _send(tab, cdp.dom.resolve_node(backend_node_id=node.backend_node_id))
                            visible = await _on_node(tab, remote.object_id, """function () {
                                for (let node = this; node; node = node.parentElement || node.getRootNode()?.host) {
                                    const style = getComputedStyle(node);
                                    if (style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
                                }
                                return true;
                            }""")
                            state["present"] |= visible is True
                        finally:
                            if remote:
                                with suppress(Exception):
                                    await _send(tab, cdp.runtime.release_object(remote.object_id))
            if node.node_name in {"INPUT", "TEXTAREA"} and attrs.get("name") in {
                "cf-turnstile-response", "cf_challenge_response", "g-recaptcha-response", "h-captcha-response",
            }:
                remote = None
                try:
                    remote = await _send(tab, cdp.dom.resolve_node(backend_node_id=node.backend_node_id))
                    solved = await _on_node(tab, remote.object_id,
                        "function () { return Boolean(this.value && this.value.length > 10); }")
                    state["solved"] |= solved is True
                finally:
                    if remote:
                        with suppress(Exception):
                            await _send(tab, cdp.runtime.release_object(remote.object_id))
            pending.extend((node.children or []) + (node.shadow_roots or []))
            if node.content_document:
                pending.append(node.content_document)
    except Exception:
        pass
    return state


CONTROL_FUNCTION = """function () {
    const hit = this.nodeType === 1 ? this : this.parentElement;
    const el = hit?.matches('input[type="checkbox"], [role="checkbox"]') ? hit :
        hit?.closest('label')?.control || hit?.closest('[role="checkbox"]');
    if (!el?.matches('input[type="checkbox"], [role="checkbox"]') || !el.isConnected) return {control: false};
    // A styled checkbox can hide its input and expose a clickable label/span.
    // Validate the visible hit element while checking the associated control.
    const style = hit.ownerDocument.defaultView.getComputedStyle(hit);
    const rect = hit.getBoundingClientRect();
    const enabled = !el.matches(':disabled') && !el.closest('[inert], [aria-disabled="true"]') &&
        !el.checked && el.getAttribute('aria-checked') !== 'true';
    return {control: true, enabled,
        visible: rect.width > 0 && rect.height > 0 && style.visibility === 'visible' &&
            style.display !== 'none' && Number(style.opacity) !== 0 && style.pointerEvents !== 'none'};
}"""


async def _hit_target(tab, x, y) -> dict:
    # CDP hit testing sees closed shadow roots too. A strong image match on
    # unrelated page content or beneath an overlay must never receive input.
    backend, frame_id, _ = await _send(tab, cdp.dom.get_node_for_location(
        x=round(x), y=round(y), include_user_agent_shadow_dom=True,
        ignore_pointer_events_none=False,
    ))
    try:
        node = await _send(tab, cdp.dom.describe_node(backend_node_id=backend, depth=0))
    except Exception:
        # An out-of-process frame may expose its hit node only to its own CDP
        # session. Verify the frame owner in the parent instead; do not claim
        # that input was observed inside that frame.
        owner, _ = await _send(tab, cdp.dom.get_frame_owner(frame_id))
        node = await _send(tab, cdp.dom.describe_node(backend_node_id=owner, depth=0))
        if _cloudflare_frame(node):
            return {"ready": True, "backend": owner, "target": "cloudflare_frame"}
        return {"ready": False, "reason": "unrelated_target"}
    frame_only = _cloudflare_frame(node)
    if frame_only:
        return {"ready": True, "backend": backend, "target": "cloudflare_frame"}

    # When CDP exposes a node inside a frame, verify its owner before treating
    # it as a challenge control. Do not accept a checkbox in an unrelated frame.
    tree = await _send(tab, cdp.page.get_frame_tree())
    if frame_id != tree.frame.id_:
        owner, _ = await _send(tab, cdp.dom.get_frame_owner(frame_id))
        owner_node = await _send(tab, cdp.dom.describe_node(backend_node_id=owner, depth=0))
        if not _cloudflare_frame(owner_node):
            return {"ready": False, "reason": "unrelated_target"}
    else:
        # A top-level checkbox is allowed only on a Cloudflare interstitial,
        # never on the regular login or Vote page.
        if not await interstitial_context(tab):
            return {"ready": False, "reason": "unrelated_target"}

    remote = await _send(tab, cdp.dom.resolve_node(backend_node_id=backend))
    try:
        control = await _on_node(tab, remote.object_id, CONTROL_FUNCTION)
    finally:
        with suppress(Exception):
            await _send(tab, cdp.runtime.release_object(remote.object_id))
    if not isinstance(control, dict) or control.get("control") is not True:
        return {"ready": False, "reason": "unrelated_target"}
    if control.get("enabled") is not True or control.get("visible") is not True:
        return {"ready": False, "reason": "disabled_or_hidden"}
    return {"ready": True, "backend": backend, "target": "checkbox"}


VISIBLE_CONTROL_FUNCTION = """function () {
    const control = this;
    if (!control.matches('input[type="checkbox"], [role="checkbox"]') ||
        control.matches(':disabled') || control.checked || control.getAttribute('aria-checked') === 'true' ||
        control.closest('[inert], [aria-disabled="true"]')) return null;
    const visible = el => {
        if (!el?.isConnected || !el.getClientRects().length) return false;
        const style = el.ownerDocument.defaultView.getComputedStyle(el);
        return style.visibility === 'visible' && style.display !== 'none' &&
            Number(style.opacity) !== 0 && style.pointerEvents !== 'none';
    };
    if (visible(control)) return control;
    const label = control.labels?.[0] || control.closest('label');
    return visible(label) ? label : null;
}"""


LOCAL_POINT_FUNCTION = """function () {
    const rect = this.getBoundingClientRect();
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    // Each enclosing root must hit this control/label or its shadow host.
    // This detects an overlay both inside and outside a closed shadow root.
    let node = this;
    while (node) {
        const root = node.getRootNode();
        const hit = root.elementFromPoint?.(x, y);
        if (!hit || !(node === hit || node.contains(hit))) return null;
        node = root.host || null;
    }
    const view = this.ownerDocument.defaultView;
    return {x, y, width:view.innerWidth, height:view.innerHeight};
}"""


def _checkbox_nodes(document, allowed):
    pending, candidates, frames, visited = [(document, allowed)], [], [], 0
    while pending:
        node, allowed = pending.pop()
        visited += 1
        if visited > 4096:
            raise ValueError("document_too_large")
        if node.node_name == "IFRAME":
            allowed = _cloudflare_frame(node)
            if allowed and not node.content_document and node.frame_id:
                frames.append(node.frame_id)
                if len(frames) > 4:
                    raise ValueError("ambiguous_controls")
        attrs = dict(zip((node.attributes or [])[::2], (node.attributes or [])[1::2]))
        if allowed and ((node.node_name == "INPUT" and attrs.get("type", "").lower() == "checkbox")
                        or attrs.get("role") == "checkbox"):
            candidates.append(node.backend_node_id)
            if len(candidates) > 8:
                raise ValueError("ambiguous_controls")
        for child in (node.children or []) + (node.shadow_roots or []):
            pending.append((child, allowed))
        if node.content_document:
            pending.append((node.content_document, allowed))
    return candidates, frames


async def _frame_checkbox_targets(tab, frame_id, width, height):
    targets = []
    async with _provider_session(tab, frame_id) as (child, owner):
        document = await _send(child, cdp.dom.get_document(depth=-1, pierce=True))
        candidates, _ = _checkbox_nodes(document, True)
        owner_model = await _send(tab, cdp.dom.get_box_model(backend_node_id=owner))
        quad = owner_model.content
        for backend in candidates:
            remote, visible = None, None
            try:
                remote = await _send(child, cdp.dom.resolve_node(backend_node_id=backend))
                visible, exception = await _send(child, cdp.runtime.call_function_on(
                    VISIBLE_CONTROL_FUNCTION, object_id=remote.object_id, return_by_value=False))
                if exception or not visible or not visible.object_id:
                    continue
                point = await _on_node(child, visible.object_id, LOCAL_POINT_FUNCTION)
                if (not isinstance(point, dict) or not all(type(point.get(key)) in (int, float)
                        and math.isfinite(point[key]) for key in ("x", "y", "width", "height"))
                        or not (0 < point["x"] < point["width"] and 0 < point["y"] < point["height"])):
                    continue
                # JS rectangles are explicitly child-viewport coordinates.
                # Map through the owner's content quad; do not guess whether a
                # cross-process DOM.getBoxModel is local or main-page relative.
                u, v = point["x"] / point["width"], point["y"] / point["height"]
                x = (1-u)*(1-v)*quad[0] + u*(1-v)*quad[2] + u*v*quad[4] + (1-u)*v*quad[6]
                y = (1-u)*(1-v)*quad[1] + u*(1-v)*quad[3] + u*v*quad[5] + (1-u)*v*quad[7]
                if not (0 < x < width and 0 < y < height):
                    continue
                hit_backend, hit_frame, _ = await _send(tab, cdp.dom.get_node_for_location(
                    x=round(x), y=round(y), include_user_agent_shadow_dom=True,
                    ignore_pointer_events_none=False))
                if hit_backend != owner and hit_frame != frame_id:
                    continue
                targets.append({"ready":True, "backend":backend, "frame_id":frame_id,
                    "target":"checkbox", "x":x, "y":y, "score":None, "source":"frame_dom"})
            finally:
                for ref in (visible, remote):
                    if ref and ref.object_id:
                        with suppress(Exception):
                            await _send(child, cdp.runtime.release_object(ref.object_id))
    return targets


async def semantic_checkbox_target(tab, width, height) -> dict:
    """Find a real control in closed shadow DOM without relying on its appearance."""
    main_allowed = await interstitial_context(tab)
    document = await _send(tab, cdp.dom.get_document(depth=-1, pierce=True))
    try:
        candidates, frames = _checkbox_nodes(document, main_allowed)
    except ValueError as exc:
        return {"ready":False, "reason":str(exc)}
    targets = []
    for backend in candidates:
        remote, visible = None, None
        try:
            remote = await _send(tab, cdp.dom.resolve_node(backend_node_id=backend))
            visible, exception = await _send(tab, cdp.runtime.call_function_on(
                VISIBLE_CONTROL_FUNCTION, object_id=remote.object_id, return_by_value=False))
            if exception or not visible or not visible.object_id:
                continue
            model = await _send(tab, cdp.dom.get_box_model(object_id=visible.object_id))
            quad = model.border
            x, y = sum(quad[::2]) / 4, sum(quad[1::2]) / 4
            if not (0 < x < width and 0 < y < height):
                continue
            hit = await _hit_target(tab, x, y)
            # A semantic target must expose a real checkbox. An opaque frame
            # owner alone needs the separate, strong image-match fallback.
            if hit.get("ready") is True and hit.get("target") == "checkbox":
                if not any(target["backend"] == hit["backend"] and abs(target["x"] - x) <= 1
                           and abs(target["y"] - y) <= 1 for target in targets):
                    targets.append({**hit, "x": x, "y": y, "score": None, "source": "dom"})
        except Exception:
            continue
        finally:
            for ref in (visible, remote):
                if ref and ref.object_id:
                    with suppress(Exception):
                        await _send(tab, cdp.runtime.release_object(ref.object_id))
    for frame_id in frames:
        with suppress(Exception):
            targets.extend(await _frame_checkbox_targets(tab, frame_id, width, height))
    if len(targets) == 1:
        return targets[0]
    return {"ready": False, "reason": "ambiguous_controls" if targets else "no_semantic_control"}


async def checkbox_target(tab, evaluate) -> dict:
    viewport = await asyncio.wait_for(evaluate(tab,
        "({width: innerWidth, height: innerHeight})"), timeout=2)
    if not isinstance(viewport, dict):
        return {"ready": False, "reason": "viewport_changed"}
    width, height = viewport.get("width"), viewport.get("height")
    if not all(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 4096
               for value in (width, height)):
        return {"ready": False, "reason": "viewport_changed"}
    try:
        semantic = await semantic_checkbox_target(tab, width, height)
    except Exception:
        semantic = {"ready": False, "reason": "semantic_observation_unavailable"}
    if semantic.get("ready") is True:
        return semantic
    raw = await _send(tab, cdp.page.capture_screenshot(
        format_="png", clip=cdp.page.Viewport(0, 0, width, height, 1),
        from_surface=True, capture_beyond_viewport=False,
    ), timeout=4)
    if not isinstance(raw, str) or len(raw) > 24 * 1024 * 1024:
        return {"ready": False, "reason": "invalid_image"}
    target = await asyncio.to_thread(match_checkbox, base64.b64decode(raw, validate=True), width, height)
    if target.get("ready") is not True:
        return target
    hit = await _hit_target(tab, target["x"], target["y"])
    return {**target, **hit}


ARM_FUNCTION = """function () {
    const hit = this.nodeType === 1 ? this : this.parentElement;
    const el = hit?.matches('input[type="checkbox"], [role="checkbox"]') ? hit :
        hit?.closest('label')?.control || hit?.closest('[role="checkbox"]');
    if (!el?.matches('input[type="checkbox"], [role="checkbox"]') || !el.isConnected) return false;
    // Outside a closed shadow root, composedPath() hides its internal control.
    // Observe inside the same root rather than reporting a real click as absent.
    const scope = el.getRootNode();
    const labels = [...(el.labels || [])].filter(label => label.control === el);
    const receipt = {pressed: false, released: false, clicked: false};
    const listeners = ['pointerdown', 'pointerup', 'click'].map(type => {
        const handler = event => {
            const path = event.composedPath();
            if (event.isTrusted && (path.includes(el) || labels.some(label => path.includes(label)))) {
                receipt[{pointerdown: 'pressed', pointerup: 'released', click: 'clicked'}[type]] = true;
            }
        };
        scope.addEventListener(type, handler, true);
        return [type, handler];
    });
    this.__autoCfReceipt = {scope, receipt, listeners};
    return true;
}"""

READ_FUNCTION = """function () { return this.__autoCfReceipt?.receipt || null; }"""
CLEAN_FUNCTION = """function () {
    const state = this.__autoCfReceipt;
    for (const [type, handler] of state?.listeners || []) state.scope.removeEventListener(type, handler, true);
    delete this.__autoCfReceipt;
}"""


def _same_target(first, second) -> bool:
    return (second.get("ready") is True and first.get("backend") == second.get("backend")
            and first.get("frame_id") == second.get("frame_id") and all(
        abs(first[key] - second[key]) <= 1 for key in ("x", "y")
    ))


async def _click_target(tab, target) -> None:
    async with AsyncExitStack() as stack:
        observer = tab
        if target.get("frame_id"):
            observer, _ = await stack.enter_async_context(_provider_session(tab, target["frame_id"]))
        await _send_checkbox_input(tab, observer, target)


async def _send_checkbox_input(tab, observer, target) -> None:
    remote = None
    observable = False
    flags = {key: None for key in ("pressed", "released", "clicked")}
    try:
        if target["target"] == "checkbox":
            with suppress(Exception):
                remote = await _send(observer, cdp.dom.resolve_node(backend_node_id=target["backend"]))
                observable = await _on_node(observer, remote.object_id, ARM_FUNCTION) is True
        try:
            await _send(tab, cdp.input_.dispatch_mouse_event(
                "mousePressed", x=target["x"], y=target["y"],
                button=cdp.input_.MouseButton.LEFT, buttons=1, click_count=1,
            ))
        finally:
            await _send(tab, cdp.input_.dispatch_mouse_event(
                "mouseReleased", x=target["x"], y=target["y"],
                button=cdp.input_.MouseButton.LEFT, buttons=0, click_count=1,
            ))
        if observable:
            with suppress(Exception):
                receipt = await _on_node(observer, remote.object_id, READ_FUNCTION)
                if isinstance(receipt, dict):
                    flags = {key: receipt.get(key) is True for key in flags}
        print("  → Cloudflare mouse input: " + json.dumps({
            "target": target["target"], "match_score": target["score"],
            "target_source": target.get("source", "image"),
            "input_sent": True, "trusted_events": flags,
        }, sort_keys=True))
    finally:
        if remote:
            with suppress(Exception):
                await _on_node(observer, remote.object_id, CLEAN_FUNCTION)
            with suppress(Exception):
                await _send(observer, cdp.runtime.release_object(remote.object_id))


async def _click_cloudflare_checkbox_impl(tab, evaluate, cleared) -> str:
    """Wait for a stable target; send at most one click and never infer clearance."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TARGET_WAIT_SEC
    previous, stable_since, last_reason = None, None, None
    while loop.time() < deadline:
        if await cleared():
            return "cleared"
        try:
            target = await checkbox_target(tab, evaluate)
        except Exception:
            target = {"ready": False, "reason": "observation_unavailable"}
        if target.get("ready") is True:
            if previous is None or not _same_target(previous, target):
                stable_since = loop.time()
            previous = target
            if loop.time() - stable_since >= TARGET_STABLE_SEC:
                await _send(tab, cdp.input_.dispatch_mouse_event(
                    "mouseMoved", x=target["x"], y=target["y"], buttons=0,
                ))
                await asyncio.sleep(POLL_SEC)
                if await cleared():
                    return "cleared"
                final = await checkbox_target(tab, evaluate)
                if _same_target(target, final):
                    print("  → Cloudflare checkbox target stable; sending native mouse input")
                    await _click_target(tab, final)
                    return "sent"
                previous, stable_since = None, None
                reason = "changed_after_hover"
            else:
                reason = "settling"
        else:
            previous, stable_since = None, None
            reason = target.get("reason", "observation_unavailable")
        if reason != last_reason:
            print(f"  → Waiting for Cloudflare checkbox: {reason}")
            last_reason = reason
        await asyncio.sleep(POLL_SEC)
    print("  ⚠️  No verified Cloudflare checkbox target; no mouse click sent")
    return "unavailable"


async def click_cloudflare_checkbox(tab, evaluate, cleared) -> str:
    try:
        return await asyncio.wait_for(_click_cloudflare_checkbox_impl(tab, evaluate, cleared), timeout=TARGET_WAIT_SEC)
    except TimeoutError:
        print("  ⚠️ Cloudflare control observation deadline reached")
        return "unavailable"
