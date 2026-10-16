"""Regression coverage for the active AniGame browser and recovery runtime."""


import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock
import action_recovery
STATE = {'version': 1, 'scope': '0' * 64, 'records': {}}


class RestoreReadRetryTests(unittest.TestCase):

    def exercise(self, *, runs=None, states=None, budget=120):
        now, sleeps = ([0], [])

        def sleep(seconds):
            now[0] += seconds
            sleeps.append(seconds)
        client = MagicMock()
        client.runs.side_effect = runs or [[{'id': 42, 'status': 'completed'}]]
        client.state.side_effect = states or [STATE]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'state.json'
            action_recovery.restore(client, 43, '', path, clock=lambda: now[0], sleep=sleep, budget=budget)
            self.assertEqual(json.loads(path.read_text()), STATE)
        return (client, sleeps)

    def test_transient_listing_failure_is_retried_locally(self):
        error = urllib.error.HTTPError('https://api.github.com', 503, 'Unavailable', {}, None)
        client, sleeps = self.exercise(runs=[error, [{'id': 42, 'status': 'completed'}]])
        self.assertEqual(client.runs.call_count, 2)
        self.assertEqual(sleeps, [1])

    def test_transient_artifact_download_failure_is_retried_without_rescanning(self):
        client, _ = self.exercise(states=[urllib.error.URLError('temporary connection failure'), STATE])
        self.assertEqual(client.state.call_count, 2)
        client.runs.assert_called_once()

    def test_rate_limit_retry_after_is_respected(self):
        error = urllib.error.HTTPError('https://api.github.com', 429, 'Limited', {'Retry-After': '4'}, None)
        _, sleeps = self.exercise(states=[error, STATE])
        self.assertEqual(sleeps, [4])

    def test_permanent_errors_and_invalid_state_cannot_replace_saved_handoff(self):
        for error in (urllib.error.HTTPError('https://api.github.com', 401, 'Denied', {}, None), urllib.error.HTTPError('https://api.github.com', 403, 'Denied', {}, None), RuntimeError('Untrusted source'), ValueError('Invalid artifact'), {'version': 1}):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'state.json'
                path.write_text('existing handoff')
                client = MagicMock()
                client.runs.return_value = [{'id': 42, 'status': 'completed'}]
                client.state.side_effect = [error]
                with self.assertRaises((urllib.error.HTTPError, RuntimeError, ValueError)):
                    action_recovery.restore(client, 43, '', path, sleep=lambda _: self.fail('Permanent errors must not retry'))
                self.assertEqual(path.read_text(), 'existing handoff')
                client.state.assert_called_once()

    def test_unavailable_reads_are_bounded_and_do_not_start_fresh(self):
        for budget, expected in ((120, 5), (3, 2)):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as folder:
                now = [0]
                client = MagicMock()
                client.runs.side_effect = TimeoutError('Read unavailable')
                path = Path(folder) / 'state.json'
                with self.assertRaises(TimeoutError):
                    action_recovery.restore(client, 43, '', path, budget=budget, clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                self.assertEqual(client.runs.call_count, expected)
                self.assertFalse(path.exists())
