"""Persistent interstitials and standalone CAPTCHA keep distinct vote outcomes."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import vote


class VoteChallengeOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_phase(self, phase, auth_state):
        presence_by_phase = {
            "before_ad": [True],
            "after_ad": [False, True],
            "waiting_for_vote": [False, False, True],
            "after_click": [False, False, True],
            "verification": [False, False, False, True],
        }
        after_vote = phase in {"after_click", "verification"}
        tab = AsyncMock()
        screenshot_path = "screenshots/vote_account_111_captcha.png"
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.current_url", new=AsyncMock(return_value="https://top.gg/bot/111/vote")),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch("vote.body_text", new=AsyncMock(return_value="Ready to vote")),
            patch("vote.evaluate", new=AsyncMock(return_value="Vote for a bot")),
            patch("vote.is_turnstile_present", new=AsyncMock(side_effect=presence_by_phase[phase])) as present,
            patch("vote.solve_turnstile", new=AsyncMock(return_value=False)) as solver,
            patch("vote.unresolved_challenge_auth_state", new=AsyncMock(return_value=auth_state)) as classify,
            patch("vote.wait_for_ad", new=AsyncMock(return_value=None)),
            patch("vote.mark_vote_button", new=AsyncMock(return_value={
                "found": phase != "waiting_for_vote",
                "disabled": phase == "waiting_for_vote",
            })),
            patch("vote._click_marked", new=AsyncMock(return_value=True)) as click,
            patch("vote.browser_screenshot", new=AsyncMock(return_value=screenshot_path)) as screenshot,
            patch("vote.persisted_vote_confirmation", new_callable=AsyncMock) as confirmation,
        ):
            result = await vote.vote_for_bot(tab, "111", "account")

        self.assertEqual(result["bot_id"], "111")
        self.assertEqual(present.await_count, len(presence_by_phase[phase]))
        solver.assert_awaited_once_with(tab)
        classify.assert_awaited_once_with(tab)
        confirmation.assert_not_awaited()
        self.assertEqual(click.await_count, int(after_vote))
        self.assertEqual(tab.reload.await_count, int(phase == "verification"))
        self.assertTrue(vote.has_business_failure([[result]]))
        if auth_state == vote.AUTH_BLOCKED:
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("screenshot_path", result)
            screenshot.assert_not_awaited()
            if after_vote:
                self.assertIn("unverified", result["detail"])
                self.assertNotIn("before Vote", result["detail"])
            else:
                self.assertIn("access denied before Vote", result["detail"])
        else:
            self.assertEqual(result["status"], "captcha_required")
            self.assertEqual(result["screenshot_path"], screenshot_path)
            screenshot.assert_awaited_once_with(tab, screenshot_path, required=True)
            self.assertIn("CAPTCHA", result["detail"])

    async def test_persistent_interstitial_is_blocked_at_every_vote_phase(self):
        for phase in ("before_ad", "after_ad", "waiting_for_vote", "after_click", "verification"):
            with self.subTest(phase=phase):
                await self.exercise_phase(phase, vote.AUTH_BLOCKED)

    async def test_standalone_captcha_remains_terminal_at_every_vote_phase(self):
        for phase in ("before_ad", "after_ad", "waiting_for_vote", "after_click", "verification"):
            with self.subTest(phase=phase):
                await self.exercise_phase(phase, vote.AUTH_CAPTCHA_REQUIRED)

    async def test_result_helper_preserves_captcha_detail_and_screenshot(self):
        tab = AsyncMock()
        detail = "Interactive CAPTCHA still requires completion during verification"
        with (
            patch("builtins.print"),
            patch("vote.unresolved_challenge_auth_state", new=AsyncMock(return_value=vote.AUTH_CAPTCHA_REQUIRED)),
            patch("vote.browser_screenshot", new=AsyncMock(return_value="private-screenshot.png")) as screenshot,
        ):
            result = await vote.unresolved_challenge_result(
                tab, "222", detail, "second-account", after_vote=True,
            )

        self.assertEqual(result, {
            "bot_id": "222",
            "status": "captcha_required",
            "detail": detail,
            "screenshot_path": "private-screenshot.png",
        })
        screenshot.assert_awaited_once_with(
            tab, "screenshots/vote_second-account_222_captcha.png", required=True,
        )

    async def test_classification_uses_existing_retry_and_schedule_policies(self):
        now = datetime(2026, 9, 24, 20, 26, tzinfo=timezone.utc)
        for auth_state, expected_calls, expected_delay in (
            (vote.AUTH_BLOCKED, vote.MAX_BLOCKED_ATTEMPTS, vote.BLOCKED_RETRY_DELAY_SEC),
            (vote.AUTH_CAPTCHA_REQUIRED, 1, vote.CAPTCHA_RETRY_DELAY_SEC),
        ):
            with self.subTest(auth_state=auth_state):
                with (
                    patch("builtins.print"),
                    patch("vote.unresolved_challenge_auth_state", new=AsyncMock(return_value=auth_state)),
                    patch("vote.browser_screenshot", new=AsyncMock(return_value=None)),
                ):
                    result = await vote.unresolved_challenge_result(AsyncMock(), "111", "CAPTCHA required")
                with (
                    patch("builtins.print"),
                    patch("vote.asyncio.sleep", new_callable=AsyncMock),
                    patch("vote._run_account", new=AsyncMock(return_value=[result])) as run_account,
                ):
                    results = await vote.process_account("token", ["111"], 1, 1)

                self.assertEqual(run_account.await_count, expected_calls)
                self.assertEqual(results[0]["status"], auth_state)
                self.assertEqual(
                    vote.retry_at_for_result(results[0], now),
                    int(now.timestamp()) + expected_delay,
                )
                self.assertEqual(
                    vote.should_request_protection_retry([results]),
                    auth_state == vote.AUTH_BLOCKED,
                )


if __name__ == "__main__":
    unittest.main()
