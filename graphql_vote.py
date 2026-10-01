"""Bounded inspection of vote operations; no query, variables or response text is retained."""

import hashlib
import json
import re


MAX_PAYLOAD = 32768
MAX_RESPONSE = 131072
VOTE_FIELDS = frozenset({"vote", "castVote", "submitVote", "voteForBot", "voteBot",
                         "createVote", "addVote", "voteEntity", "entityVote", "createEntityVote",
                         "submitEntityVote", "castEntityVote", "createBotVote", "submitBotVote"})
STATE_FIELDS = frozenset({"canVote", "hasVoted", "getVoteState", "getVoteStatus",
                          "getVoteEligibility", "checkVote", "voteState", "voteStatus",
                          "voteEligibility", "viewerVote", "userVote", "currentUserVote"})
BOT_KEYS = frozenset({"botId", "botID", "bot_id", "bot", "discordId", "discordBotId", "platformId"})
ENTITY_KEYS = frozenset({"id", "entityId", "entityID", "entity_id"})
TOKEN = re.compile(r'\s+|#[^\r\n]*|,|\.\.\.|"(?:[^"\\\r\n]|\\.)*"|'
                   r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|'
                   r'[_A-Za-z][_0-9A-Za-z]*|[!$():=@\[\]{}]')
NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*\Z")


class Document:
    """A small GraphQL subset. Unsupported/mixed operations remain unknown."""

    def __init__(self, text, variables):
        self.tokens = []
        position = 0
        for match in TOKEN.finditer(text):
            if match.start() != position:
                raise ValueError("unsupported syntax")
            position = match.end()
            token = match.group()
            if not token.isspace() and not token.startswith("#") and token != ",":
                self.tokens.append(token)
        if position != len(text) or len(self.tokens) > 2048:
            raise ValueError("unsupported syntax")
        self.index, self.variables = 0, variables
        self.fragments = {}

    def take(self, expected=None):
        if self.index >= len(self.tokens):
            raise ValueError("incomplete document")
        value = self.tokens[self.index]
        if expected is not None and value != expected:
            raise ValueError("unexpected token")
        self.index += 1
        return value

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def name(self):
        value = self.take()
        if not NAME.fullmatch(value):
            raise ValueError("invalid name")
        return value

    def variable_type(self, depth=0):
        if depth > 8:
            raise ValueError("nested type")
        if self.peek() == "[":
            self.take("[")
            self.variable_type(depth + 1)
            self.take("]")
        else:
            self.name()
        if self.peek() == "!":
            self.take("!")

    def value(self, depth=0):
        if depth > 8:
            raise ValueError("nested value")
        token = self.take()
        if token == "$":
            return self.variables.get(self.name())
        if token == "{":
            result = {}
            while self.peek() != "}":
                key = self.name()
                if key in result:
                    raise ValueError("duplicate input")
                self.take(":")
                result[key] = self.value(depth + 1)
            self.take("}")
            return result
        if token == "[":
            result = []
            while self.peek() != "]":
                result.append(self.value(depth + 1))
            self.take("]")
            return result
        if token.startswith('"') or token in {"true", "false", "null"} or token[0] in "-0123456789":
            return json.loads(token)
        if NAME.fullmatch(token):
            return token
        raise ValueError("invalid value")

    def selections(self, depth=0):
        if depth > 12:
            raise ValueError("nested selection")
        self.take("{")
        result = []
        while self.peek() != "}":
            # Fragments/directives at the operation root require schema-aware
            # resolution. Never infer that a conditional mutation was executed.
            if self.peek() == "...":
                if depth == 0:
                    raise ValueError("fragment at operation root")
                self.take("...")
                fragment = self.name()
                if fragment == "on":
                    self.name()
                    result.extend(self.selections(depth + 1))
                else:
                    result.append(("...", fragment, {}, []))
                continue
            field = self.name()
            response_key = field
            if self.peek() == ":":
                self.take(":")
                field = self.name()
            args = {}
            if self.peek() == "(":
                self.take("(")
                while self.peek() != ")":
                    key = self.name()
                    if key in args:
                        raise ValueError("duplicate argument")
                    self.take(":")
                    args[key] = self.value()
                self.take(")")
            children = self.selections(depth + 1) if self.peek() == "{" else []
            result.append((field, response_key, args, children))
        self.take("}")
        if not result:
            raise ValueError("empty selection")
        return result

    def operation(self):
        kind, name = "query", None
        if self.peek() != "{":
            kind = self.take()
            if kind not in {"query", "mutation"}:
                raise ValueError("unsupported operation")
            if self.peek() not in {"(", "{"}:
                name = self.name()
            if self.peek() == "(":
                self.take("(")
                seen = set()
                while self.peek() != ")":
                    self.take("$")
                    variable = self.name()
                    if variable in seen:
                        raise ValueError("duplicate variable")
                    seen.add(variable)
                    self.take(":")
                    self.variable_type()
                    if self.peek() == "=":
                        self.take("=")
                        default = self.value()
                        if variable not in self.variables:
                            self.variables[variable] = default
                self.take(")")
        selections = self.selections()
        while self.peek() == "fragment":
            self.take("fragment")
            fragment = self.name()
            if fragment in self.fragments or len(self.fragments) >= 16:
                raise ValueError("duplicate/large fragments")
            self.take("on")
            self.name()
            self.fragments[fragment] = self.selections(depth=1)
        if self.peek() is not None:
            raise ValueError("multiple operations")
        return kind, name, selections

    def state_fields(self, children, visited=frozenset()):
        if len(visited) > 12:
            return False
        for field, key, _, nested in children:
            if field in STATE_FIELDS:
                return True
            if nested and self.state_fields(nested, visited):
                return True
            if field == "..." and key not in visited and key in self.fragments:
                if self.state_fields(self.fragments[key], visited | {key}):
                    return True
        return False


