"""A page acknowledgement must survive a would-be blocked verification reload."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import vote


BEFORE = {"observed": True, "confirmed": False, "evidence": None}
ACK = {"observed": True, "confirmed": True, "evidence": "bounded cooldown"}


class VoteConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, snapshots, *, challenged=False, reload_error=None, click=True):
        tab = AsyncMock()
        tab.reload.side_effect = reload_error
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.current_url", new=AsyncMock(return_value="https://top.gg/bot/111/vote")),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch("vote.body_text", new=AsyncMock(return_value="Ready to vote")),
            patch("vote.evaluate", new=AsyncMock(return_value="Vote for a bot")),
            patch("vote.is_turnstile_present", new=AsyncMock(side_effect=[False, False, challenged, False])),
            patch("vote.solve_turnstile", new=AsyncMock(return_value=True)) as solver,
            patch("vote.wait_for_ad", new=AsyncMock(return_value=None)),
            patch("vote.mark_vote_button", new=AsyncMock(return_value={"found": True, "disabled": False})),
            patch("vote._click_marked", new=AsyncMock(
                side_effect=click if isinstance(click, Exception) else None,
                return_value=click,
            )) as clicked,
            patch("vote.vote_page_confirmation", new=AsyncMock(side_effect=snapshots)),
            patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value={
                "confirmed": True, "evidence": "bounded cooldown",
            })) as persisted,
        ):
            result = await vote.vote_for_bot(tab, "111", "account")
        return result, tab, clicked, solver, persisted

    async def test_acknowledgement_avoids_reload_that_would_be_blocked(self):
        result, tab, clicked, solver, persisted = await self.exercise(
            [BEFORE, ACK, ACK], reload_error=RuntimeError("Cloudflare on reload"),
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("on page", result["detail"])
        self.assertGreater(result["retry_at"], int(datetime.now(timezone.utc).timestamp()) + 12 * 3600)
        self.assertFalse(vote.has_business_failure([[result]]))
        self.assertFalse(vote.should_request_protection_retry([[result]]))
        tab.reload.assert_not_awaited()
        clicked.assert_awaited_once()
        solver.assert_not_awaited()
        persisted.assert_not_awaited()

    async def test_acknowledgement_after_post_click_challenge_avoids_reload(self):
        result, tab, clicked, solver, persisted = await self.exercise(
            [BEFORE, BEFORE, BEFORE, BEFORE, BEFORE, ACK, ACK], challenged=True,
        )
        self.assertEqual(result["status"], "success")
        self.assertIn("after verification", result["detail"])
        tab.reload.assert_not_awaited()
        clicked.assert_awaited_once()
        solver.assert_awaited_once()
        persisted.assert_not_awaited()

    async def test_missing_acknowledgement_retains_independent_confirmation_fallback(self):
        result, tab, _, _, persisted = await self.exercise([BEFORE] * 5)
        self.assertEqual(result["status"], "success")
        tab.reload.assert_awaited_once()
        persisted.assert_awaited_once()

    async def test_post_click_browser_error_preserves_submission_and_prevents_retry(self):
        result, tab, clicked, _, _ = await self.exercise(
            [BEFORE] * 5, reload_error=RuntimeError("browser disconnected"),
        )
        self.assertEqual(result["status"], "uncertain")
        self.assertIs(result["vote_submitted"], True)
        self.assertFalse(vote.is_retryable_result(result))
        self.assertTrue(vote.has_business_failure([[result]]))
        clicked.assert_awaited_once()
        tab.reload.assert_awaited_once()

    async def test_failed_click_command_does_not_blindly_repeat_a_possible_submission(self):
        result, tab, clicked, _, persisted = await self.exercise([BEFORE], click=False)
        self.assertEqual(result["status"], "uncertain")
        self.assertIs(result["vote_submitted"], True)
        self.assertFalse(vote.is_retryable_result(result))
        clicked.assert_awaited_once()
        tab.reload.assert_not_awaited()
        persisted.assert_not_awaited()

    async def test_unactionable_control_is_not_misreported_as_a_submission(self):
        result, tab, clicked, _, persisted = await self.exercise(
            [BEFORE], click=vote.VoteClickNotReady("covered"),
        )
        self.assertEqual(result["status"], "error")
        self.assertIs(result["vote_submitted"], False)
        self.assertTrue(vote.is_retryable_result(result))
        clicked.assert_awaited_once()
        tab.reload.assert_not_awaited()
        persisted.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
