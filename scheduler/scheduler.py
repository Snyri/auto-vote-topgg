import io
import json
import os
import time
import zipfile
from datetime import datetime, timezone

import requests


GH_TOKEN = os.environ["GH_TOKEN"].strip()
GH_REPOSITORY = os.environ["GH_REPOSITORY"].strip()
GH_REF = os.environ.get("GH_REF", "master").strip()
GH_WORKFLOW = os.environ.get("GH_WORKFLOW", "vote.yml").strip()

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
ERROR_RETRY_SECONDS = int(os.environ.get("ERROR_RETRY_SECONDS", "300"))

API = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_TOKEN}",
    "X-GitHub-Api-Version": "2026-03-10",
    "User-Agent": "northflank-topgg-scheduler",
}

session = requests.Session()
session.headers.update(HEADERS)


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


def latest_vote_run():
    response = api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/workflows/{GH_WORKFLOW}/runs",
        params={
            "branch": GH_REF,
            "per_page": 1,
        },
    )

    runs = response.json().get("workflow_runs", [])
    return runs[0] if runs else None


def get_run(run_id):
    return api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/runs/{run_id}",
    ).json()


def wait_for_run(run_id):
    while True:
        run = get_run(run_id)

        if run.get("status") == "completed":
            log(
                f"Workflow run {run_id} completed: "
                f"{run.get('conclusion')}"
            )
            return run

        log(
            f"Workflow run {run_id} is "
            f"{run.get('status')}; waiting..."
        )

        time.sleep(POLL_SECONDS)


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
                "next-vote artifact does not contain "
                "next-vote.json"
            )

        data = json.loads(
            zf.read(names[0]).decode("utf-8")
        )

    value = data.get("next_vote_at")

    if not isinstance(value, int):
        raise RuntimeError(
            "next-vote.json has invalid next_vote_at"
        )

    return value


def dispatch_vote():
    before = time.time()

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

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    run_id = payload.get("workflow_run_id")

    if isinstance(run_id, int):
        log(f"Dispatched workflow run {run_id}")
        return run_id

    # Fallback per compatibilità con eventuali risposte API
    # che non contengano direttamente l'ID della nuova run.
    deadline = time.time() + 60

    while time.time() < deadline:
        run = latest_vote_run()

        if run and run.get("created_at"):
            created_ts = datetime.fromisoformat(
                run["created_at"].replace("Z", "+00:00")
            ).timestamp()

            if (
                created_ts >= before - 5
                and run.get("event") == "workflow_dispatch"
            ):
                run_id = int(run["id"])
                log(f"Dispatched workflow run {run_id}")
                return run_id

        time.sleep(2)

    raise RuntimeError(
        "Dispatch succeeded but the new workflow "
        "run could not be identified"
    )


def resolve_schedule():
    while True:
        try:
            run = latest_vote_run()

            if run and run.get("status") != "completed":
                run = wait_for_run(int(run["id"]))

            if run:
                next_at = next_vote_at_from_run(
                    int(run["id"])
                )

                if next_at is not None:
                    return next_at

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
                    f"re-resolving in "
                    f"{ERROR_RETRY_SECONDS}s"
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