def _target(args, bot_id):
    bot_targets, entity_targets = [], []
    pending = [(args, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if visited > 64 or depth > 6:
            return "unknown", None
        if isinstance(node, dict):
            for key, value in node.items():
                if key in BOT_KEYS | ENTITY_KEYS:
                    pattern = r"[0-9]{1,20}" if key in BOT_KEYS else r"(?:[0-9]{1,20}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
                    if type(value) not in {str, int} or not re.fullmatch(pattern, str(value)):
                        return "unknown", None
                    (bot_targets if key in BOT_KEYS else entity_targets).append(str(value))
                elif isinstance(value, (dict, list)):
                    pending.append((value, depth + 1))
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    if bot_targets:
        if any(value != bot_id for value in bot_targets):
            return "other_bot", None
        if len(set(entity_targets)) > 1:
            return "unknown", None
        return "matching_bot", None
    if not entity_targets or len(set(entity_targets)) != 1:
        return "unknown", None
    if entity_targets[0] == bot_id:
        return "matching_bot", None
    # Top.gg uses internal entity identifiers as well as Discord bot IDs.
    # Keep the distinction explicit. An entity mutation is only retryable when
    # every same-site write following the trusted press was denied by Cloudflare.
    digest = hashlib.sha256(entity_targets[0].encode("ascii")).hexdigest()
    return "page_entity", digest


def inspect_request(raw, bot_id):
    result = {"operation": "unrelated", "graphql_kind": "unknown", "graphql_target": "unknown",
              "graphql_shape": "unknown", "graphql_gate": "payload_missing", "entity_key": None, "response_key": None}
    if not isinstance(raw, str) or len(raw) > MAX_PAYLOAD:
        if isinstance(raw, str):
            result["graphql_gate"] = "payload_too_large"
        return result
    try:
        result["graphql_gate"] = "payload_invalid"
        data = json.loads(raw)
        if isinstance(data, list):
            if len(data) != 1:
                result["graphql_shape"] = "batch"
                result["graphql_gate"] = "mixed_batch"
                return result
            data = data[0]
        if not isinstance(data, dict) or not isinstance(data.get("query"), str):
            result["graphql_gate"] = "document_missing"
            return result
        variables = data.get("variables") or {}
        if not isinstance(variables, dict):
            return result
        result["graphql_gate"] = "unsupported_document"
        document = Document(data["query"], variables.copy())
        kind, name, fields = document.operation()
        result.update(graphql_kind=kind, graphql_shape="single" if len(fields) == 1 else "mixed")
        if data.get("operationName") is not None and data["operationName"] != name:
            result["graphql_gate"] = "operation_mismatch"
            return result
        if len(fields) != 1:
            result["graphql_gate"] = "mixed_operation"
            return result
        field, response_key, args, children = fields[0]
        # A bounded single-operation response can expose application errors even
        # when the live schema uses a root name outside the semantic allowlist.
        # That does not identify a vote or permit resubmission.
        result["response_key"] = hashlib.sha256(response_key.encode()).hexdigest()
        scope, entity_key = _target(args, bot_id)
        result["graphql_target"] = scope
        result["entity_key"] = entity_key
        state_field = field in STATE_FIELDS or document.state_fields(children)
        if (kind == "mutation" and field not in VOTE_FIELDS) or (kind == "query" and not state_field):
            result["graphql_gate"] = "unrecognized_field"
            return result
        if scope not in {"matching_bot", "page_entity"}:
            result["graphql_gate"] = "other_bot" if scope == "other_bot" else "target_unresolved"
            return result
        result.update(operation="vote_submission" if kind == "mutation" else "vote_state",
                      entity_key=entity_key, graphql_gate="recognized")
        return result
    except (ValueError, TypeError, RecursionError, IndexError):
        return result


def inspect_response(raw, response_key, operation=None):
    """A GraphQL 200 can contain an error. Return only a fixed outcome category."""
    if not isinstance(raw, str) or len(raw) > MAX_RESPONSE:
        return "unavailable"
    try:
        data = json.loads(raw)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            return "invalid"
        errors = data.get("errors")
        if errors is not None and not isinstance(errors, list):
            return "error"
        if errors:
            if not isinstance(errors, list):
                return "error"
            for error in errors[:16]:
                if not isinstance(error, dict):
                    continue
                extensions = error.get("extensions")
                code = extensions.get("code") if isinstance(extensions, dict) else None
                message = error.get("message")
                message = message.lower()[:1024] if isinstance(message, str) else ""
                if code in {"CAPTCHA_REQUIRED", "TURNSTILE_REQUIRED", "INVALID_CAPTCHA"} or any(
                    word in message for word in ("captcha", "turnstile", "verify you are human")):
                    return "captcha_required"
                if code == "UNAUTHENTICATED":
                    return "unauthenticated"
            return "error"
        payload = data.get("data")
        if (not isinstance(payload, dict) or len(payload) > 32 or not isinstance(response_key, str)
                or not any(isinstance(key, str) and hashlib.sha256(key.encode()).hexdigest() == response_key
                           and value is not None for key, value in payload.items())):
            return "invalid"
        selected = next(value for key, value in payload.items()
                        if hashlib.sha256(key.encode()).hexdigest() == response_key)
        if selected is False and operation == "vote_submission":
            return "error"
        pending = [(selected, 0)]
        inspected = 0
        while pending:
            node, depth = pending.pop()
            inspected += 1
            if inspected > 64 or depth > 6:
                return "unavailable"
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"ok", "success", "accepted"} and value is False:
                        return "error"
                    if key in {"captchaRequired", "requiresCaptcha", "turnstileRequired"} and value is True:
                        return "captcha_required"
                    if key in {"code", "status", "__typename"} and isinstance(value, str):
                        marker = value.replace("_", "").lower()
                        if marker in {"captcharequired", "turnstilerequired", "invalidcaptcha",
                                      "votecaptcharequired", "voteturnstilerequired"}:
                            return "captcha_required"
                        if marker in {"unauthenticated", "voteunauthenticated"}:
                            return "unauthenticated"
                        if marker in {"error", "voteerror", "failed", "votefailed", "failure"}:
                            return "error"
                    if isinstance(value, (dict, list)):
                        pending.append((value, depth + 1))
            elif isinstance(node, list):
                pending.extend((value, depth + 1) for value in node)
        # Even a valid mutation response does not prove voting succeeded.
        # UI acknowledgement/cooldown confirmation remains mandatory.
        return "usable"
    except (ValueError, TypeError, RecursionError):
        return "invalid"


def inspect_json_response(raw, operation):
    """Inspect a bounded REST/tRPC result with the same application-error rules."""
    if not isinstance(raw, str) or len(raw) > MAX_RESPONSE:
        return "unavailable"
    try:
        data = json.loads(raw)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, (dict, bool)):
            return "invalid"
        if isinstance(data, dict) and data.get("errors"):
            return inspect_response(json.dumps(data), None, operation)
        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                return inspect_response(json.dumps({"errors": [{"message": error.get("message"),
                    "extensions": {"code": error.get("code")}}]}), None, operation)
            return "error"
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            if isinstance(data, dict) and "json" in data:
                data = data["json"]
        return inspect_response(json.dumps({"data": {"result": data}}),
                                hashlib.sha256(b"result").hexdigest(), operation)
    except (ValueError, TypeError, RecursionError):
        return "invalid"
