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


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


GH_TOKEN = required_env("GH_TOKEN")
GH_REPOSITORY = required_env("GH_REPOSITORY")
GH_REF = os.environ.get("GH_REF", "master").strip() or "master"
GH_WORKFLOW = os.environ.get("GH_WORKFLOW", "vote.yml").strip() or "vote.yml"

POLL_SECONDS = env_int("POLL_SECONDS", 15, 5)
ERROR_RETRY_SECONDS = env_int("ERROR_RETRY_SECONDS", 300, 300)
MAX_RUN_WAIT_SECONDS = env_int("MAX_RUN_WAIT_SECONDS", 2700, 300)
MAX_SCHEDULE_AHEAD_SECONDS = 48 * 60 * 60
MAX_SCHEDULE_PAST_SECONDS = 24 * 60 * 60
MAX_ARTIFACT_BYTES = 1024 * 1024
SCHEDULE_REFRESH_SECONDS = env_int("SCHEDULE_REFRESH_SECONDS", 60, 15)

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


def latest_vote_run(minimum_run_id=None):
    # Do not assume that a single API result is the newest run: a stale
    # result once moved the schedule backwards from #138 to #136.
    runs = list_vote_runs(20)
    valid = [run for run in runs if type(run.get("id")) is int]
    latest = max(valid, key=lambda run: run["id"]) if valid else None
    if minimum_run_id is not None and (
        latest is None or latest["id"] < minimum_run_id
    ):
        # A stale list response must not erase a run already observed by this
        # process, including one dispatched during the previous cycle.
        latest = get_run(minimum_run_id)
        if latest.get("id") != minimum_run_id:
            raise RuntimeError("GitHub returned an unexpected workflow run")
    return latest


