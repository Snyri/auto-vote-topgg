"""Passive CDP metadata; never retain raw URLs, payloads or credentials."""

import asyncio
import base64
import json
from collections import OrderedDict
from contextlib import suppress
from urllib.parse import urlparse

from nodriver import cdp

import graphql_vote
from vote_network import VoteNetworkState, bot_vote_page, safe_api_route, number


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
            "content_kind": "json" if content in {"application/json", "application/graphql-response+json"}
            else "html" if content == "text/html" else "other"}


class RequestDiagnostics:
    def __init__(self, tab):
        self.tab, self.phase = tab, "authentication"
        self.requests = OrderedDict()
        self.completed = OrderedDict()
        self.vote_network = VoteNetworkState()
        self.document_request = None
        self.document_challenged = False
        self.main_frame = None
        self.handlers = [(cdp.network.RequestWillBeSent, self.on_request),
                         (cdp.network.ResponseReceived, self.on_response),
                         (cdp.network.ResponseReceivedExtraInfo, self.on_extra),
                         (cdp.network.LoadingFinished, self.on_finished),
                         (cdp.network.LoadingFailed, self.on_failed),
                         (cdp.page.FrameNavigated, self.on_navigated)]

    async def start(self):
        try:
            for event, callback in self.handlers: self.tab.add_handler(event, callback)
            await asyncio.wait_for(self.tab.send(cdp.page.enable()), timeout=2)
            await asyncio.wait_for(self.tab.send(cdp.network.enable(
                max_total_buffer_size=2 * 1024 * 1024, max_resource_buffer_size=256 * 1024,
                max_post_data_size=graphql_vote.MAX_PAYLOAD,
            )), timeout=2)
            self.tab._topgg_diagnostics = self
            return True
        except Exception:
            self.stop()
            print("  Network diagnostics unavailable")
            return False

    async def on_navigated(self, event):
        frame = event.frame
        if getattr(frame, "parent_id", None) is None:
            self.main_frame = frame.id_
            self.vote_network.select_document(str(frame.loader_id))

    def stop(self):
        for event, callback in self.handlers:
            with suppress(Exception): self.tab.remove_handler(event, callback)
        self.requests.clear()
        self.completed.clear()
        self.vote_network.end_input()
        self.document_challenged = False
        if getattr(self.tab, "_topgg_diagnostics", None) is self:
            self.tab._topgg_diagnostics = None

    async def on_request(self, event):
        # A redirect reuses its request ID. Report its response before replacing
        # the metadata, with no Location URL or request headers.
        if getattr(event, "redirect_response", None):
            previous = self.requests.get(event.request_id) or self.completed.get(event.request_id)
            if previous:
                previous["vote_network"]["redirected"] = True
            self.emit(event.request_id, event.redirect_response.status, event.redirect_response.headers)
        method = str(event.request.method).upper()
        resource = getattr(getattr(event, "type_", None), "value", "Other")
        self.completed.pop(event.request_id, None)
        self.requests[event.request_id] = {
            "request_kind": request_kind(event.request.url), "method": method if method in METHODS else "OTHER",
            "resource_type": resource if resource in RESOURCE_TYPES else "Other",
            "phase": self.phase if self.phase in PHASES else "authentication", "emitted": set(),
            "vote_network": self.vote_network.on_request(event),
            "api_route": safe_api_route(event.request.url),
        }
        if (resource == "Document" and self.requests[event.request_id]["request_kind"] == "topgg_page"
                and (self.main_frame is None or getattr(event, "frame_id", None) == self.main_frame)
                and self.vote_network.bot_id is not None
                and bot_vote_page(event.request.url, self.vote_network.bot_id)):
            self.document_request = event.request_id
            self.document_challenged = False
        self.requests.move_to_end(event.request_id)
        info = self.requests[event.request_id]["vote_network"]
        if info["graphql"]:
            print("  GraphQL request diagnostic: " + json.dumps({
                "phase": self.phase, "vote_operation": info["operation"],
                **{key: info[key] for key in ("graphql_kind", "graphql_target", "graphql_shape", "graphql_gate")},
                "payload_available": isinstance(getattr(event.request, "post_data", None), str),
            }, sort_keys=True))
        while len(self.requests) > MAX_REQUESTS:
            request_id, _ = self.requests.popitem(last=False)
            if request_id in self.vote_network.candidates:
                self.vote_network.incomplete = True

    def emit(self, request_id, status, headers):
        info = self.requests.get(request_id) or self.completed.get(request_id)
        if not info or isinstance(status, bool) or not isinstance(status, (int, float)) or not 100 <= status <= 599:
            return
        status = int(status)
        safe = safe_headers(headers)
        if request_id == self.document_request:
            self.document_challenged |= safe["cloudflare_challenge"] is True and safe["content_kind"] == "html"
        self.vote_network.response(info["vote_network"], status, safe)
        relevant_write = info["phase"] in {"vote_input", "vote_confirmation"} and info["request_kind"] == "topgg_api" and info["method"] in {"POST", "PUT", "PATCH", "DELETE"}
        if status < 400 and not relevant_write: return
        signature = (status, safe["cloudflare_challenge"])
        if signature in info["emitted"]: return
        info["emitted"].add(signature)
        print("  Network response diagnostic: " + json.dumps({
            **{key: info[key] for key in ("request_kind", "resource_type", "method", "phase")}, "status": status, **safe,
            "vote_operation": info["vote_network"]["operation"],
            "api_route": info["api_route"],
            **({key: info["vote_network"][key] for key in ("graphql_kind", "graphql_target", "graphql_shape", "graphql_gate")}
               if info["vote_network"]["graphql"] else {}),
        }, sort_keys=True))

    async def on_response(self, event):
        self.emit(event.request_id, event.response.status, event.response.headers)

    async def on_extra(self, event):
        # This also records a denial whose response is hidden from page JS by CORS.
        self.emit(event.request_id, event.status_code, event.headers)

    async def on_finished(self, event, *, failed=False):
        info = self.requests.pop(event.request_id, None)
        if info:
            self.vote_network.finish(info["vote_network"], failed=failed)
            # ExtraInfo can arrive after the ordinary response/finish event.
            # Retain only already-sanitized metadata in a second bounded cache.
            self.completed[event.request_id] = info
            while len(self.completed) > MAX_REQUESTS:
                request_id, _ = self.completed.popitem(last=False)
                if request_id in self.vote_network.candidates:
                    self.vote_network.incomplete = True
            network = info["vote_network"]
            if (not failed and (network["graphql"] or network["operation"] != "unrelated")
                    and network["json_response"] and not network["readiness_invalid"]):
                outcome = "unavailable"
                try:
                    body, encoded = await asyncio.wait_for(
                        self.tab.send(cdp.network.get_response_body(event.request_id)), timeout=2)
                    if isinstance(body, str) and len(body) <= graphql_vote.MAX_RESPONSE * 4 // 3 + 4:
                        if encoded:
                            body = base64.b64decode(body, validate=True).decode("utf-8")
                        inspected_operation = "vote_submission" if network["application_submission"] else network["operation"]
                        if network["graphql"]:
                            outcome = graphql_vote.inspect_response(body, network["response_key"], inspected_operation)
                        else:
                            outcome = graphql_vote.inspect_json_response(body, inspected_operation)
                except Exception:
                    pass
                self.vote_network.response_body(network, outcome)
                print("  API response diagnostic: " + json.dumps({
                    "phase": info["phase"], "vote_operation": network["operation"],
                    "outcome": outcome,
                }, sort_keys=True))

    async def on_failed(self, event):
        info = self.requests.get(event.request_id)
        if info:
            print("  Network request failed: " + json.dumps({
                key: info[key] for key in ("request_kind", "resource_type", "method", "phase")
            }, sort_keys=True))
        await self.on_finished(event, failed=True)


def set_phase(tab, phase):
    tracker = getattr(tab, "_topgg_diagnostics", None)
    if isinstance(tracker, RequestDiagnostics) and phase in PHASES: tracker.phase = phase


def vote_state(tab):
    tracker = getattr(tab, "_topgg_diagnostics", None)
    return tracker.vote_network if isinstance(tracker, RequestDiagnostics) else None


def select_vote_bot(tab, bot_id):
    state = vote_state(tab)
    if state is not None:
        state.select_bot(bot_id)


def begin_vote_input(tab):
    state = vote_state(tab)
    if state is not None:
        state.begin_input()


def record_vote_receipt(tab, receipt):
    state = vote_state(tab)
    if state is not None:
        state.receipt(receipt)
