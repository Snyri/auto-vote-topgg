import importlib.util
import os
import pathlib
import unittest
from unittest.mock import patch

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

        self.assertEqual(result, new_target)

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


class ArtifactValidationTests(unittest.TestCase):
    @patch.object(scheduler, "api")
    def test_next_vote_artifact_rejects_unexpected_json_fields(self, api):
        import io
        import json
        import zipfile

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
        self.assertIn('if [ "$RECOVERY_DEPTH" -ge 2 ]', workflow)
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
