"""Credential-free, atomic handoff of votes between fresh workflow runs."""

import hashlib
import json
import re
import time
from pathlib import Path

MAX_STATE_BYTES = 65536
MAX_RECORDS = 256
PENDING_LIFETIME = 12 * 3600 + 30


def configuration_scope(tokens, bot_ids):
    accounts = sorted(hashlib.sha256(token.encode()).hexdigest() for token in tokens)
    return hashlib.sha256(json.dumps([accounts, sorted(bot_ids)], separators=(",", ":")).encode()).hexdigest()


def validate_state(data, now=None):
    now = int(time.time()) if now is None else int(now)
    if (not isinstance(data, dict) or set(data) != {"version", "scope", "records"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["scope"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["scope"])
            or not isinstance(data["records"], dict) or len(data["records"]) > MAX_RECORDS):
        raise ValueError("Invalid vote recovery state")
    for key, record in data["records"].items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                or not isinstance(record, dict) or set(record) != {"kind", "until"}
                or record["kind"] not in {"pending", "complete"}
                or type(record["until"]) is not int or not now - 172800 <= record["until"] <= now + 172800):
            raise ValueError("Invalid vote recovery record")
    return data


class SubmissionJournal:
    def __init__(self, path, tokens, bot_ids, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.scope = configuration_scope(tokens, bot_ids)
        self.records, self.active_key = {}, None
        if self.path.exists():
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("Vote recovery state is too large")
            data = validate_state(json.loads(self.path.read_text(encoding="utf-8")), self.clock())
            allowed = {hashlib.sha256((token + "\0" + bot_id).encode()).hexdigest()
                       for token in tokens for bot_id in bot_ids}
            self.records = {key: dict(value) for key, value in data["records"].items()
                            if key in allowed and value["until"] > self.clock()}
        self.save()

    def select(self, token, bot_id):
        self.active_key = hashlib.sha256((token + "\0" + bot_id).encode()).hexdigest()
        return self.records.get(self.active_key)

    def save(self):
        data = {"version": 1, "scope": self.scope, "records": self.records}
        validate_state(data, self.clock())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.path)

    def before_press(self):
        if self.active_key is None:
            raise RuntimeError("Vote recovery target was not selected")
        self.records[self.active_key] = {"kind": "pending", "until": int(self.clock()) + PENDING_LIFETIME}
        self.save()

    def record_result(self, result):
        if result.get("status") in {"success", "cooldown"}:
            self.records[self.active_key] = {"kind": "complete", "until": int(result["retry_at"])}
        elif result.get("submission_rejected") is True or result.get("vote_submitted") is False:
            self.records.pop(self.active_key, None)
        self.save()

    def clear_current(self):
        self.records.pop(self.active_key, None)
        self.save()