def latest_prior_success_schedule(run_id, now=None):
    """Preserve a still-future confirmed schedule after a newer failed run.

    A failed early workflow can produce a short retry artifact, but it must
    not override a future schedule from the most recent successful run.
    """
    now = time.time() if now is None else now
    successful = [
        run for run in list_vote_runs(100)
        if type(run.get("id")) is int
        and run["id"] < run_id
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not successful:
        return None
    newest_success = max(successful, key=lambda run: run["id"])
    scheduled = next_vote_at_from_run(newest_success["id"])
    if scheduled is not None and scheduled > now:
        return newest_success["id"], scheduled
    return None


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
    artifact_size = artifact.get("size_in_bytes")
    if (
        type(artifact_size) is not int
        or artifact_size < 1
        or artifact_size > MAX_ARTIFACT_BYTES
    ):
        raise RuntimeError("next-vote artifact has an invalid size")
    if type(artifact.get("id")) is not int or artifact["id"] < 1:
        raise RuntimeError("next-vote artifact has an invalid id")

    archive = api(
        "GET",
        f"/repos/{GH_REPOSITORY}/actions/artifacts/"
        f"{artifact['id']}/zip",
        stream=True,
    )
    content = bytearray()
    try:
        for chunk in archive.iter_content(chunk_size=65536):
            if len(content) + len(chunk) > MAX_ARTIFACT_BYTES:
                raise RuntimeError("next-vote artifact download is too large")
            content.extend(chunk)
    finally:
        archive.close()

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        if zf.namelist() != ["next-vote.json"]:
            raise RuntimeError(
                "next-vote artifact must contain exactly next-vote.json"
            )
        info = zf.getinfo("next-vote.json")
        if info.file_size > 4096:
            raise RuntimeError("next-vote.json is unexpectedly large")
        data = json.loads(zf.read("next-vote.json").decode("utf-8"))

    if not isinstance(data, dict) or set(data) != {"next_vote_at"}:
        raise RuntimeError("next-vote.json has unexpected fields")
    return validate_next_vote_at(data["next_vote_at"])


def select_new_dispatched_run(
    runs,
    known_ids,
    not_before,
    expected_source=None,
):
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
            if expected_source is not None:
                expected_title = f"Top.gg Auto Vote · {expected_source}"
                if str(run.get("display_title") or "") != expected_title:
                    continue
            candidates.append((created_at, run_id))

    return max(candidates)[1] if candidates else None


def wait_for_new_dispatched_run(
    known_ids,
    not_before,
    timeout_seconds=60,
    expected_source=None,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run_id = select_new_dispatched_run(
            list_vote_runs(20),
            known_ids,
            not_before,
            expected_source=expected_source,
        )
        if run_id is not None:
            return run_id
        time.sleep(2)
    return None


def dispatch_vote():
    existing_runs = list_vote_runs(20)
    active_run = next(
        (run for run in existing_runs if run.get("status") != "completed"),
        None,
    )
    if active_run is not None:
        run_id = int(active_run["id"])
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
                    "recovery_depth": "0",
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
            expected_source="northflank",
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

    run_id = wait_for_new_dispatched_run(
        known_ids,
        before,
        expected_source="northflank",
    )
    if run_id is not None:
        log(f"Dispatched workflow run {run_id}")
        return run_id

    raise RuntimeError(
        "Dispatch succeeded but the new workflow run could not be identified"
    )


def resolve_schedule(minimum_run_id=None):
    while True:
        try:
            run = latest_vote_run(minimum_run_id=minimum_run_id)
            if run:
                minimum_run_id = max(minimum_run_id or 0, int(run["id"]))

            if run and run.get("status") != "completed":
                run = wait_for_run(int(run["id"]))

            if run:
                target = schedule_from_completed_run(run)
                if target is not None:
                    return target

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
            minimum_run_id = max(minimum_run_id or 0, run_id)
            completed_run = wait_for_run(run_id)

            target = schedule_from_completed_run(completed_run)
            if target is not None:
                return target

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


def schedule_from_completed_run(run):
    next_at = next_vote_at_from_run(int(run["id"]))
    if run.get("conclusion") not in {None, "success"}:
        guarded = latest_prior_success_schedule(int(run["id"]))
        if guarded is not None and (next_at is None or guarded[1] > next_at):
            log(
                f"Retaining future schedule from successful run "
                f"{guarded[0]}; failed run {run['id']} cannot "
                f"bring the next dispatch forward"
            )
            # This decision has observed the newer failed run. Retain its ID
            # so subsequent stale lists cannot undo the successful-run guard.
            return int(run["id"]), guarded[1]
    if next_at is None:
        return None
    return int(run["id"]), next_at


def latest_schedule_target(minimum_run_id=None):
    run = latest_vote_run(minimum_run_id=minimum_run_id)
    if not run or run.get("status") != "completed":
        return None
    return schedule_from_completed_run(run)


def wait_until(epoch, source_run_id=None):
    """Return the accepted source and target, refusing older run artifacts."""
    current_epoch = epoch
    current_source = source_run_id
    next_refresh = 0.0

    while True:
        now = time.time()
        remaining = current_epoch - now
        if remaining <= 0:
            return current_source, current_epoch

        monotonic_now = time.monotonic()
        if monotonic_now >= next_refresh:
            next_refresh = monotonic_now + SCHEDULE_REFRESH_SECONDS
            try:
                latest = latest_schedule_target(minimum_run_id=current_source)
                if latest is not None:
                    run_id, refreshed_epoch = latest
                    if current_source is not None and run_id < current_source:
                        log(
                            f"Ignoring stale schedule from run {run_id}; "
                            f"current source is run {current_source}"
                        )
                    else:
                        current_source = run_id
                        if refreshed_epoch != current_epoch:
                            old_target = datetime.fromtimestamp(
                                current_epoch, timezone.utc
                            ).isoformat(timespec="seconds")
                            new_target = datetime.fromtimestamp(
                                refreshed_epoch, timezone.utc
                            ).isoformat(timespec="seconds")
                            log(
                                f"Schedule refreshed from run {run_id}: "
                                f"{old_target} -> {new_target}"
                            )
                            current_epoch = refreshed_epoch
                            continue
            except Exception as exc:
                log(
                    f"Schedule refresh error: "
                    f"{type(exc).__name__}: {exc}"
                )

        if remaining > 300:
            target = datetime.fromtimestamp(
                current_epoch,
                timezone.utc,
            ).isoformat(timespec="seconds")
            log(
                f"Next vote scheduled for {target}; "
                f"{int(remaining)}s remaining"
            )

        sleep_for = min(
            max(current_epoch - time.time(), 0),
            SCHEDULE_REFRESH_SECONDS,
        )
        if sleep_for > 0:
            time.sleep(sleep_for)


def main():
    log(
        f"Scheduler started for "
        f"{GH_REPOSITORY}/{GH_WORKFLOW} "
        f"on {GH_REF}"
    )

    schedule = None
    minimum_run_id = None
    while True:
        if schedule is None:
            schedule = resolve_schedule(minimum_run_id=minimum_run_id)
        schedule_run_id, next_at = schedule
        target = datetime.fromtimestamp(
            next_at,
            timezone.utc,
        ).isoformat(timespec="seconds")
        log(f"Next vote target: {target} (source run {schedule_run_id})")

        schedule_run_id, next_at = wait_until(
            next_at, source_run_id=schedule_run_id
        )
        minimum_run_id = max(minimum_run_id or 0, schedule_run_id)

        try:
            # Close the race between the last refresh and the dispatch.
            # An updated successful schedule must supersede a now-stale timer.
            latest = latest_schedule_target(minimum_run_id=minimum_run_id)
            if (
                latest is not None
                and latest[0] >= schedule_run_id
                and latest[1] > time.time() + 1
            ):
                log(
                    f"Skipping dispatch: run {latest[0]} has a newer "
                    f"future target"
                )
                schedule = latest
                continue
            log(
                "Vote target reached; "
                "dispatching GitHub workflow"
            )
            run_id = dispatch_vote()
            minimum_run_id = max(minimum_run_id, run_id)
            completed_run = wait_for_run(run_id)

            new_schedule = schedule_from_completed_run(completed_run)
            if new_schedule is None:
                log(
                    f"No next-vote artifact; "
                    f"re-resolving in {ERROR_RETRY_SECONDS}s"
                )
                time.sleep(ERROR_RETRY_SECONDS)
                schedule = None
            else:
                target = datetime.fromtimestamp(
                    new_schedule[1],
                    timezone.utc,
                ).isoformat(timespec="seconds")
                log(
                    f"New next-vote artifact received: "
                    f"{target}"
                )
                # Carry this verified target directly into the next cycle.
                # Re-reading a stale list here used to cause duplicate votes.
                schedule = (
                    new_schedule if run_id >= schedule_run_id else None
                )

        except Exception as exc:
            log(
                f"Vote dispatch cycle failed: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(ERROR_RETRY_SECONDS)
            schedule = None


if __name__ == "__main__":
    main()
