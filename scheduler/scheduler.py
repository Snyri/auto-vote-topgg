import io
import json
import os
import time
import zipfile
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def env_int(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    return max(value, minimum)


GH_TOKEN = os.environ["GH_TOKEN"].strip()
GH_REPOSITORY = os.environ["GH_REPOSITORY"].strip()
GH_REF = os.environ.get("GH_REF", "master").strip()
GH_WORKFLOW = os.environ.get("GH_WORKFLOW", "vote.yml").strip()

POLL_SECONDS = env_int("POLL_SECONDS", 15, 5)
ERROR_RETRY_SECONDS = env_int("ERROR_RETRY_SECONDS", 300, 300)
MAX_RUN_WAIT_SECONDS = env_int("MAX_RUN_WAIT_SECONDS", 2700, 300)
MAX_SCHEDULE_AHEAD_SECONDS = 48 * 60 * 60
MAX_SCHEDULE_PAST_SECONDS = 24 * 60 * 60

API = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "northflank-topgg-scheduler",
}

session = requests.Session()
session.headers.update(HEADERS)
read_retry = Retry(
    total=4,
    connect=4,
    read=4,
    status=4,
    backoff_factor=1,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
)
session.mount("https://", HTTPAdapter(max_retries=read_retry))


def log(message):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{now}] {message}", flush=True)


def api(method, path, **kwargs):
    response = session.request(
        method,
        f"{API}{path}",
        timeout=30,
        **kwargs,
    )
    response.raise_for_status()
    return response


def list_vote_runs(per_page=20):
    response = api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/workflows/{GH_WORKFLOW}/runs",
        params={
            "branch": GH_REF,
            "per_page": per_page,
        },
    )
    return response.json().get("workflow_runs", [])


def latest_vote_run():
    runs = list_vote_runs(1)
    return runs[0] if runs else None


def get_run(run_id):
    return api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/runs/{run_id}",
    ).json()


def wait_for_run(run_id, timeout_seconds=None):
    timeout_seconds = timeout_seconds or MAX_RUN_WAIT_SECONDS
    deadline = time.monotonic() + timeout_seconds

    while True:
        run = get_run(run_id)

        if run.get("status") == "completed":
            log(
                f"Workflow run {run_id} completed: "
                f"{run.get('conclusion')}"
            )
            return run

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Workflow run {run_id} exceeded "
                f"{timeout_seconds}s wait limit"
            )

        log(
            f"Workflow run {run_id} is "
            f"{run.get('status')}; waiting..."
        )
        time.sleep(POLL_SECONDS)


def validate_next_vote_at(value, now=None):
    if type(value) is not int:
        raise RuntimeError("next-vote.json has invalid next_vote_at")

    current = int(time.time() if now is None else now)
    if value < current - MAX_SCHEDULE_PAST_SECONDS:
        raise RuntimeError("next_vote_at is too far in the past")
    if value > current + MAX_SCHEDULE_AHEAD_SECONDS:
        raise RuntimeError("next_vote_at is too far in the future")
    return value


def next_vote_at_from_run(run_id):
    response = api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/runs/{run_id}/artifacts",
        params={"per_page": 100},
    )

    artifacts = [
        artifact
        for artifact in response.json().get("artifacts", [])
        if artifact.get("name") == "next-vote"
        and not artifact.get("expired")
    ]

    if not artifacts:
        return None

    artifact = sorted(
        artifacts,
        key=lambda item: item.get("created_at", ""),
    )[-1]

    archive = api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/artifacts/"
        f"{artifact['id']}/zip",
    )

    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        names = [
            name
            for name in zf.namelist()
            if name.endswith("next-vote.json")
        ]
        if not names:
            raise RuntimeError(
                "next-vote artifact does not contain next-vote.json"
            )
        data = json.loads(zf.read(names[0]).decode("utf-8"))

    return validate_next_vote_at(data.get("next_vote_at"))


def select_new_dispatched_run(runs, known_ids, not_before):
    candidates = []
    for run in runs:
        try:
            run_id = int(run["id"])
            created_at = datetime.fromisoformat(
                str(run["created_at"]).replace("Z", "+00:00")
            ).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if (
            run_id not in known_ids
            and run.get("event") == "workflow_dispatch"
            and created_at >= not_before - 5
        ):
            candidates.append((created_at, run_id))

    return max(candidates)[1] if candidates else None


