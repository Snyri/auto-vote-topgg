"""Conservative vote-request evidence. Raw URLs and payloads are never retained."""

import json
import math
import re
import time
from urllib.parse import parse_qs, urlparse

import graphql_vote


MAX_VOTE_REQUESTS = 16
RPC_VOTE_MUTATIONS = {"vote", "castvote", "submitvote", "voteforbot"}
RPC_VOTE_READS = {"getvotestate", "getvotestatus", "getvoteeligibility", "canvote", "hasvoted", "checkvote"}
ROUTE_WORDS = frozenset({"api", "client", "v0", "v1", "bot", "bots", "entity", "entities", "vote", "votes",
    "trpc", "graphql", "status", "eligibility", "check", "user", "users", "analytics", "track",
    "canVote", "hasVoted", "getVoteState", "getVoteStatus", "getVoteEligibility", "checkVote",
    "castVote", "submitVote", "voteForBot", "createVote", "addVote"})


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def bot_vote_page(url, bot_id):
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname in {"top.gg", "www.top.gg"}
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path.rstrip("/") == "/bot/" + bot_id + "/vote")
    except (ValueError, TypeError, RecursionError):
        return False


def safe_api_route(url):
    """A fixed-vocabulary template; query strings and arbitrary segments vanish."""
    try:
        parsed = urlparse(url)
        if parsed.hostname not in {"top.gg", "www.top.gg", "api.top.gg"} or not parsed.path.startswith("/api/"):
            return None
        segments = parsed.path.strip("/").split("/")
        result = []
        for segment in segments[:8]:
            if re.fullmatch(r"[0-9]+|[0-9a-fA-F-]{32,36}", segment):
                result.append(":id")
            else:
                parts = re.split(r"([.,])", segment)
                result.append("".join(part if part in ROUTE_WORDS or part in {".", ","} else ":other"
                                      for part in parts[:9]))
        if len(segments) > 8: result.append(":more")
        return "/" + "/".join(result)
    except (ValueError, TypeError):
        return None


def _rpc_target(raw, bot_id):
    """Inspect only explicit bot identifiers in a small single-operation input."""
    if not isinstance(raw, str) or len(raw) > 32768:
        return False
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return False
    if isinstance(value, dict) and value and all(str(key).isdigit() for key in value) and len(value) != 1:
        return False
    targets = []
    pending = [(value, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if visited > 64 or depth > 5:
            return False
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"botId", "botID", "bot_id", "bot"}:
                    if type(item) not in {str, int} or not re.fullmatch(r"[0-9]{1,20}", str(item)):
                        return False
                    targets.append(str(item))
                elif isinstance(item, (dict, list)):
                    pending.append((item, depth + 1))
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return bool(targets) and all(target == bot_id for target in targets)


def vote_operation(url, method, document_url, bot_id, post_data=None):
    """Recognize exact bot vote routes or an explicitly targeted RPC operation.

    Unknown endpoints and mixed RPC batches are deliberately unclassified.
    A nearby POST or an arbitrary occurrence of the word 'vote' is insufficient.
    """
    if (not isinstance(url, str) or len(url) > 8192
            or not isinstance(bot_id, str) or not re.fullmatch(r"[0-9]{1,20}", bot_id)):
        return "unrelated"
    if not bot_vote_page(document_url, bot_id):
        return "unrelated"
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"top.gg", "www.top.gg", "api.top.gg"}
                or parsed.port not in {None, 443} or parsed.username or parsed.password):
            return "unrelated"
        route = re.fullmatch(r"/api/(?:(?:client|v0|v1)/)?(?:bot|bots|entity|entities)/([0-9]{1,20})/vote(?:/(status|eligibility|check))?/?", parsed.path)
        if route and route.group(1) == bot_id:
            if method == "POST" and route.group(2) is None:
                return "vote_submission"
            if method == "GET":
                return "vote_state"
        if parsed.path.rstrip("/") == "/api/graphql" and method == "POST":
            return graphql_vote.inspect_request(post_data, bot_id)["operation"]
        rpc = re.fullmatch(r"/api/trpc/(bot|bots|vote|votes|voting)\.([A-Za-z]+)", parsed.path)
        if rpc:
            action = rpc.group(2).lower()
            query = parse_qs(parsed.query, max_num_fields=16)
            if query.get("batch") not in (None, ["1"]):
                return "unrelated"
            raw = post_data if method == "POST" else (query.get("input") or [None])[0]
            if not _rpc_target(raw, bot_id):
                return "unrelated"
            if method == "POST" and action in RPC_VOTE_MUTATIONS:
                return "vote_submission"
            if method in {"GET", "POST"} and action in RPC_VOTE_READS:
                return "vote_state"
    except (ValueError, TypeError):
        pass
    return "unrelated"


