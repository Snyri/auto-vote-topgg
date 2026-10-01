"""Regression cases reproduced in the repository audit, using production paths."""

import asyncio
import json
import tempfile
import unittest
import urllib.error
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import action_recovery
import graphql_vote
import request_diagnostics
import vote
from recovery_state import SubmissionJournal
from test_vote_page_receipt import raw_page
from vote_network import VoteNetworkState

PAGE = "https://top.gg/bot/111/vote"
JSON = {"cloudflare_challenge": False, "content_kind": "json"}
CF = {"cloudflare_challenge": True, "content_kind": "html"}


def request(state, *, query=None, method="POST", started=101, request_id="vote"):
    return state.on_request(NS(request_id=request_id, wall_time=started, document_url=PAGE, redirect_response=None,
        request=NS(url="https://top.gg/api/graphql" if query else "https://top.gg/api/bots/111/vote" + ("/status" if method == "GET" else ""),
                   method=method, post_data=json.dumps({"query": query}) if query else None)))


def armed_state():
    state = VoteNetworkState()
    state.select_bot("111")
    state.begin_input()
    state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100})
    return state


class NetworkAuditRegressions(unittest.TestCase):
    def test_old_duplicate_denial_cannot_reactivate_a_cleared_gate(self):
        state = armed_state()
        old = request(state, method="GET")
        state.response(old, 403, CF)
        state.finish(old)
        new = request(state, method="GET", started=102, request_id="new")
        state.response(new, 200, JSON)
        state.finish(new)
        self.assertTrue(state.protection_pending())  # Body is still uninspected.
        state.response_body(new, "usable")
        self.assertFalse(state.protection_pending())
        state.response(old, 403, CF)
        self.assertFalse(state.protection_pending())
        state.response(new, 403, CF)  # Contradiction of the actual recovery.
        self.assertTrue(state.protection_pending())

    def test_unknown_entity_mutation_application_error_is_effective(self):
        state = armed_state()
        info = request(state, query='mutation { submitEntityBallot(entityId:"999") { ok } }')
        state.response(info, 200, JSON)
        state.finish(info)
        state.response_body(info, graphql_vote.inspect_response(
            '{"errors":[{"extensions":{"code":"CAPTCHA_REQUIRED"}}]}', info["response_key"], "vote_submission"))
        self.assertEqual(state.submission_outcome(), "captcha_required")
        self.assertFalse(state.confirmation_covered())
        self.assertFalse(state.definitely_rejected())

    def test_unknown_root_with_explicit_vote_state_selection_is_recognized(self):
        state = armed_state()
        info = request(state, query='query { lookupVoting(botId:"111") { canVote } }')
        self.assertEqual(info["operation"], "vote_state")
        state.response(info, 403, CF)
        self.assertTrue(state.protection_pending())

    def test_uuid_entity_scope_is_supported_without_retaining_raw_id(self):
        value = "01234567-89ab-cdef-0123-456789abcdef"
        info = graphql_vote.inspect_request(json.dumps({"query": 'mutation { voteEntity(entityId:"' + value + '") { ok } }'}), "111")
        self.assertEqual(info["graphql_target"], "page_entity")
        self.assertNotIn(value, repr(info))

    def test_transport_failure_does_not_look_like_a_pending_healthy_response(self):
        state = armed_state()
        info = request(state)
        state.response(info, 200, JSON)
        state.finish(info, failed=True)
        self.assertEqual(state.submission_outcome(), "unavailable")
        self.assertFalse(state.confirmation_covered())


class ConfirmationAuditRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_pointer_deadline_preserves_the_observed_blocking_reason(self):
        tab = AsyncMock()
        with patch("vote.TIMEOUT_VOTE_SEC", 0.01), patch("vote.dismiss_privacy_overlay", new=AsyncMock()), \
                patch("vote.mark_vote_button", new=AsyncMock()), \
                patch("vote._vote_pointer_target", new=AsyncMock(return_value={"ready":False,"reason":"disabled"})), \
                patch("vote.evaluate", new=AsyncMock(return_value={})), \
                patch("vote.RECOVERY_JOURNAL", None):
            with self.assertRaisesRegex(vote.VoteClickNotReady, "disabled"):
                await vote._click_vote_control(tab)
        tab.send.assert_not_awaited()

    async def verify(self, state, *, late_error=False, persisted=False):
        tab = AsyncMock()
        async def observe(*_):
            if late_error:
                state.response_body(next(iter(state.candidates.values())), "error")
            return True
        with (
            patch("builtins.print"), patch("vote.asyncio.sleep", new=AsyncMock()),
            patch("request_diagnostics.vote_state", return_value=state),
            patch("vote.confirm_vote_without_reload", new=AsyncMock(side_effect=observe)) as acknowledge,
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
            patch("vote.fresh_vote_document", new=AsyncMock(return_value=True)) as fresh,
            patch("vote.settle_privacy_overlay", new=AsyncMock()),
            patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value={"confirmed": persisted, "vote_enabled": not persisted})),
            patch("vote.browser_screenshot", new=AsyncMock(return_value=None)),
        ):
            result = await vote.verify_submitted_vote(tab, "111", "account", {"observed": True, "evidence": None})
        return result, acknowledge, fresh

    async def test_error_arriving_during_ui_acknowledgement_vetoes_success(self):
        state = armed_state()
        info = request(state)
        state.response(info, 200, JSON)
        state.finish(info)
        state.response_body(info, "usable")
        result, acknowledge, fresh = await self.verify(state, late_error=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(state.submission_outcome(), "error")
        acknowledge.assert_awaited_once()
        fresh.assert_awaited_once()

    async def test_missing_pending_and_unreadable_observers_need_independent_confirmation(self):
        for outcome in (None, "pending", "unavailable", "invalid"):
            with self.subTest(outcome=outcome):
                state = None
                if outcome is not None:
                    state = armed_state()
                    info = request(state)
                    state.response(info, 200, JSON)
                    if outcome != "pending":
                        state.finish(info)
                        state.response_body(info, outcome)
                result, acknowledge, fresh = await self.verify(state, persisted=True)
                self.assertEqual(result["status"], "success")
                acknowledge.assert_not_awaited()
                fresh.assert_awaited_once()

    async def test_persisted_confirmation_rejects_success_copy_with_errors(self):
        with patch("vote.evaluate", new=AsyncMock(return_value=raw_page(text="Thanks for voting! Something went wrong. Please try again.", error_present=True))):
            result = await vote.persisted_vote_confirmation(AsyncMock(), "111")
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["error_present"])

    async def test_new_document_is_required_after_reload(self):
        tab = AsyncMock()
        stamps = [{"epoch": 100, "url": PAGE, "ready": True}, {"epoch": 100, "url": PAGE, "ready": True},
                  {"epoch": 101, "url": PAGE, "ready": False}, {"epoch": 101, "url": PAGE, "ready": True}]
        with patch("vote.evaluate", new=AsyncMock(side_effect=stamps)), patch("vote.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(await vote.fresh_vote_document(tab, "111"))
        tab.reload.assert_awaited_once()

    async def test_hung_ad_read_is_cancelled_by_its_phase_deadline(self):
        cancelled = asyncio.Event()
        async def hung(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch("vote.TIMEOUT_VOTE_SEC", 0.01), patch("vote.body_text", new=hung), patch("vote.error_screenshot", new=AsyncMock(return_value=None)):
            result = await asyncio.wait_for(vote.wait_for_ad(AsyncMock(), "111"), 0.2)
        self.assertEqual(result["status"], "error")
        self.assertTrue(cancelled.is_set())

    async def test_hung_cdp_evaluation_is_cancelled(self):
        tab = AsyncMock()
        cancelled = asyncio.Event()
        async def hung(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        tab.send.side_effect = hung
        with patch("vote.BROWSER_COMMAND_TIMEOUT_SEC", 0.01):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(vote.evaluate(tab, "location.href"), 0.2)
        self.assertTrue(cancelled.is_set())

    async def test_rest_body_is_inspected_before_readiness_is_restored(self):
        tab = AsyncMock()
        tracker = request_diagnostics.RequestDiagnostics(tab)
        tracker.vote_network.select_bot("111")
        old = request(tracker.vote_network, method="GET")
        tracker.vote_network.response(old, 403, CF)
        tab.send.return_value = ('{"success":false}', False)
        await tracker.on_request(NS(request_id="read", wall_time=102, document_url=PAGE, redirect_response=None,
            request=NS(url="https://top.gg/api/bots/111/vote/status", method="GET", post_data=None)))
        await tracker.on_response(NS(request_id="read", response=NS(status=200, headers={"content-type":"application/json"})))
        with patch("builtins.print"):
            await tracker.on_finished(NS(request_id="read"))
        self.assertTrue(tracker.vote_network.protection_pending())
        self.assertEqual(tracker.completed["read"]["vote_network"]["response_outcome"], "error")


class HandoffAuditRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_production_pointer_persists_pending_state_before_native_press(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            journal = SubmissionJournal(path, ["token"], ["111"])
            journal.select("token", "111")
            tab = AsyncMock()
            pressed = []
            async def send(command):
                wire = next(command)
                if wire["params"]["type"] == "mousePressed":
                    pressed.append(json.loads(path.read_text())["records"][journal.active_key]["kind"])
            tab.send.side_effect = send
            with patch("vote.RECOVERY_JOURNAL", journal), patch("vote.asyncio.sleep", new=AsyncMock()), \
                    patch("vote.dismiss_privacy_overlay", new=AsyncMock()), patch("vote.mark_vote_button", new=AsyncMock()), \
                    patch("vote._vote_pointer_target", new=AsyncMock(return_value={"ready":True,"x":10,"y":10})), \
                    patch("vote.evaluate", new=AsyncMock(return_value={"pressed":True,"released":True,"clicked":True,"pressed_at":100})):
                self.assertTrue(await vote._click_vote_control(tab))
            self.assertEqual(pressed, ["pending"])

    async def test_completed_vote_is_skipped_on_fresh_action_before_browser_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            journal = SubmissionJournal(path, ["secret-token"], ["111"])
            journal.select("secret-token", "111")
            journal.before_press()
            journal.record_result(vote.successful_vote_result("111"))
            recovered = SubmissionJournal(path, ["secret-token"], ["111"])
            session = MagicMock()
            session.acquire = AsyncMock(side_effect=AssertionError("No browser needed for confirmed vote"))
            with patch("vote.RECOVERY_JOURNAL", recovered):
                results = await vote._process_account_attempts("secret-token", ["111"], 1, 1, session=session)
            self.assertEqual(results[0]["status"], "cooldown")
            session.acquire.assert_not_awaited()
            self.assertNotIn("secret-token", path.read_text())

    async def test_uncertain_handoff_is_verification_only_on_unresolved_page(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            journal = SubmissionJournal(path, ["secret-token"], ["111"])
            journal.select("secret-token", "111")
            journal.before_press()
            recovered = SubmissionJournal(path, ["secret-token"], ["111"])
            recovered.select("secret-token", "111")
            with patch("vote.RECOVERY_JOURNAL", recovered), patch("vote.fresh_vote_document", new=AsyncMock(return_value=True)), \
                    patch("vote.asyncio.sleep", new=AsyncMock()), patch("vote.settle_privacy_overlay", new=AsyncMock()), \
                    patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value={"observed": False})), \
                    patch("vote._click_marked", new=AsyncMock()) as click:
                result = await vote.recover_prior_submission(AsyncMock(), "111", "account")
            self.assertEqual(result["status"], "uncertain")
            click.assert_not_awaited()
            self.assertEqual(recovered.records[recovered.active_key]["kind"], "pending")

    def test_configuration_change_keeps_common_votes_without_blocking_new_bot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            journal = SubmissionJournal(path, ["token"], ["111"])
            journal.select("token", "111")
            journal.record_result(vote.successful_vote_result("111"))
            recovered = SubmissionJournal(path, ["token"], ["111", "222"])
            self.assertEqual(recovered.select("token", "111")["kind"], "complete")
            self.assertIsNone(recovered.select("token", "222"))


class RetryDispatchAuditRegressions(unittest.TestCase):
    def test_newer_handoff_supersedes_older_explicit_retry_origin(self):
        client = MagicMock()
        client.runs.return_value = [{"id": 44, "status":"completed"}]
        client.state.return_value = {"version":1,"scope":"0"*64,"records":{}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            action_recovery.restore(client, 45, "42", path)
            self.assertEqual(json.loads(path.read_text()), client.state.return_value)
        client.state.assert_called_once_with(44)

    def test_signed_artifact_download_receives_no_github_credential(self):
        data = {"version":1,"scope":"0"*64,"records":{}}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("vote-recovery.json", json.dumps(data))
        client = action_recovery.GitHub("owner/repo", "private-github-token")
        client.request = MagicMock(side_effect=[
            {"head_branch":"master","event":"workflow_dispatch","path":".github/workflows/vote.yml"},
            {"artifacts":[{"id":1,"name":"vote-recovery","size_in_bytes":len(archive.getvalue()),"expired":False}]},
            urllib.error.HTTPError("https://api.github.com",302,"Found",{"Location":"https://storage.example/signed"},None),
        ])
        response = MagicMock()
        response.__enter__.return_value.read.return_value = archive.getvalue()
        client.opener = MagicMock()
        client.opener.open.return_value = response
        self.assertEqual(client.state(42), data)
        request = client.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_recovery_rejects_a_run_from_an_untrusted_branch(self):
        client = action_recovery.GitHub("owner/repo", "token")
        client.request = MagicMock(return_value={"head_branch":"untrusted","event":"workflow_dispatch","path":".github/workflows/vote.yml"})
        with self.assertRaises(RuntimeError):
            client.state(42)
        self.assertEqual(client.request.call_count, 1)

    def exercise(self, *, accepted_on_error=False, existing=False):
        now = [0]
        client = MagicMock()
        title = "Top.gg Auto Vote · failure-retry · 42"
        run = {"id": 43, "display_title": title}
        client.runs.side_effect = lambda: [run] if existing or (accepted_on_error and now[0] >= 2) else []
        client.request.side_effect = TimeoutError("private response") if accepted_on_error else None
        action_recovery.dispatch_retry(client, 42, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), budget=8)
        return client

    def test_accepted_post_with_lost_response_does_not_dispatch_twice(self):
        client = self.exercise(accepted_on_error=True)
        self.assertEqual(client.request.call_count, 1)

    def test_existing_correlated_run_is_reused(self):
        self.exercise(existing=True).request.assert_not_called()

    def test_definitive_failure_retries_and_remains_bounded_per_dispatch_job(self):
        now = [0]
        client = MagicMock()
        client.runs.return_value = []
        client.request.side_effect = urllib.error.HTTPError("https://api.github.com", 503, "Unavailable", {}, None)
        with self.assertRaises(RuntimeError):
            action_recovery.dispatch_retry(client, 42, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), budget=65)
        self.assertGreater(client.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
