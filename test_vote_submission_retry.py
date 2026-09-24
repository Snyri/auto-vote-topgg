"""Keep unconfirmed Vote submissions out of automatic recovery attempts."""

import unittest
from unittest.mock import AsyncMock, patch

import vote


def result(bot_id, status, *, submitted=False, detail=None):
    item = {
        "bot_id": bot_id,
        "status": status,
        "detail": detail or status,
        "account_id": "account",
    }
    if submitted:
        item["vote_submitted"] = True
    return item


class SubmissionRetryPolicyTests(unittest.TestCase):
    def test_unsubmitted_results_keep_existing_retry_policy(self):
        for status in (
            "error", "auth_failed", "uncertain", "blocked",
            "captcha_required", "success", "cooldown",
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    vote.is_retryable_result(result("111", status)),
                    status in {"error", "auth_failed", "uncertain"},
                )

    def test_no_result_with_a_recorded_submission_is_retryable(self):
        for status in (
            "error", "auth_failed", "uncertain", "blocked",
            "captcha_required", "success", "cooldown",
        ):
            with self.subTest(status=status):
                self.assertFalse(vote.is_retryable_result(
                    result("111", status, submitted=True),
                ))

    def test_bot_retry_selection_excludes_submitted_bots(self):
        self.assertEqual(vote.retryable_bot_ids([
            result("111", "uncertain", submitted=True),
            result("222", "error"),
            result("333", "error", submitted=True),
            result("444", "auth_failed"),
            result("all", "error"),
        ]), ["222", "444"])

    def test_unconfirmed_submission_suppresses_fresh_run_even_for_another_account(self):
        for status in ("uncertain", "error", "blocked", "captcha_required", "auth_failed"):
            with self.subTest(status=status):
                submitted = result("111", status, submitted=True)
                blocked = result("222", "blocked")
                self.assertFalse(vote.should_request_protection_retry([[submitted, blocked]]))
                self.assertFalse(vote.should_request_protection_retry([[submitted], [blocked]]))

    def test_confirmed_submission_does_not_suppress_other_bots_recovery(self):
        for status in ("success", "cooldown"):
            with self.subTest(status=status):
                self.assertTrue(vote.should_request_protection_retry([
                    [result("111", status, submitted=True)],
                    [result("222", "blocked")],
                ]))

    def test_fresh_run_policy_without_unconfirmed_submission_is_unchanged(self):
        self.assertFalse(vote.should_request_protection_retry([]))
        self.assertFalse(vote.should_request_protection_retry([[]]))
        self.assertFalse(vote.should_request_protection_retry([[result("111", "uncertain")]]))
        self.assertTrue(vote.should_request_protection_retry([[result("all", "blocked")]]))
        self.assertTrue(vote.should_request_protection_retry([[
            result("111", "uncertain"), result("222", "blocked"),
        ]]))

    def test_protection_marker_is_not_written_for_an_unconfirmed_submission(self):
        with patch("vote.Path") as path:
            written = vote.write_protection_retry_state([[
                result("111", "uncertain", submitted=True),
                result("222", "blocked"),
            ]], "protection-retry.json")
        self.assertFalse(written)
        path.assert_not_called()


class SubmissionRetryOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def process(self, attempts, bot_ids):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote._run_account", new=AsyncMock(side_effect=attempts)) as run_account,
        ):
            results = await vote.process_account("token", bot_ids, 1, 1)
        return results, [call.args[1] for call in run_account.await_args_list]

    async def test_single_submitted_failure_is_preserved_without_a_fresh_browser(self):
        for status in ("uncertain", "error", "blocked", "captcha_required", "auth_failed"):
            with self.subTest(status=status):
                submitted = result("111", status, submitted=True, detail="Vote clicked; outcome unknown")
                results, attempts = await self.process([[submitted]], ["111"])
                self.assertEqual(results, [submitted])
                self.assertEqual(attempts, [["111"]])
                self.assertTrue(vote.has_business_failure([results]))

    async def test_other_pending_bots_can_recover_without_repeating_the_submission(self):
        submitted = result("111", "uncertain", submitted=True, detail="Vote clicked; confirmation unavailable")
        completed = result("222", "success")
        cooldown = result("333", "cooldown")
        results, attempts = await self.process([
            [submitted, result("222", "error"), result("333", "auth_failed")],
            [completed, cooldown],
        ], ["111", "222", "333"])
        self.assertEqual(results, [submitted, completed, cooldown])
        self.assertEqual(attempts, [["111", "222", "333"], ["222", "333"]])

    async def test_later_account_auth_failures_cannot_erase_a_submission(self):
        for status in ("blocked", "auth_failed", "captcha_required"):
            with self.subTest(status=status):
                submitted = result("111", "uncertain", submitted=True, detail="Original post-click evidence")
                latest = result("all", status, detail="Later authentication failed")
                results, attempts = await self.process([
                    [submitted, result("222", "error", detail="Original pending error")],
                    [latest],
                    [latest],
                ], ["111", "222"])
                self.assertEqual(results[0], submitted)
                self.assertEqual(results[1], {**latest, "bot_id": "222"})
                self.assertEqual(attempts[0], ["111", "222"])
                self.assertTrue(all(ids == ["222"] for ids in attempts[1:]))
                self.assertFalse(vote.should_request_protection_retry([results]))

    async def test_later_startup_failure_updates_only_unsubmitted_pending_bots(self):
        submitted = result("111", "uncertain", submitted=True, detail="Original post-click evidence")
        results, attempts = await self.process([
            [submitted, result("222", "error")],
            vote.BrowserStartupError("fresh browser did not start"),
        ], ["111", "222"])
        self.assertEqual(results[0], submitted)
        self.assertEqual(results[1]["bot_id"], "222")
        self.assertEqual(results[1]["status"], "error")
        self.assertTrue(results[1]["detail"].startswith(vote.BROWSER_STARTUP_DETAIL_PREFIX))
        self.assertEqual(attempts, [["111", "222"], ["222"]])
        self.assertFalse(vote.should_request_browser_startup_retry([results]))

    async def test_mixed_protection_blocks_retry_only_the_bot_that_was_not_submitted(self):
        submitted = result("111", "blocked", submitted=True, detail="Protection after Vote")
        recovered = result("222", "success")
        results, attempts = await self.process([
            [submitted, result("222", "blocked", detail="Protection before Vote")],
            [recovered],
        ], ["111", "222"])
        self.assertEqual(results, [submitted, recovered])
        self.assertEqual(attempts, [["111", "222"], ["222"]])
        self.assertFalse(vote.should_request_protection_retry([results]))

    async def test_independent_persistent_block_keeps_its_existing_retry_bound(self):
        submitted = result("111", "uncertain", submitted=True, detail="Vote clicked; outcome unknown")
        blocked = result("222", "blocked")
        results, attempts = await self.process([
            [submitted, blocked],
            [blocked],
        ], ["111", "222"])
        self.assertEqual(results, [submitted, blocked])
        self.assertEqual(len(attempts), vote.MAX_BLOCKED_ATTEMPTS)
        self.assertEqual(attempts, [["111", "222"], ["222"]])
        self.assertFalse(vote.should_request_protection_retry([results]))


if __name__ == "__main__":
    unittest.main()
