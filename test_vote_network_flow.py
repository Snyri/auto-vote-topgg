"""Response-driven Vote recovery preserves uncertain and completed submissions."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import request_diagnostics
import vote


PAGE = "https://top.gg/bot/111/vote"
ENDPOINT = "https://top.gg/api/bots/111/vote"
BEFORE = {"observed": True, "confirmed": False, "evidence": None}
ACK = {"observed": True, "confirmed": True, "evidence": "thanks for voting"}
CHALLENGE = {"cf-mitigated": "challenge", "content-type": "text/html"}
JSON = {"content-type": "application/json"}


class VoteNetworkFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tab = AsyncMock()
        self.tracker = request_diagnostics.RequestDiagnostics(self.tab)
        self.tab._topgg_diagnostics = self.tracker
        self.tracker.vote_network.select_bot("111")

    async def response(self, status=403, headers=CHALLENGE, url=ENDPOINT, method="POST", request_id="1", started=101):
        await self.tracker.on_request(NS(request_id=request_id, wall_time=started, document_url=PAGE,
            redirect_response=None, request=NS(url=url, method=method, post_data=None)))
        await self.tracker.on_response(NS(request_id=request_id, response=NS(status=status, headers=headers)))
        await self.tracker.on_finished(NS(request_id=request_id))

    async def exercise(self, statuses=(403,), *, url=ENDPOINT, acknowledge=False, preflight=False, clear=False, cooldown=False):
        async def click(*_):
            request_diagnostics.begin_vote_input(self.tab)
            for index, status in enumerate(statuses):
                await self.response(status, CHALLENGE if status == 403 else JSON,
                                    url=url, request_id="submit-" + str(index))
            request_diagnostics.record_vote_receipt(self.tab, {
                "pressed": True, "released": True, "clicked": True, "pressed_at": 100.0,
            })
            return True

        async def sleep(_):
            if clear and self.tracker.vote_network.protection_pending():
                await self.response(200, JSON, ENDPOINT + "/status", "GET", "ready", started=102)

        if preflight:
            with patch("builtins.print"):
                await self.response(url=ENDPOINT + "/status", method="GET")
        snapshots = [BEFORE, ACK, ACK] if acknowledge else [BEFORE] * 5
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new=AsyncMock(side_effect=sleep)),
            patch("vote.current_url", new=AsyncMock(return_value=PAGE)),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch("vote.body_text", new=AsyncMock(side_effect=(
                ["Ready to vote", "You can vote again in 11 hours"] if cooldown else None),
                return_value="Ready to vote")),
            patch("vote.evaluate", new=AsyncMock(return_value="Vote for a bot")),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
            patch("vote.wait_for_ad", new=AsyncMock(return_value=None)),
            patch("vote.mark_vote_button", new=AsyncMock(return_value={"found": True, "disabled": False})),
            patch("vote._click_marked", new=AsyncMock(side_effect=click)) as clicked,
            patch("vote.vote_page_confirmation", new=AsyncMock(side_effect=snapshots)),
            patch("vote.persisted_vote_confirmation", new=AsyncMock(return_value={"confirmed": False, "vote_enabled": True})) as persisted,
            patch("vote.browser_screenshot", new=AsyncMock(return_value=None)),
        ):
            result = await vote.vote_for_bot(self.tab, "111", "account")
        return result, clicked, persisted

    async def test_exact_cloudflare_denial_skips_reloads_and_permits_fresh_browser(self):
        result, clicked, persisted = await self.exercise()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["submission_rejected"])
        self.assertFalse(result["vote_submitted"])
        clicked.assert_awaited_once()
        self.tab.reload.assert_not_awaited()
        persisted.assert_not_awaited()
        self.assertTrue(vote.should_request_protection_retry([[result]]))

    async def test_other_api_denial_and_mixed_vote_responses_remain_uncertain(self):
        for statuses, url in (((403,), "https://top.gg/api/analytics"), ((403, 200), ENDPOINT)):
            with self.subTest(statuses=statuses, url=url):
                await self.asyncSetUp()
                result, clicked, _ = await self.exercise(statuses, url=url)
                self.assertEqual(result["status"], "uncertain")
                self.assertTrue(result["vote_submitted"])
                self.assertNotIn("submission_rejected", result)
                clicked.assert_awaited_once()
                self.assertFalse(vote.is_retryable_result(result))

    async def test_healthy_response_still_requires_page_acknowledgement(self):
        result, _, _ = await self.exercise((200,), acknowledge=True)
        self.assertEqual(result["status"], "success")
        self.tab.reload.assert_not_awaited()
        await self.asyncSetUp()
        result, _, _ = await self.exercise((200,))
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["vote_submitted"])

    async def test_recent_essential_denial_blocks_mouse_input(self):
        result, clicked, _ = await self.exercise(preflight=True)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["vote_submitted"])
        clicked.assert_not_awaited()
        self.tab.reload.assert_not_awaited()

    async def test_fresh_vote_state_allows_input_after_preflight(self):
        result, clicked, _ = await self.exercise((200,), acknowledge=True, preflight=True, clear=True)
        self.assertEqual(result["status"], "success")
        clicked.assert_awaited_once()

    async def test_cooldown_appearing_during_preflight_is_not_clicked(self):
        result, clicked, _ = await self.exercise(preflight=True, clear=True, cooldown=True)
        self.assertEqual(result["status"], "cooldown")
        clicked.assert_not_awaited()

    async def test_completed_and_uncertain_bots_are_excluded_from_rejection_retry(self):
        rejected = {"bot_id": "111", "status": "blocked", "detail": "Vote API challenged",
                    "vote_submitted": False, "submission_rejected": True}
        uncertain = {"bot_id": "222", "status": "uncertain", "detail": "Unknown", "vote_submitted": True}
        completed = {"bot_id": "333", "status": "success", "detail": "Confirmed"}
        recovered = {"bot_id": "111", "status": "success", "detail": "Confirmed"}
        with (patch("builtins.print"), patch("vote.asyncio.sleep", new_callable=AsyncMock),
              patch("vote._run_account", new=AsyncMock(side_effect=[[rejected, uncertain, completed], [recovered]])) as run):
            result = await vote.process_account("token", ["111", "222", "333"], 1, 1)
        self.assertEqual(result, [recovered, uncertain, completed])
        self.assertEqual([call.args[1] for call in run.await_args_list], [["111", "222", "333"], ["111"]])


class VoteNetworkCDPTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_headers_and_cors_failure_preserve_exact_denial_evidence(self):
        tab = AsyncMock()
        tracker = request_diagnostics.RequestDiagnostics(tab)
        tracker.vote_network.select_bot("111")
        tracker.vote_network.begin_input()
        tracker.vote_network.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100})
        with patch("builtins.print") as log:
            await tracker.on_request(NS(request_id="vote", document_url=PAGE, wall_time=101,
                redirect_response=None, request=NS(url=ENDPOINT + "?token=SECRET", method="POST")))
            await tracker.on_failed(NS(request_id="vote", error_text="PRIVATE"))
            self.assertFalse(tracker.vote_network.definitely_rejected())
            await tracker.on_extra(NS(request_id="vote", status_code=403, headers=CHALLENGE))
        self.assertTrue(tracker.vote_network.definitely_rejected())
        self.assertNotIn("SECRET", str(log.call_args_list))
        self.assertNotIn("PRIVATE", str(log.call_args_list))
        self.assertIn('"vote_operation": "vote_submission"', str(log.call_args_list))


if __name__ == "__main__":
    unittest.main()
