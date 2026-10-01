"""Exercise prerequisite transitions that otherwise repeat across fresh Actions."""

import asyncio
import json
import tempfile
import time
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import action_recovery
import vote
from recovery_state import SubmissionJournal
from test_vote_page_receipt import raw_page


PAGE = "https://top.gg/bot/111/vote"
ELIGIBLE = {"observed": True, "confirmed": False, "evidence": None,
            "vote_enabled": True, "challenge": False, "login_required": False, "error_present": False}
STATE = {"version": 1, "scope": "0" * 64, "records": {}}


class PendingPrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    async def recover(self, *, ad=0, hydration=0, challenged=False, persisted=False, api_pending=0):
        elapsed, checks = [0], []
        solved = [not challenged]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vote-recovery.json"
            journal = SubmissionJournal(path, ["local-token"], ["111"])
            journal.select("local-token", "111")
            journal.before_press()
            # Read the same artifact a fresh Action would restore.
            journal = SubmissionJournal(path, ["local-token"], ["111"])
            session = NS(authenticated=True, acquire=AsyncMock(return_value=(None, AsyncMock())),
                         reusable=AsyncMock(return_value=True))

            async def sleep(seconds):
                elapsed[0] += seconds

            async def settle(_):
                elapsed[0] += 2.25

            async def body(_):
                if elapsed[0] < ad:
                    return "You will be able to vote after this ad"
                return "You can vote again in 1 hour" if persisted else "Ready to vote"

            async def snapshot(*_):
                checks.append(elapsed[0])
                if elapsed[0] < max(ad, hydration):
                    return {**ELIGIBLE, "observed": False, "vote_enabled": False}
                if persisted:
                    return {**ELIGIBLE, "confirmed": True, "vote_enabled": False, "evidence": "bounded cooldown"}
                return dict(ELIGIBLE)

            async def solve(_):
                solved[0] = True
                return True

            async def ordinary_vote(*_):
                self.assertGreaterEqual(elapsed[0], max(ad, hydration))
                self.assertTrue(solved[0])
                self.assertNotIn(journal.active_key, journal.records)
                journal.before_press()
                return vote.successful_vote_result("111")

            network = MagicMock()
            network.protection_pending.side_effect = [True] * api_pending + [False] * 20
            with (
                patch("builtins.print"), patch("vote.RECOVERY_JOURNAL", journal),
                patch("vote.topgg_auth_state", new=AsyncMock(return_value=vote.AUTHENTICATED)),
                patch("vote.fresh_vote_document", new=AsyncMock(return_value=True)) as fresh,
                patch("vote.asyncio.sleep", new=sleep), patch("vote.settle_privacy_overlay", new=settle),
                patch("vote.body_text", new=body), patch("vote.persisted_vote_confirmation", new=snapshot),
                patch("vote.is_turnstile_present", new=AsyncMock(side_effect=lambda _: not solved[0])),
                patch("vote.solve_turnstile", new=AsyncMock(side_effect=solve)) as solver,
                patch("request_diagnostics.vote_state", return_value=network),
                patch("vote.vote_for_bot", new=AsyncMock(side_effect=ordinary_vote)) as submit,
            ):
                result = (await vote._run_account("local-token", ["111"], "account", session=session))[0]
            fresh.assert_awaited_once()
            self.assertEqual(fresh.await_args.kwargs, {"navigate": True})
            self.assertEqual(journal.records[journal.active_key]["kind"], "complete")
            return result, submit, solver, checks

    async def test_restored_pending_state_waits_the_full_ad_without_restarting_it(self):
        result, submit, _, checks = await self.recover(ad=10)
        self.assertEqual(result["status"], "success")
        submit.assert_awaited_once()
        self.assertGreaterEqual(checks[0], 10)
        self.assertEqual(len(checks), 2)

    async def test_restored_pending_state_allows_hydration_before_deciding(self):
        result, submit, _, checks = await self.recover(hydration=10)
        self.assertEqual(result["status"], "success")
        self.assertLess(checks[0], 10)
        self.assertGreaterEqual(checks[-2], 10)
        submit.assert_awaited_once()

    async def test_restored_pending_state_recovers_a_challenge_then_waits_the_ad(self):
        result, submit, solver, _ = await self.recover(challenged=True, ad=10)
        self.assertEqual(result["status"], "success")
        solver.assert_awaited_once()
        submit.assert_awaited_once()

    async def test_eligibility_must_stay_clear_after_a_transient_api_denial(self):
        result, submit, _, checks = await self.recover(api_pending=2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(checks), 4)
        submit.assert_awaited_once()

    async def test_already_persisted_vote_preserves_remaining_cooldown_without_input(self):
        result, submit, _, _ = await self.recover(persisted=True)
        self.assertEqual(result["status"], "cooldown")
        self.assertLess(result["retry_at"], time.time() + 4000)
        submit.assert_not_awaited()

    async def test_unknown_and_failed_challenge_keep_pending_state_and_never_submit(self):
        for challenge in (False, True):
            with self.subTest(challenge=challenge), tempfile.TemporaryDirectory() as folder:
                journal = SubmissionJournal(Path(folder) / "state.json", ["token"], ["111"])
                journal.select("token", "111")
                journal.before_press()
                with (
                    patch("builtins.print"), patch("vote.RECOVERY_JOURNAL", journal),
                    patch("vote.RECOVERY_OBSERVE_TIMEOUT_SEC", 0.01),
                    patch("vote.fresh_vote_document", new=AsyncMock(return_value=True)),
                    patch("vote.asyncio.sleep", new=AsyncMock()), patch("vote.settle_privacy_overlay", new=AsyncMock()),
                    patch("vote.wait_for_ad", new=AsyncMock(return_value=None)),
                    patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value={"observed": False})),
                    patch("vote.is_turnstile_present", new=AsyncMock(return_value=challenge)),
                    patch("vote.solve_turnstile", new=AsyncMock(return_value=False)),
                    patch("vote.vote_for_bot", new=AsyncMock()) as submit,
                ):
                    result = await vote.recover_prior_submission(AsyncMock(), "111", "account")
                self.assertEqual(result["status"], "uncertain")
                self.assertTrue(result["vote_submitted"])
                self.assertEqual(journal.records[journal.active_key]["kind"], "pending")
                submit.assert_not_awaited()

    async def test_observation_timeout_does_not_cancel_an_eligible_submission(self):
        original_sleep = asyncio.sleep
        async def submit(*_):
            await original_sleep(0.04)
            return vote.successful_vote_result("111")
        with (
            patch("builtins.print"), patch("vote.RECOVERY_JOURNAL", None),
            patch("vote.RECOVERY_OBSERVE_TIMEOUT_SEC", 0.02),
            patch("vote.fresh_vote_document", new=AsyncMock(return_value=True)),
            patch("vote.asyncio.sleep", new=AsyncMock()), patch("vote.settle_privacy_overlay", new=AsyncMock()),
            patch("vote.wait_for_ad", new=AsyncMock(return_value=None)),
            patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value=ELIGIBLE)),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
            patch("request_diagnostics.vote_state", return_value=None),
            patch("vote.vote_for_bot", new=submit),
        ):
            result = await vote.recover_prior_submission(AsyncMock(), "111", "account")
        self.assertEqual(result["status"], "success")


class CooldownPrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_copy_cannot_override_conflicting_page_state(self):
        conflicts = (
            {"vote_enabled": True}, {"error_present": True}, {"challenge": True},
            {"login_required": True}, {"exact_vote_page": False}, {"ready": False},
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict), patch("vote.evaluate", new=AsyncMock(return_value=raw_page(
                    text="You can vote again in 11 hours. Something on the page.", **conflict))):
                result = await vote.confirmed_cooldown(AsyncMock(), "111", "You can vote again in 11 hours")
                self.assertIsNone(result)

    async def test_unstable_cooldown_never_completes_the_handoff(self):
        with (
            patch("vote.asyncio.sleep", new=AsyncMock()),
            patch("vote.evaluate", new=AsyncMock(side_effect=[raw_page(text="You can vote again in 1 hour"),
                                                            raw_page(text="Ready to vote", vote_enabled=True)])),
        ):
            self.assertIsNone(await vote.confirmed_cooldown(AsyncMock(), "111", "You can vote again in 1 hour"))

    async def test_cooldown_copy_during_an_ad_does_not_confirm_a_disabled_vote(self):
        text = "You can vote again in 11 hours. You will be able to vote after this ad"
        with patch("vote.evaluate", new=AsyncMock(return_value=raw_page(text=text))):
            self.assertIsNone(await vote.confirmed_cooldown(AsyncMock(), "111", text))

    async def flow(self, texts, snapshots, button):
        with ExitStack() as stack:
            for name, value in {
                "current_url": PAGE, "settle_privacy_overlay": None, "evaluate": "Voting fixture",
                "is_turnstile_present": False, "wait_for_ad": None, "_wait_for_vote_api": False,
                "mark_vote_button": button,
                "verify_submitted_vote": vote.successful_vote_result("111"),
                "asyncio.sleep": None,
            }.items():
                stack.enter_context(patch("vote." + name, new=AsyncMock(return_value=value)))
            stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch("vote.body_text", new=AsyncMock(side_effect=texts)))
            stack.enter_context(patch("vote.vote_page_confirmation", new=AsyncMock(side_effect=snapshots)))
            clicked = stack.enter_context(patch("vote._click_marked", new=AsyncMock(return_value=True)))
            result = await vote.vote_for_bot(AsyncMock(), "111", "account")
        return result, clicked

    async def test_initial_stale_cooldown_does_not_suppress_the_available_vote(self):
        result, clicked = await self.flow(["You can vote again in 11 hours"], [ELIGIBLE, ELIGIBLE],
                                          {"found": True, "disabled": False})
        self.assertEqual(result["status"], "success")
        clicked.assert_awaited_once()

    async def test_cooldown_becoming_available_after_the_ad_finishes_without_input(self):
        ack = {**ELIGIBLE, "vote_enabled": False, "confirmed": True, "evidence": "bounded cooldown"}
        result, clicked = await self.flow(["Ready to vote", "You can vote again in 1 hour"], [ack, ack],
                                          {"found": False, "disabled": True})
        self.assertEqual(result["status"], "cooldown")
        clicked.assert_not_awaited()


class ProbeBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_response_after_generic_deadline_still_authenticates(self):
        tab = AsyncMock()
        async def send(_):
            await asyncio.sleep(0.08)
            return NS(value={"status": 200, "ok": True, "contentType": "application/json",
                             "jsonOk": True, "userPresent": True}), None
        tab.send.side_effect = send
        with patch("builtins.print"), patch("vote.BROWSER_COMMAND_TIMEOUT_SEC", 0.02), patch("vote.SESSION_PROBE_TIMEOUT_SEC", 0.2):
            self.assertTrue((await vote.topgg_session_probe(tab))["authenticated"])
            with self.assertRaises(TimeoutError):
                await vote.evaluate(tab, "location.href")


class RestoreReadRetryTests(unittest.TestCase):
    def exercise(self, *, runs=None, states=None, budget=120):
        now, sleeps = [0], []
        def sleep(seconds):
            now[0] += seconds
            sleeps.append(seconds)
        client = MagicMock()
        client.runs.side_effect = runs or [[{"id": 42, "status": "completed"}]]
        client.state.side_effect = states or [STATE]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "state.json"
            action_recovery.restore(client, 43, "", path, clock=lambda: now[0], sleep=sleep, budget=budget)
            self.assertEqual(json.loads(path.read_text()), STATE)
        return client, sleeps

    def test_transient_listing_failure_is_retried_locally(self):
        error = urllib.error.HTTPError("https://api.github.com", 503, "Unavailable", {}, None)
        client, sleeps = self.exercise(runs=[error, [{"id": 42, "status": "completed"}]])
        self.assertEqual(client.runs.call_count, 2)
        self.assertEqual(sleeps, [1])

    def test_transient_artifact_download_failure_is_retried_without_rescanning(self):
        client, _ = self.exercise(states=[urllib.error.URLError("temporary connection failure"), STATE])
        self.assertEqual(client.state.call_count, 2)
        client.runs.assert_called_once()

    def test_rate_limit_retry_after_is_respected(self):
        error = urllib.error.HTTPError("https://api.github.com", 429, "Limited", {"Retry-After": "4"}, None)
        _, sleeps = self.exercise(states=[error, STATE])
        self.assertEqual(sleeps, [4])

    def test_permanent_errors_and_invalid_state_cannot_replace_saved_handoff(self):
        for error in (urllib.error.HTTPError("https://api.github.com", 401, "Denied", {}, None),
                      urllib.error.HTTPError("https://api.github.com", 403, "Denied", {}, None),
                      RuntimeError("Untrusted source"), ValueError("Invalid artifact"), {"version": 1}):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "state.json"
                path.write_text("existing handoff")
                client = MagicMock()
                client.runs.return_value = [{"id": 42, "status": "completed"}]
                client.state.side_effect = [error]
                with self.assertRaises((urllib.error.HTTPError, RuntimeError, ValueError)):
                    action_recovery.restore(client, 43, "", path, sleep=lambda _: self.fail("Permanent errors must not retry"))
                self.assertEqual(path.read_text(), "existing handoff")
                client.state.assert_called_once()

    def test_unavailable_reads_are_bounded_and_do_not_start_fresh(self):
        for budget, expected in ((120, 5), (3, 2)):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as folder:
                now = [0]
                client = MagicMock()
                client.runs.side_effect = TimeoutError("Read unavailable")
                path = Path(folder) / "state.json"
                with self.assertRaises(TimeoutError):
                    action_recovery.restore(client, 43, "", path, budget=budget, clock=lambda: now[0],
                                            sleep=lambda delay: now.__setitem__(0, now[0] + delay))
                self.assertEqual(client.runs.call_count, expected)
                self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
