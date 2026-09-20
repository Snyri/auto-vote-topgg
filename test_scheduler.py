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


if __name__ == "__main__":
    unittest.main()
