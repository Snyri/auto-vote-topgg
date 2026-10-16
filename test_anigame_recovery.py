"""Votes confirmed on one service must not suppress another service's vote."""

import tempfile
import unittest
from pathlib import Path

from recovery_state import SubmissionJournal, configuration_scope


class AniGameRecoveryTests(unittest.TestCase):
    def test_legacy_complete_and_pending_records_are_not_anigame_votes(self):
        for kind in ("complete", "pending"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "vote-recovery.json"
                legacy = SubmissionJournal(path, ["account-token"], ["571027211407196161"], clock=lambda: 1000)
                legacy.select("account-token", "571027211407196161")
                legacy.before_press()
                if kind == "complete":
                    legacy.record_result({"status": "success", "retry_at": 12000})
                migrated = SubmissionJournal(path, ["account-token"], ["571027211407196161"],
                    clock=lambda: 1000, namespace="https://www.anigame.gg/vote:v1")
                self.assertIsNone(migrated.select("account-token", "571027211407196161"))
                self.assertEqual(migrated.records, {})

    def test_anigame_confirmation_retains_its_original_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vote-recovery.json"
            first = SubmissionJournal(path, ["account-token"], ["571027211407196161"],
                clock=lambda: 1000, namespace="https://www.anigame.gg/vote:v1")
            first.select("account-token", "571027211407196161")
            first.before_press()
            first.record_result({"status": "success", "retry_at": 12000})
            retry = SubmissionJournal(path, ["account-token"], ["571027211407196161"],
                clock=lambda: 2000, namespace="https://www.anigame.gg/vote:v1")
            self.assertEqual(retry.select("account-token", "571027211407196161"),
                {"kind": "complete", "until": 12000})
            self.assertNotIn("account-token", path.read_text())

    def test_provider_scope_is_distinct_and_legacy_scope_stays_compatible(self):
        old = configuration_scope(["account-token"], ["571027211407196161"])
        self.assertEqual(old, configuration_scope(["account-token"], ["571027211407196161"], ""))
        self.assertNotEqual(old, configuration_scope(["account-token"], ["571027211407196161"],
            "https://www.anigame.gg/vote:v1"))


if __name__ == "__main__":
    unittest.main()
