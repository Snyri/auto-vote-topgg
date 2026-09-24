import importlib.util
import io
import json
import os
import pathlib
import unittest
import zipfile
from unittest.mock import MagicMock, patch

os.environ.setdefault("GH_TOKEN", "test-token")
os.environ.setdefault("GH_REPOSITORY", "owner/repo")

MODULE_PATH = pathlib.Path(__file__).with_name("scheduler") / "scheduler.py"
SPEC = importlib.util.spec_from_file_location("scheduler_under_test", MODULE_PATH)
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


class EnvironmentValidationTests(unittest.TestCase):
    def test_required_env_rejects_missing_or_blank_values(self):
        with patch.dict(os.environ, {"EXAMPLE_REQUIRED": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "EXAMPLE_REQUIRED is required"):
                scheduler.required_env("EXAMPLE_REQUIRED")


class SchedulerValidationTests(unittest.TestCase):
    def test_validate_next_vote_at_accepts_reasonable_timestamp(self):
        now = 2_000_000_000
        self.assertEqual(
            scheduler.validate_next_vote_at(now + 3600, now),
            now + 3600,
        )

    def test_validate_next_vote_at_rejects_non_integer_and_extreme_future(self):
        now = 2_000_000_000
        with self.assertRaises(RuntimeError):
            scheduler.validate_next_vote_at("bad", now)
        with self.assertRaises(RuntimeError):
            scheduler.validate_next_vote_at(
                now + scheduler.MAX_SCHEDULE_AHEAD_SECONDS + 1,
                now,
            )

    def test_select_new_dispatched_run_ignores_preexisting_ids(self):
        runs = [
            {
                "id": 12,
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:30Z",
            },
            {
                "id": 11,
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:20Z",
            },
        ]
        not_before = 2_000_000_000

        selected = scheduler.select_new_dispatched_run(
            runs,
            {11},
            not_before,
        )

        self.assertEqual(selected, 12)

    @patch.object(scheduler, "list_vote_runs")
    @patch.object(scheduler, "api")
    def test_dispatch_reuses_active_run_without_duplicate_post(self, api, list_runs):
        list_runs.return_value = [{
            "id": 99,
            "status": "in_progress",
            "event": "workflow_dispatch",
            "created_at": "2033-05-18T03:33:20Z",
        }]

        self.assertEqual(scheduler.dispatch_vote(), 99)
        api.assert_not_called()


    @patch.object(scheduler, "wait_for_new_dispatched_run", return_value=101)
    @patch.object(scheduler, "list_vote_runs", return_value=[])
    @patch.object(scheduler, "api", side_effect=scheduler.requests.Timeout("uncertain"))
    def test_dispatch_recovers_ambiguous_post_without_duplicate(
        self, api, _list_runs, wait_new
    ):
        self.assertEqual(scheduler.dispatch_vote(), 101)
        self.assertEqual(api.call_count, 1)
        wait_new.assert_called_once()

    @patch.object(scheduler.time, "sleep")
    @patch.object(scheduler, "list_vote_runs")
    def test_wait_for_new_dispatched_run_polls_until_visible(self, list_runs, _sleep):
        list_runs.side_effect = [
            [],
            [{
                "id": 12,
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:30Z",
            }],
        ]
        with patch.object(
            scheduler.time,
            "monotonic",
            side_effect=[0.0, 0.0, 1.0, 1.0],
        ):
            self.assertEqual(
                scheduler.wait_for_new_dispatched_run(set(), 2_000_000_000, 10),
                12,
            )


class ScheduleRefreshTests(unittest.TestCase):
    @patch.object(scheduler.time, "sleep")
    @patch.object(scheduler, "latest_schedule_target")
    def test_wait_until_refreshes_after_newer_manual_run(self, latest_target, sleep):
        old_target = 2_000_000_600
        new_target = 2_000_001_100
        latest_target.side_effect = [(133, new_target), (133, new_target)]
        with (
            patch.object(
                scheduler.time,
                "time",
                side_effect=[
                    2_000_000_000,
                    2_000_000_000,
                    2_000_000_000,
                    2_000_001_101,
                ],
            ),
            patch.object(
                scheduler.time,
                "monotonic",
                side_effect=[0.0, 61.0],
            ),
        ):
            result = scheduler.wait_until(old_target)

        self.assertEqual(result, (133, new_target))

    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_000_500)
    @patch.object(scheduler, "latest_vote_run")
    def test_latest_schedule_target_uses_latest_completed_run(
        self, latest_run, next_vote
    ):
        latest_run.return_value = {"id": 133, "status": "completed"}

        self.assertEqual(
            scheduler.latest_schedule_target(),
            (133, 2_000_000_500),
        )
        next_vote.assert_called_once_with(133)


    @patch.object(scheduler, "list_vote_runs")
    def test_latest_run_uses_max_id_not_stale_first_result(self, list_runs):
        list_runs.return_value = [
            {"id": 136, "status": "completed"},
            {"id": 138, "status": "completed"},
            {"id": 137, "status": "completed"},
        ]
        self.assertEqual(scheduler.latest_vote_run()["id"], 138)
        list_runs.assert_called_once_with(20)

    @patch.object(scheduler, "get_run", return_value={"id": 138, "status": "completed"})
    @patch.object(scheduler, "list_vote_runs", return_value=[{"id": 136}])
    def test_latest_run_refetches_known_run_when_list_regresses(self, list_runs, get_run):
        self.assertEqual(scheduler.latest_vote_run(minimum_run_id=138)["id"], 138)
        get_run.assert_called_once_with(138)

    @patch.object(scheduler, "get_run", return_value={"id": 138, "status": "completed"})
    @patch.object(scheduler, "list_vote_runs", return_value=[])
    def test_empty_list_cannot_erase_known_run(self, list_runs, get_run):
        self.assertEqual(scheduler.latest_vote_run(minimum_run_id=138)["id"], 138)
        get_run.assert_called_once_with(138)

    @patch.object(scheduler, "get_run")
    @patch.object(scheduler, "list_vote_runs", return_value=[{"id": 140}])
    def test_newer_run_supersedes_known_run_without_extra_fetch(self, list_runs, get_run):
        self.assertEqual(scheduler.latest_vote_run(minimum_run_id=138)["id"], 140)
        get_run.assert_not_called()

    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_040_000)
    @patch.object(scheduler, "list_vote_runs")
    def test_failed_run_cannot_advance_still_future_success_schedule(
        self, list_runs, artifact
    ):
        list_runs.return_value = [
            {"id": 140, "status": "completed", "conclusion": "failure"},
            {"id": 136, "status": "completed", "conclusion": "success"},
            {"id": 138, "status": "completed", "conclusion": "success"},
        ]
        with patch.object(scheduler.time, "time", return_value=2_000_000_000):
            target = scheduler.latest_prior_success_schedule(140)
        self.assertEqual(target, (138, 2_000_040_000))
        artifact.assert_called_once_with(138)

    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_000_000)
    @patch.object(scheduler, "list_vote_runs")
    def test_prior_success_guard_expires_at_original_target(self, list_runs, artifact):
        list_runs.return_value = [
            {"id": 138, "status": "completed", "conclusion": "success"},
        ]
        self.assertIsNone(scheduler.latest_prior_success_schedule(140, 2_000_000_001))
        artifact.assert_called_once_with(138)

    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_040_000)
    def test_prior_success_search_covers_retained_history_after_many_failures(self, artifact):
        history = [
            {"id": run_id, "status": "completed", "conclusion": "failure"}
            for run_id in range(160, 138, -1)
        ] + [{"id": 138, "status": "completed", "conclusion": "success"}]
        with patch.object(scheduler, "list_vote_runs", side_effect=lambda per_page: history[:per_page]):
            self.assertEqual(
                scheduler.latest_prior_success_schedule(161, now=2_000_000_000),
                (138, 2_000_040_000),
            )

    @patch.object(scheduler, "latest_prior_success_schedule", return_value=(138, 2_000_040_000))
    @patch.object(scheduler, "latest_vote_run", return_value={
        "id": 140, "status": "completed", "conclusion": "failure"
    })
    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_000_500)
    def test_refresh_ignores_failed_retry_before_prior_success_cooldown(
        self, artifact, latest_run, prior_success
    ):
        self.assertEqual(scheduler.latest_schedule_target(), (140, 2_000_040_000))
        artifact.assert_called_once_with(140)
        prior_success.assert_called_once_with(140)

    @patch.object(scheduler, "latest_prior_success_schedule", return_value=(138, 2_000_040_000))
    @patch.object(scheduler, "latest_vote_run", return_value={
        "id": 140, "status": "completed", "conclusion": "failure"
    })
    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_000_500)
    def test_restart_retains_success_target_after_short_failed_retry(
        self, artifact, latest_run, prior_success
    ):
        with patch.object(scheduler, "log"):
            self.assertEqual(scheduler.resolve_schedule(), (140, 2_000_040_000))
        artifact.assert_called_once_with(140)
        prior_success.assert_called_once_with(140)

    @patch.object(scheduler, "latest_prior_success_schedule", return_value=(138, 2_000_040_000))
    @patch.object(scheduler, "latest_vote_run", return_value={
        "id": 140, "status": "completed", "conclusion": "failure"
    })
    @patch.object(scheduler, "next_vote_at_from_run", return_value=2_000_050_000)
    def test_prior_success_does_not_shorten_newer_failure_deferral(
        self, artifact, latest_run, prior_success
    ):
        self.assertEqual(scheduler.latest_schedule_target(), (140, 2_000_050_000))

    @patch.object(scheduler, "latest_prior_success_schedule", return_value=(138, 2_000_040_000))
    @patch.object(scheduler, "latest_vote_run", return_value={
        "id": 140, "status": "completed", "conclusion": "failure"
    })
    @patch.object(scheduler, "next_vote_at_from_run", return_value=None)
    def test_prior_success_survives_failure_without_artifact(
        self, artifact, latest_run, prior_success
    ):
        self.assertEqual(scheduler.latest_schedule_target(), (140, 2_000_040_000))

    @patch.object(scheduler, "latest_schedule_target", return_value=(136, 2_000_000_100))
    def test_wait_ignores_older_run_even_if_its_target_is_past(self, refresh):
        clock = [2_000_000_000]
        target = 2_000_000_120
        with (
            patch.object(scheduler.time, "time", side_effect=lambda: clock[0]),
            patch.object(scheduler.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                scheduler.time, "sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
            ),
            patch.object(scheduler, "log") as logs,
        ):
            self.assertEqual(
                scheduler.wait_until(target, source_run_id=138), (138, target)
            )
        self.assertGreaterEqual(clock[0], target)
        self.assertTrue(
            any("Ignoring stale schedule" in str(call) for call in logs.call_args_list)
        )

    def test_refresh_waits_between_downloads_after_target_changes(self):
        clock = [2_000_000_000]
        with (
            patch.object(scheduler.time, "time", side_effect=lambda: clock[0]),
            patch.object(scheduler.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(scheduler.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
            patch.object(scheduler, "latest_schedule_target", return_value=(140, 2_000_000_120)) as refresh,
            patch.object(scheduler, "log"),
        ):
            self.assertEqual(scheduler.wait_until(2_000_000_100, 138), (140, 2_000_000_120))
        self.assertEqual(refresh.call_count, 2)


class SchedulerCycleTests(unittest.TestCase):
    def test_dispatched_failure_is_guarded_before_carrying_schedule_forward(self):
        with (
            patch.object(scheduler, "resolve_schedule", return_value=(137, 100)),
            patch.object(scheduler, "wait_until", side_effect=[(137, 100), KeyboardInterrupt]) as wait,
            patch.object(scheduler, "latest_schedule_target", return_value=None),
            patch.object(scheduler, "dispatch_vote", return_value=140),
            patch.object(scheduler, "wait_for_run", return_value={"id": 140, "conclusion": "failure"}),
            patch.object(scheduler, "next_vote_at_from_run", return_value=200),
            patch.object(scheduler, "latest_prior_success_schedule", return_value=(138, 500)),
            patch.object(scheduler, "log"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()
        self.assertEqual(wait.call_args_list[1].args, (500,))
        self.assertEqual(wait.call_args_list[1].kwargs, {"source_run_id": 140})

    def test_cycle_retains_dispatched_schedule_without_reading_stale_list_again(self):
        with (
            patch.object(scheduler, "resolve_schedule", return_value=(138, 100)) as resolve,
            patch.object(scheduler, "wait_until", side_effect=[(138, 100), KeyboardInterrupt]) as wait,
            patch.object(scheduler, "latest_schedule_target", return_value=(138, 100)),
            patch.object(scheduler, "dispatch_vote", return_value=140),
            patch.object(scheduler, "wait_for_run", return_value={"id": 140, "conclusion": "success"}),
            patch.object(scheduler, "next_vote_at_from_run", return_value=500),
            patch.object(scheduler.time, "time", return_value=101),
            patch.object(scheduler, "log"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()
        resolve.assert_called_once()
        self.assertEqual(wait.call_args_list[1].args, (500,))
        self.assertEqual(wait.call_args_list[1].kwargs, {"source_run_id": 140})

    def test_final_check_uses_source_accepted_during_wait(self):
        with (
            patch.object(scheduler, "resolve_schedule", return_value=(138, 100)),
            patch.object(scheduler, "wait_until", return_value=(140, 120)),
            patch.object(scheduler, "latest_schedule_target", return_value=(139, 500)) as latest,
            patch.object(scheduler, "dispatch_vote", side_effect=KeyboardInterrupt) as dispatch,
            patch.object(scheduler.time, "time", return_value=121),
            patch.object(scheduler, "log"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()
        latest.assert_called_once_with(minimum_run_id=140)
        dispatch.assert_called_once()

    def test_missing_new_artifact_preserves_run_floor_when_resolving(self):
        with (
            patch.object(scheduler, "resolve_schedule", side_effect=[(138, 100), KeyboardInterrupt]) as resolve,
            patch.object(scheduler, "wait_until", return_value=(138, 100)),
            patch.object(scheduler, "latest_schedule_target", return_value=None),
            patch.object(scheduler, "dispatch_vote", return_value=140),
            patch.object(scheduler, "wait_for_run", return_value={"id": 140, "conclusion": "success"}),
            patch.object(scheduler, "next_vote_at_from_run", return_value=None),
            patch.object(scheduler.time, "sleep"),
            patch.object(scheduler, "log"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()
        self.assertEqual(resolve.call_args_list[1].kwargs, {"minimum_run_id": 140})


class ArtifactValidationTests(unittest.TestCase):
    def artifact_responses(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, value in entries:
                zf.writestr(name, value)
        content = buffer.getvalue()
        listing = MagicMock()
        listing.json.return_value = {"artifacts": [{
            "id": 1,
            "name": "next-vote",
            "expired": False,
            "created_at": "2033-05-18T03:33:20Z",
            "size_in_bytes": len(content),
        }]}
        archive = MagicMock()
        archive.iter_content.return_value = [content]
        return listing, archive

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_accepts_only_schedule_and_closes_download(self, api):
        listing, archive = self.artifact_responses([
            ("next-vote.json", json.dumps({"next_vote_at": 2_000_000_100})),
        ])
        api.side_effect = [listing, archive]
        with patch.object(scheduler.time, "time", return_value=2_000_000_000):
            self.assertEqual(scheduler.next_vote_at_from_run(123), 2_000_000_100)
        self.assertTrue(api.call_args.kwargs["stream"])
        archive.close.assert_called_once()

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_rejects_extra_archive_entries(self, api):
        api.side_effect = self.artifact_responses([
            ("next-vote.json", json.dumps({"next_vote_at": 2_000_000_100})),
            ("unexpected.txt", "ignored previously"),
        ])
        with self.assertRaisesRegex(RuntimeError, "exactly next-vote.json"):
            scheduler.next_vote_at_from_run(123)

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_bounds_actual_download_even_if_metadata_is_small(self, api):
        listing, archive = self.artifact_responses([
            ("next-vote.json", json.dumps({"next_vote_at": 2_000_000_100})),
        ])
        archive.iter_content.return_value = [b"a" * 65536] * 17
        api.side_effect = [listing, archive]
        with self.assertRaisesRegex(RuntimeError, "download is too large"):
            scheduler.next_vote_at_from_run(123)
        archive.close.assert_called_once()

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_rejects_oversized_uncompressed_json(self, api):
        api.side_effect = self.artifact_responses([
            ("next-vote.json", " " * 4097),
        ])
        with self.assertRaisesRegex(RuntimeError, "unexpectedly large"):
            scheduler.next_vote_at_from_run(123)

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_rejects_unexpected_json_fields(self, api):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(
                "next-vote.json",
                json.dumps({"next_vote_at": 2_000_000_100, "extra": True}),
            )
        listing = unittest.mock.MagicMock()
        listing.json.return_value = {
            "artifacts": [{
                "id": 1,
                "name": "next-vote",
                "expired": False,
                "created_at": "2033-05-18T03:33:20Z",
                "size_in_bytes": len(buffer.getvalue()),
            }]
        }
        archive = unittest.mock.MagicMock(content=buffer.getvalue())
        archive.iter_content.return_value = [buffer.getvalue()]
        api.side_effect = [listing, archive]

        with patch.object(scheduler.time, "time", return_value=2_000_000_000):
            with self.assertRaisesRegex(RuntimeError, "unexpected fields"):
                scheduler.next_vote_at_from_run(123)

    @patch.object(scheduler, "api")
    def test_next_vote_artifact_rejects_oversized_archive_metadata(self, api):
        response = unittest.mock.MagicMock()
        response.json.return_value = {
            "artifacts": [{
                "id": 1,
                "name": "next-vote",
                "expired": False,
                "created_at": "2033-05-18T03:33:20Z",
                "size_in_bytes": 1024 * 1024 + 1,
            }]
        }
        api.return_value = response
        with self.assertRaisesRegex(RuntimeError, "invalid size"):
            scheduler.next_vote_at_from_run(123)
        self.assertEqual(api.call_count, 1)


class WorkflowConfigurationTests(unittest.TestCase):
    def test_all_github_jobs_pin_ubuntu_24_04(self):
        root = pathlib.Path(__file__).parent
        for relative in (
            ".github/workflows/vote.yml",
            ".github/workflows/security.yml",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("ubuntu-latest", text)
            self.assertNotIn("ubuntu-22.04", text)
            self.assertIn("runs-on: ubuntu-24.04", text)

    def test_long_running_jobs_have_explicit_timeouts(self):
        root = pathlib.Path(__file__).parent
        vote_workflow = (root / ".github/workflows/vote.yml").read_text(encoding="utf-8")
        security_workflow = (root / ".github/workflows/security.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(vote_workflow.count("timeout-minutes:"), 5)
        self.assertEqual(security_workflow.count("timeout-minutes: 10"), 3)


    def test_select_new_dispatched_run_filters_expected_source(self):
        runs = [
            {
                "id": 13,
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:31Z",
                "display_title": "Top.gg Auto Vote · manual",
            },
            {
                "id": 12,
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:30Z",
                "display_title": "Top.gg Auto Vote · northflank",
            },
        ]
        self.assertEqual(
            scheduler.select_new_dispatched_run(
                runs,
                set(),
                2_000_000_000,
                expected_source="northflank",
            ),
            12,
        )

    @patch.object(scheduler, "list_vote_runs")
    @patch.object(scheduler, "api")
    def test_dispatch_reuses_any_active_run_not_only_first(self, api, list_runs):
        list_runs.return_value = [
            {
                "id": 100,
                "status": "completed",
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:34:00Z",
            },
            {
                "id": 99,
                "status": "in_progress",
                "event": "workflow_dispatch",
                "created_at": "2033-05-18T03:33:20Z",
            },
        ]
        self.assertEqual(scheduler.dispatch_vote(), 99)
        api.assert_not_called()


    def test_vote_workflow_bounds_cross_category_recovery(self):
        root = pathlib.Path(__file__).parent
        workflow = (root / ".github/workflows/vote.yml").read_text(encoding="utf-8")
        self.assertIn("recovery_depth:", workflow)
        self.assertIn('0) NEXT_DEPTH=1 ;;', workflow)
        self.assertIn('1) NEXT_DEPTH=2 ;;', workflow)
        self.assertIn('*) echo "Fresh-run recovery depth is invalid or exhausted."; exit 0 ;;', workflow)
        self.assertIn('inputs[recovery_depth]=$NEXT_DEPTH', workflow)
        self.assertIn('if [ "$SOURCE" = "browser-startup-retry" ]', workflow)
        self.assertIn('if [ "$SOURCE" = "protection-retry" ]', workflow)

    def test_scheduler_image_pins_runtime_dependencies_and_non_root_user(self):
        root = pathlib.Path(__file__).parent
        dockerfile = (root / "scheduler" / "Dockerfile").read_text(encoding="utf-8")
        for requirement in (
            "requests==2.34.2",
            "urllib3==2.7.0",
            "certifi==2026.7.22",
            "charset-normalizer==3.4.9",
            "idna==3.18",
        ):
            self.assertIn(requirement, dockerfile)
        self.assertIn("--no-deps", dockerfile)
        self.assertIn("python -m pip check", dockerfile)
        self.assertIn("USER app", dockerfile)


if __name__ == "__main__":
    unittest.main()
