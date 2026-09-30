"""Passive CDP response metadata; never collect bodies, credentials or URLs."""

import asyncio
import json
from collections import OrderedDict
from contextlib import suppress
from urllib.parse import urlparse

from nodriver import cdp


MAX_REQUESTS = 256
METHODS = frozenset({"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"})
PHASES = frozenset({"authentication", "vote_page", "vote_input", "vote_confirmation"})
RESOURCE_TYPES = frozenset({"Document", "Stylesheet", "Image", "Media", "Font", "Script", "XHR", "Fetch", "WebSocket", "Other"})


def request_kind(url: str) -> str:
    try:
        parsed = urlparse(url)
        host, path = (parsed.hostname or "").lower(), parsed.path
        if host in {"top.gg", "www.top.gg", "api.top.gg"}:
            if path == "/api/auth/session": return "topgg_session"
            if path.startswith("/api/auth/"): return "topgg_auth"
            if path.startswith("/api/") or host == "api.top.gg": return "topgg_api"
            if path.startswith("/_next/") or path.endswith((".js", ".css", ".png", ".svg")): return "topgg_asset"
            return "topgg_page"
        if host == "challenges.cloudflare.com": return "cloudflare_challenge"
        if host in {"discord.com", "www.discord.com"}:
            return "discord_oauth" if path.startswith("/oauth2/") else "discord_service"
    except (ValueError, TypeError):
        pass
    return "other"


def safe_headers(headers) -> dict:
    headers = headers if isinstance(headers, dict) else {}
    selected = {str(key).lower(): value for key, value in headers.items()
                if str(key).lower() in {"cf-mitigated", "cf-ray", "content-type"}}
    ray = selected.get("cf-ray", "")
    # Accept only the documented correlation-ID shape; arbitrary header strings
    # could otherwise leak data to public Actions logs.
    import re
    ray = ray if isinstance(ray, str) and re.fullmatch(r"[a-fA-F0-9]{16}-[A-Z]{3}", ray) else None
    content = str(selected.get("content-type", "")).lower().split(";", 1)[0]
    return {"cloudflare_challenge": selected.get("cf-mitigated") == "challenge", "cf_ray": ray,
            "content_kind": "json" if content == "application/json" else "html" if content == "text/html" else "other"}


class RequestDiagnostics:
    def __init__(self, tab):
        self.tab, self.phase = tab, "authentication"
        self.requests = OrderedDict()
        self.completed = OrderedDict()
        self.handlers = [(cdp.network.RequestWillBeSent, self.on_request),
                         (cdp.network.ResponseReceived, self.on_response),
                         (cdp.network.ResponseReceivedExtraInfo, self.on_extra),
                         (cdp.network.LoadingFinished, self.on_finished),
                         (cdp.network.LoadingFailed, self.on_failed)]

    async def start(self):
        try:
            for event, callback in self.handlers: self.tab.add_handler(event, callback)
            await asyncio.wait_for(self.tab.send(cdp.network.enable()), timeout=2)
            self.tab._topgg_diagnostics = self
        except Exception:
            self.stop()
            print("  Network diagnostics unavailable")

    def stop(self):
        for event, callback in self.handlers:
            with suppress(Exception): self.tab.remove_handler(event, callback)
        self.requests.clear()
        self.completed.clear()
        if getattr(self.tab, "_topgg_diagnostics", None) is self:
            self.tab._topgg_diagnostics = None

    async def on_request(self, event):
        # A redirect reuses its request ID. Report its response before replacing
        # the metadata, with no Location URL or request headers.
        if getattr(event, "redirect_response", None):
            self.emit(event.request_id, event.redirect_response.status, event.redirect_response.headers)
        method = str(event.request.method).upper()
        resource = getattr(getattr(event, "type_", None), "value", "Other")
        self.completed.pop(event.request_id, None)
        self.requests[event.request_id] = {
            "request_kind": request_kind(event.request.url), "method": method if method in METHODS else "OTHER",
            "resource_type": resource if resource in RESOURCE_TYPES else "Other",
            "phase": self.phase if self.phase in PHASES else "authentication", "emitted": set(),
        }
        self.requests.move_to_end(event.request_id)
        while len(self.requests) > MAX_REQUESTS: self.requests.popitem(last=False)

    def emit(self, request_id, status, headers):
        info = self.requests.get(request_id) or self.completed.get(request_id)
        if not info or isinstance(status, bool) or not isinstance(status, (int, float)) or not 100 <= status <= 599:
            return
        status = int(status)
        relevant_write = info["phase"] in {"vote_input", "vote_confirmation"} and info["request_kind"] == "topgg_api" and info["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if status < 400 and not relevant_write: return
        safe = safe_headers(headers)
        signature = (status, safe["cloudflare_challenge"])
        if signature in info["emitted"]: return
        info["emitted"].add(signature)
        print("  Network response diagnostic: " + json.dumps({
            **{key: info[key] for key in ("request_kind", "resource_type", "method", "phase")}, "status": status, **safe,
        }, sort_keys=True))

    async def on_response(self, event):
        self.emit(event.request_id, event.response.status, event.response.headers)

    async def on_extra(self, event):
        # This also records a denial whose response is hidden from page JS by CORS.
        self.emit(event.request_id, event.status_code, event.headers)

    async def on_finished(self, event):
        info = self.requests.pop(event.request_id, None)
        if info:
            # ExtraInfo can arrive after the ordinary response/finish event.
            # Retain only already-sanitized metadata in a second bounded cache.
            self.completed[event.request_id] = info
            while len(self.completed) > MAX_REQUESTS: self.completed.popitem(last=False)

    async def on_failed(self, event):
        info = self.requests.get(event.request_id)
        if info:
            print("  Network request failed: " + json.dumps({
                key: info[key] for key in ("request_kind", "resource_type", "method", "phase")
            }, sort_keys=True))
        await self.on_finished(event)


def set_phase(tab, phase):
    tracker = getattr(tab, "_topgg_diagnostics", None)
    if isinstance(tracker, RequestDiagnostics) and phase in PHASES: tracker.phase = phase
