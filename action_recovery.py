"""Recover vote state and dispatch a correlated fresh Action after failure.

Uses only the standard library so retrying does not depend on browser packages.
"""

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from recovery_state import MAX_STATE_BYTES, validate_state


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository, self.token = repository, token
        self.opener = urllib.request.build_opener(NoRedirect)
        self.deadline = None

    def timeout(self):
        remaining = 15 if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Recovery read budget exhausted")
        return min(15, remaining)

    def request(self, path, payload=None, limit=1024 * 1024):
        request = urllib.request.Request("https://api.github.com/repos/" + self.repository + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        with self.opener.open(request, timeout=self.timeout()) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise RuntimeError("GitHub response exceeds limit")
        return json.loads(content) if content else None

    def runs(self):
        return self.request("/actions/workflows/vote.yml/runs?branch=master&per_page=100")["workflow_runs"]

    def state(self, run_id):
        run = self.request(f"/actions/runs/{run_id}")
        if run.get("head_branch") != "master" or run.get("path") != ".github/workflows/vote.yml" or run.get("event") != "workflow_dispatch":
            raise RuntimeError("Recovery source is not a trusted master vote run")
        artifacts = self.request(f"/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
        candidates = [item for item in artifacts if item.get("name") == "vote-recovery" and not item.get("expired")]
        if not candidates:
            return None
        artifact = max(candidates, key=lambda item: item["id"])
        if type(artifact.get("size_in_bytes")) is not int or not 0 < artifact["size_in_bytes"] <= 1024 * 1024:
            raise RuntimeError("Invalid recovery artifact size")
        try:
            self.request(f"/actions/artifacts/{int(artifact['id'])}/zip")
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
            location = exc.headers.get("Location", "")
        else:
            raise RuntimeError("Missing artifact download redirect")
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("Invalid artifact download redirect")
        # Signed storage URLs receive no GitHub credential and no further redirect.
        with self.opener.open(urllib.request.Request(location), timeout=self.timeout()) as response:
            archive = response.read(1024 * 1024 + 1)
        if len(archive) > 1024 * 1024:
            raise RuntimeError("Recovery artifact download exceeds limit")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            if bundle.namelist() != ["vote-recovery.json"] or bundle.getinfo("vote-recovery.json").file_size > MAX_STATE_BYTES:
                raise RuntimeError("Invalid recovery artifact contents")
            return validate_state(json.loads(bundle.read("vote-recovery.json")))


def restore(client, current_run, origin, path, *, clock=time.monotonic, sleep=time.sleep, budget=120):
    if origin and not re.fullmatch(r"[0-9]+", origin):
        raise ValueError("Invalid recovery origin")
    deadline = clock() + budget

    def read(operation):
        for attempt in range(5):
            if clock() >= deadline:
                raise TimeoutError("Recovery read budget exhausted")
            try:
                return operation()
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                # Retry reads, never dispatch POSTs or invalid/untrusted state.
                if isinstance(exc, urllib.error.HTTPError) and not (exc.code == 429 or 500 <= exc.code < 600):
                    raise
                if attempt == 4:
                    raise
                delay = 2 ** attempt
                if isinstance(exc, urllib.error.HTTPError):
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    if retry_after.isdigit():
                        delay = max(delay, int(retry_after))
                if delay >= deadline - clock():
                    raise TimeoutError("Recovery read budget exhausted") from exc
                print(f"Recovery read temporarily unavailable ({type(exc).__name__}); retry {attempt + 2}/5")
                sleep(delay)

    previous_deadline = client.deadline if isinstance(client, GitHub) else None
    if isinstance(client, GitHub):
        client.deadline = time.monotonic() + budget
    try:
        candidates = [int(origin)] if origin else []
        candidates += sorted((run["id"] for run in read(client.runs)
                              if type(run.get("id")) is int and run["id"] < current_run
                              and run.get("status") == "completed"), reverse=True)[:30]
        for run_id in sorted(set(candidates), reverse=True):
            data = read(lambda: client.state(run_id))
            if data is not None:
                data = validate_state(data)
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                temporary.replace(target)
                print(f"Recovered vote handoff from run {run_id}")
                return
        print("No previous recovery artifact; starting with fresh state")
    finally:
        if isinstance(client, GitHub):
            client.deadline = previous_deadline


def dispatch_retry(client, origin, *, clock=time.monotonic, sleep=time.sleep, budget=210):
    title = f"Top.gg Auto Vote · failure-retry · {origin}"
    deadline, acknowledged = clock() + budget, False
    while clock() < deadline:
        try:
            existing = [run for run in client.runs() if run.get("display_title") == title]
            if existing:
                print(f"Fresh retry already exists as run {max(existing, key=lambda run: run['id'])['id']}")
                return
            if not acknowledged:
                try:
                    client.request("/actions/workflows/vote.yml/dispatches", {
                        "ref": "master", "inputs": {"source": "failure-retry", "origin_run_id": str(origin)}})
                    acknowledged = True
                except (OSError, TimeoutError, urllib.error.URLError) as exc:
                    # POST may have been accepted. Observe before another POST.
                    print("Dispatch response uncertain (" + type(exc).__name__ + "); checking for the correlated run")
                    observe_until = min(deadline, clock() + 30)
                    while clock() < observe_until:
                        sleep(2)
                        if any(run.get("display_title") == title for run in client.runs()):
                            return
            sleep(2)
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            print("Retry dispatch check unavailable (" + type(exc).__name__ + ")")
            sleep(3)
    if acknowledged:
        print("Fresh retry accepted by GitHub; listing has not caught up")
        return
    raise RuntimeError("Could not dispatch fresh retry; scheduler will recover the failed run")


def main():
    client = GitHub(os.environ["REPOSITORY"], os.environ["GH_TOKEN"])
    run_id = int(os.environ["RUN_ID"])
    if os.environ.get("RECOVERY_MODE") == "restore":
        restore(client, run_id, os.environ.get("RUN_ORIGIN_ID", ""), os.environ["RECOVERY_STATE_FILE"])
    else:
        dispatch_retry(client, run_id)


if __name__ == "__main__":
    main()