def first_party_write(url, method, document_url, bot_id):
    """Unknown same-site writes veto a retry; they cannot establish rejection."""
    if method not in {"POST", "PUT", "PATCH", "DELETE"} or not bot_vote_page(document_url, bot_id):
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname in {"top.gg", "www.top.gg", "api.top.gg"}
                and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except (ValueError, TypeError):
        return False


class VoteNetworkState:
    """One bot's readiness and one native input's bounded response evidence."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.bot_id = None
        self.context = 0
        self.generation = 0
        self.last_denial = None
        self.denial_started = None
        self.recovered_by = None
        self.candidates = {}
        self.armed = False
        self.trusted = False
        self.pressed_at = None
        self.incomplete = False
        self.entity_key = None
        self.document_id = None

    def select_document(self, loader_id):
        """A committed main document must not inherit an earlier page's gate."""
        if not isinstance(loader_id, str) or not loader_id or loader_id == self.document_id:
            return
        self.document_id = loader_id
        self.context += 1
        self.last_denial = self.denial_started = self.recovered_by = None
        self.entity_key = None
        self.end_input()

    def select_bot(self, bot_id):
        if bot_id != self.bot_id:
            self.bot_id = bot_id
            self.context += 1
            self.last_denial = None
            self.denial_started = None
            self.recovered_by = None
            self.entity_key = None
        self.end_input()

    def begin_input(self):
        self.generation += 1
        self.candidates.clear()
        self.armed = True
        self.trusted = False
        self.pressed_at = None
        self.incomplete = False

    def end_input(self):
        self.armed = False
        self.candidates.clear()
        self.trusted = False
        self.pressed_at = None

    def receipt(self, receipt):
        self.trusted = all(receipt.get(key) is True for key in ("pressed", "released", "clicked"))
        self.pressed_at = number(receipt.get("pressed_at"))

    def on_request(self, event):
        request = event.request
        operation = vote_operation(request.url, request.method, getattr(event, "document_url", None),
                                   self.bot_id, getattr(request, "post_data", None))
        graphql = (safe_api_route(request.url) == "/api/graphql"
                   and bot_vote_page(getattr(event, "document_url", None), self.bot_id or ""))
        descriptor = graphql_vote.inspect_request(getattr(request, "post_data", None), self.bot_id) if graphql else {}
        loader = getattr(event, "loader_id", None)
        current_document = not (self.document_id and loader and loader != self.document_id)
        if not current_document:
            operation = "unrelated"
        entity_key = descriptor.get("entity_key")
        if operation != "unrelated" and entity_key is not None:
            if self.entity_key is None:
                self.entity_key = entity_key
            elif entity_key != self.entity_key:
                operation = "unrelated"
        stamp = number(getattr(event, "wall_time", None))
        info = {"operation": operation, "context": self.context if current_document else -1, "generation": self.generation,
                "started": stamp, "statuses": set(), "challenge": False, "finished": False,
                "redirected": bool(getattr(event, "redirect_response", None)),
                "json_response": False, "readiness_invalid": False, "failed": False,
                "graphql": graphql, "response_key": descriptor.get("response_key"),
                "response_outcome": "pending" if graphql else "not_applicable",
                "graphql_kind": descriptor.get("graphql_kind", "not_applicable"),
                "graphql_target": descriptor.get("graphql_target", "not_applicable"),
                "graphql_shape": descriptor.get("graphql_shape", "not_applicable"),
                "graphql_gate": descriptor.get("graphql_gate", "not_applicable") if current_document else "previous_document",
                "application_submission": current_document and graphql
                    and descriptor.get("graphql_kind") == "mutation"
                    and descriptor.get("graphql_shape") == "single"
                    and (operation == "vote_submission" or descriptor.get("graphql_target") == "matching_bot")}
        readonly_graphql = graphql and descriptor.get("graphql_kind") == "query"
        if (current_document and self.armed and operation != "vote_state" and not readonly_graphql
                and first_party_write(request.url, request.method, getattr(event, "document_url", None), self.bot_id)):
            if len(self.candidates) >= MAX_VOTE_REQUESTS:
                self.incomplete = True
            else:
                self.candidates[event.request_id] = info
        return info

    def response(self, info, status, safe):
        if info["context"] != self.context or type(status) not in {int, float} or not 100 <= status <= 599:
            return
        status = int(status)
        info["statuses"].add(status)
        challenged = status == 403 and safe["cloudflare_challenge"] is True and safe["content_kind"] == "html"
        info["challenge"] |= challenged
        info["json_response"] |= (200 <= status < 300 and safe["content_kind"] == "json"
                                   and safe["cloudflare_challenge"] is not True)
        info["readiness_invalid"] |= safe["cloudflare_challenge"] is True or safe["content_kind"] == "html"
        if info["operation"] in {"vote_submission", "vote_state"}:
            if challenged:
                self.recovered_by = None
                if self.last_denial is None:
                    self.denial_started = info["started"]
                elif self.denial_started is not None and info["started"] is not None:
                    self.denial_started = max(self.denial_started, info["started"])
                else:
                    self.denial_started = None
                self.last_denial = self.clock()
        self._clear_protection_if_ready(info)

    def finish(self, info, *, failed=False):
        info["finished"] = True
        info["failed"] |= failed
        self._clear_protection_if_ready(info)

    def response_body(self, info, outcome):
        if outcome not in {"usable", "captcha_required", "unauthenticated", "error", "invalid", "unavailable"}:
            outcome = "unavailable"
        info["response_outcome"] = outcome
        self._clear_protection_if_ready(info)

    def _clear_protection_if_ready(self, info):
        ready = (info["context"] == self.context and info["operation"] == "vote_state"
                and info["finished"] and not info["failed"] and not info["redirected"]
                and not info["readiness_invalid"] and info["json_response"]
                and (not info["graphql"] or info["response_outcome"] == "usable")
                and len(info["statuses"]) == 1 and all(200 <= status < 300 for status in info["statuses"]))
        # ExtraInfo may arrive after LoadingFinished. Revoke only this response's
        # recovery if late evidence contradicts it, without retaining raw data.
        if self.recovered_by is info and not ready:
            self.last_denial = self.clock()
            self.denial_started = info["started"]
            self.recovered_by = None
        if (ready
                and self.denial_started is not None and info["started"] is not None
                and info["started"] >= self.denial_started):
            self.recovered_by = info
            self.last_denial = None
            self.denial_started = None

    def protection_pending(self):
        # Time passing is not evidence that the denied API became usable.
        # A fresh, complete vote-state response or a new bot/browser clears it.
        return self.last_denial is not None

    def submission_outcome(self):
        """Only the latest identified mutation belonging to this trusted press."""
        if not self.armed or not self.trusted or self.pressed_at is None:
            return None
        requests = [info for info in self.candidates.values()
                    if info["application_submission"]
                    and info["context"] == self.context and info["generation"] == self.generation
                    and info["started"] is not None and info["started"] >= self.pressed_at]
        if not requests:
            return None
        # Once an actual vote operation is identified, ancillary bot mutations
        # must not replace its result merely because they completed later.
        known = [info for info in requests if info["operation"] == "vote_submission"]
        if known:
            requests = known
        latest = max(requests, key=lambda info: info["started"])
        if not known:
            for info in requests:
                if info["finished"] and info["response_outcome"] in {"captcha_required", "unauthenticated", "error", "invalid"}:
                    return info["response_outcome"]
        if latest["finished"] and latest["challenge"] and latest["statuses"] == {403}:
            return "protection_rejected"
        return latest["response_outcome"] if latest["finished"] else "pending"

    def definitely_rejected(self):
        if not self.armed or not self.trusted or self.pressed_at is None or self.incomplete:
            return False
        # Requests initiated before the actual trusted press cannot establish
        # whether this input was rejected. CDP wallTime and DOM timestamps use
        # the browser's epoch clock, including fractional seconds.
        if any(info["started"] is None for info in self.candidates.values()):
            return False
        after_press = [info for info in self.candidates.values() if info["started"] >= self.pressed_at]
        return bool(after_press) and all(
            info["operation"] == "vote_submission" and info["generation"] == self.generation
            and info["finished"] and not info["redirected"]
            and info["statuses"] == {403} and info["challenge"]
            for info in self.candidates.values()
        )
