"""Regression coverage for the active AniGame browser and recovery runtime."""


import json
import tempfile
import unittest
import urllib.error
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
import action_recovery


class RetryDispatchAuditRegressions(unittest.TestCase):

    def test_newer_handoff_supersedes_older_explicit_retry_origin(self):
        client = MagicMock()
        client.runs.return_value = [{'id': 44, 'status': 'completed'}]
        client.state.return_value = {'version': 1, 'scope': '0' * 64, 'records': {}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'vote-recovery.json'
            action_recovery.restore(client, 45, '42', path)
            self.assertEqual(json.loads(path.read_text()), client.state.return_value)
        client.state.assert_called_once_with(44)

    def test_signed_artifact_download_receives_no_github_credential(self):
        data = {'version': 1, 'scope': '0' * 64, 'records': {}}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('vote-recovery.json', json.dumps(data))
        client = action_recovery.GitHub('owner/repo', 'private-github-token')
        client.request = MagicMock(side_effect=[{'head_branch': 'master', 'event': 'workflow_dispatch', 'path': '.github/workflows/vote.yml'}, {'artifacts': [{'id': 1, 'name': 'vote-recovery', 'size_in_bytes': len(archive.getvalue()), 'expired': False}]}, urllib.error.HTTPError('https://api.github.com', 302, 'Found', {'Location': 'https://storage.example/signed'}, None)])
        response = MagicMock()
        response.__enter__.return_value.read.return_value = archive.getvalue()
        client.opener = MagicMock()
        client.opener.open.return_value = response
        self.assertEqual(client.state(42), data)
        request = client.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))

    def test_recovery_rejects_a_run_from_an_untrusted_branch(self):
        client = action_recovery.GitHub('owner/repo', 'token')
        client.request = MagicMock(return_value={'head_branch': 'untrusted', 'event': 'workflow_dispatch', 'path': '.github/workflows/vote.yml'})
        with self.assertRaises(RuntimeError):
            client.state(42)
        self.assertEqual(client.request.call_count, 1)

    def exercise(self, *, accepted_on_error=False, existing=False, prefix='Top.gg Auto Vote'):
        now = [0]
        client = MagicMock()
        title = f'{prefix} · failure-retry · 42'
        run = {'id': 43, 'display_title': title}
        client.runs.side_effect = lambda: [run] if existing or (accepted_on_error and now[0] >= 2) else []
        client.request.side_effect = TimeoutError('private response') if accepted_on_error else None
        action_recovery.dispatch_retry(client, 42, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), budget=8)
        return client

    def test_accepted_post_with_lost_response_does_not_dispatch_twice(self):
        client = self.exercise(accepted_on_error=True)
        self.assertEqual(client.request.call_count, 1)

    def test_existing_correlated_run_is_reused(self):
        for prefix in ('Top.gg Auto Vote', 'AniGame Auto Vote'):
            with self.subTest(prefix=prefix):
                self.exercise(existing=True, prefix=prefix).request.assert_not_called()

    def test_definitive_failure_retries_and_remains_bounded_per_dispatch_job(self):
        now = [0]
        client = MagicMock()
        client.runs.return_value = []
        client.request.side_effect = urllib.error.HTTPError('https://api.github.com', 503, 'Unavailable', {}, None)
        with self.assertRaises(RuntimeError):
            action_recovery.dispatch_retry(client, 42, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), budget=65)
        self.assertGreater(client.request.call_count, 1)