def wait_for_new_dispatched_run(known_ids, not_before, timeout_seconds=60):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run_id = select_new_dispatched_run(
            list_vote_runs(20),
            known_ids,
            not_before,
        )
        if run_id is not None:
            return run_id
        time.sleep(2)
    return None


def dispatch_vote():
    existing_runs = list_vote_runs(20)
    if existing_runs and existing_runs[0].get("status") != "completed":
        run_id = int(existing_runs[0]["id"])
        log(f"Vote workflow already active as run {run_id}; reusing it")
        return run_id

    known_ids = {
        int(run["id"])
        for run in existing_runs
        if isinstance(run.get("id"), int)
    }
    before = time.time()

    try:
        response = api(
            "POST",
            f"/repos/{GH_REPOSITORY}/actions/workflows/"
            f"{GH_WORKFLOW}/dispatches",
            json={
                "ref": GH_REF,
                "inputs": {
                    "source": "northflank",
                    "origin_run_id": "",
                },
            },
        )
    except requests.RequestException:
        log(
            "Workflow dispatch response was uncertain; "
            "checking for a newly-created run before retrying"
        )
        run_id = wait_for_new_dispatched_run(
            known_ids,
            before,
            timeout_seconds=30,
        )
        if run_id is not None:
            log(f"Recovered dispatched workflow run {run_id}")
            return run_id
        raise

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    run_id = payload.get("workflow_run_id")
    if isinstance(run_id, int):
        log(f"Dispatched workflow run {run_id}")
        return run_id

    run_id = wait_for_new_dispatched_run(known_ids, before)
    if run_id is not None:
        log(f"Dispatched workflow run {run_id}")
        return run_id

    raise RuntimeError(
        "Dispatch succeeded but the new workflow run could not be identified"
    )


def resolve_schedule():
    while True:
        try:
            run = latest_vote_run()

            if run and run.get("status") != "completed":
                run = wait_for_run(int(run["id"]))

            if run:
                next_at = next_vote_at_from_run(int(run["id"]))
                if next_at is not None:
                    return next_at

                if run.get("conclusion") not in {"success", "neutral", "skipped"}:
                    log(
                        f"Latest run {run.get('id')} ended "
                        f"{run.get('conclusion')} without schedule; "
                        f"waiting {ERROR_RETRY_SECONDS}s before another dispatch"
                    )
                    time.sleep(ERROR_RETRY_SECONDS)

            log(
                "No usable next-vote artifact. "
                "Running one bootstrap vote check."
            )

            run_id = dispatch_vote()
            wait_for_run(run_id)

            next_at = next_vote_at_from_run(run_id)
            if next_at is not None:
                return next_at

            log(
                f"No next-vote artifact; retrying in "
                f"{ERROR_RETRY_SECONDS}s."
            )
            time.sleep(ERROR_RETRY_SECONDS)

        except Exception as exc:
            log(
                f"Scheduler error: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(ERROR_RETRY_SECONDS)


def wait_until(epoch):
    while True:
        remaining = epoch - time.time()
        if remaining <= 0:
            return

        if remaining > 300:
            target = datetime.fromtimestamp(
                epoch,
                timezone.utc,
            ).isoformat(timespec="seconds")
            log(
                f"Next vote scheduled for {target}; "
                f"{int(remaining)}s remaining"
            )
            time.sleep(min(remaining, 300))
        else:
            time.sleep(remaining)


def main():
    log(
        f"Scheduler started for "
        f"{GH_REPOSITORY}/{GH_WORKFLOW} "
        f"on {GH_REF}"
    )

    while True:
        next_at = resolve_schedule()
        target = datetime.fromtimestamp(
            next_at,
            timezone.utc,
        ).isoformat(timespec="seconds")
        log(f"Next vote target: {target}")

        wait_until(next_at)

        try:
            log(
                "Vote target reached; "
                "dispatching GitHub workflow"
            )
            run_id = dispatch_vote()
            wait_for_run(run_id)

            new_next_at = next_vote_at_from_run(run_id)
            if new_next_at is None:
                log(
                    f"No next-vote artifact; "
                    f"re-resolving in {ERROR_RETRY_SECONDS}s"
                )
                time.sleep(ERROR_RETRY_SECONDS)
            else:
                target = datetime.fromtimestamp(
                    new_next_at,
                    timezone.utc,
                ).isoformat(timespec="seconds")
                log(
                    f"New next-vote artifact received: "
                    f"{target}"
                )

        except Exception as exc:
            log(
                f"Vote dispatch cycle failed: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(ERROR_RETRY_SECONDS)


if __name__ == "__main__":
    main()
