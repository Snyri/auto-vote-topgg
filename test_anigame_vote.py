"""Migration regressions: eligibility, acknowledgement, retries and account isolation."""

import base64
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import anigame_page as page
import anigame_vote as client


class AniGamePageTests(unittest.TestCase):
    def test_only_actual_anigame_vote_and_confirmation_urls_match(self):
        self.assertEqual(page.page_kind("https://www.anigame.gg/vote"), "vote")
        self.assertEqual(page.page_kind("https://anigame.gg/vote/confirmation?id=private"), "confirmation")
        for url in ["https://top.gg/bot/571027211407196161/vote", "https://anigame.gg.evil/vote",
                    "https://evil/?next=https://anigame.gg/vote", "http://anigame.gg/vote",
                    "https://anigame.gg:444/vote", "https://x@anigame.gg/vote"]:
            self.assertEqual(page.page_kind(url), "external", url)

    def test_cooldowns_match_the_recording_and_do_not_match_other_rewards(self):
        for text, seconds in [("4h 21m", 15660), ("11h 59m", 43140), ("5s", 5), ("1h 2m 3s", 3723)]:
            self.assertEqual(page.cooldown_seconds(text), seconds)
        for text in ["35 days", "31x", "0s", "h12", "24h 1s", "1h 2h", "1h purchase", None]:
            self.assertIsNone(page.cooldown_seconds(text))

    def test_availability_keeps_original_server_deadline(self):
        now = 1792167000
        original = now + 1234
        value = datetime.fromtimestamp(original, timezone.utc).isoformat()
        parsed = page.availability({"status":200, "vote":{"available":False, "nextEligibleAt":value}}, now)
        self.assertEqual(parsed["retry_at"], original + 30)
        self.assertIsNone(page.availability({"status":403, "vote":{"available":False}}, now)["available"])
        self.assertIsNone(page.eligible_at("2026-10-16T00:00:00", now))

    def test_oauth_cannot_authorize_a_different_app_or_callback(self):
        from urllib.parse import urlencode
        query = {"client_id":page.BOT_ID, "redirect_uri":"https://anigame.gg/api/auth/discord/callback",
                 "response_type":"code", "scope":"identify email guilds"}
        self.assertTrue(client.valid_oauth("https://discord.com/oauth2/authorize?" + urlencode(query)))
        for key, value in [("client_id","123"),("redirect_uri","https://evil/callback"),("scope","identify administrator")]:
            self.assertFalse(client.valid_oauth("https://discord.com/oauth2/authorize?" + urlencode({**query,key:value})))

    def test_optional_cookies_do_not_import_topgg_or_cloudflare_state(self):
        self.assertEqual(client.load_cookies("", 1), [[]])
        cookies = [{"name":"session","value":"PRIVATE","domain":".anigame.gg"},
                   {"name":"cf_clearance","value":"DEVICE_BOUND","domain":".anigame.gg"}]
        self.assertEqual(len(client.load_cookies(json.dumps(cookies), 1)[0]), 1)
        cookies[0]["domain"] = ".top.gg"
        with self.assertRaisesRegex(ValueError, "AniGame"):
            client.load_cookies(json.dumps(cookies), 1)

    def test_authenticated_cookie_cannot_select_another_discord_account(self):
        user = "196428430126870001"
        token = base64.b64encode(user.encode()).decode().rstrip("=") + ".rest"
        self.assertTrue(client.session_matches({"status":200,"authenticated":True,"user_id":user}, token))
        self.assertFalse(client.session_matches({"status":200,"authenticated":True,"user_id":"196428430126870002"}, token))


READY = {"kind":"vote","ready":True,"visible":True,"vote_enabled":True,"vote_present":True}
RECEIPT = {"kind":"confirmation","ready":True,"visible":True,"confirmed":True}


class AniGameFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_waits_for_the_voting_component_to_mount(self):
        initial = {"kind":"vote", "ready":True, "visible":True}
        states = AsyncMock(side_effect=[initial, READY])
        with patch.object(client,"snapshot",states), \
             patch.object(client,"availability",AsyncMock(return_value={"available":True,"retry_at":None})), \
             patch.object(client.browser_environment,"foreground",AsyncMock()), \
             patch.object(client,"POLL_INTERVAL",0):
            outcome = await client.vote_once(MagicMock(), inspect_only=True)
        self.assertEqual(outcome["status"], "inspected")
        self.assertEqual(states.await_count, 2)

    async def test_inspection_never_sends_vote_input(self):
        with patch.object(client, "snapshot", AsyncMock(return_value=READY)), \
             patch.object(client, "availability", AsyncMock(return_value={"available":True,"retry_at":None})), \
             patch.object(client.ui_click, "click_control", AsyncMock()) as click, \
             patch.object(client.browser_environment, "foreground", AsyncMock()):
            journal = MagicMock()
            outcome = await client.vote_once(MagicMock(), journal, inspect_only=True)
        self.assertEqual(outcome["status"], "inspected")
        click.assert_not_called()
        journal.before_press.assert_not_called()

    async def test_one_click_starts_registration_and_never_reloads(self):
        with patch.object(client, "snapshot", AsyncMock(return_value=READY)), \
             patch.object(client, "availability", AsyncMock(return_value={"available":True,"retry_at":None})), \
             patch.object(client.ui_click, "click_control", AsyncMock(return_value={"input_sent":True,"clicked":True})) as click, \
             patch.object(client.browser_environment, "foreground", AsyncMock()), \
             patch.object(client, "wait_for_result", AsyncMock(return_value={"status":"success"})) as confirm, \
             patch.object(client.core, "navigate_page", AsyncMock()) as navigate:
            journal = MagicMock()
            outcome = await client.vote_once(MagicMock(), journal)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(click.await_count, 1)
        journal.before_press.assert_called_once()
        confirm.assert_awaited_once()
        navigate.assert_not_called()

    async def test_lost_click_receipt_still_observes_result_after_input(self):
        with patch.object(client, "snapshot", AsyncMock(return_value=READY)), \
             patch.object(client, "availability", AsyncMock(return_value={"available":True,"retry_at":None})), \
             patch.object(client.ui_click, "click_control", AsyncMock(return_value={"input_sent":True,"clicked":False})) as click, \
             patch.object(client.browser_environment, "foreground", AsyncMock()), \
             patch.object(client, "wait_for_result", AsyncMock(return_value={"status":"success"})):
            outcome = await client.vote_once(MagicMock())
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(click.await_count, 1)

    async def test_existing_cooldown_does_not_reset_twelve_hours(self):
        state = {**READY,"vote_enabled":False,"cooldown_seconds":15660}
        exact = int(time.time()) + 300
        with patch.object(client, "snapshot", AsyncMock(return_value=state)), \
             patch.object(client, "availability", AsyncMock(return_value={"available":False,"retry_at":exact})), \
             patch.object(client.ui_click, "click_control", AsyncMock()) as click, \
             patch.object(client.browser_environment, "foreground", AsyncMock()), patch.object(client,"POLL_INTERVAL",0):
            outcome = await client.vote_once(MagicMock())
        self.assertEqual(outcome["retry_at"], exact)
        self.assertEqual(outcome["status"], "cooldown")
        click.assert_not_called()

    async def test_old_or_wrong_page_text_is_not_a_confirmation(self):
        with patch.object(client,"snapshot",AsyncMock(return_value={**RECEIPT,"kind":"vote"})), \
             patch.object(client,"RESULT_TIMEOUT",.02), patch.object(client,"POLL_INTERVAL",.001):
            outcome = await client.wait_for_result(MagicMock())
        self.assertEqual(outcome["status"], "uncertain")

    async def test_receipt_is_accepted_when_optional_api_is_unavailable(self):
        with patch.object(client,"snapshot",AsyncMock(return_value=RECEIPT)), \
             patch.object(client,"availability",AsyncMock(return_value={"available":None,"retry_at":None})), \
             patch.object(client,"POLL_INTERVAL",0):
            outcome = await client.wait_for_result(MagicMock())
        self.assertEqual(outcome["status"], "success")

    async def test_receipt_contradicted_by_fresh_available_state_is_not_accepted(self):
        with patch.object(client,"snapshot",AsyncMock(return_value=RECEIPT)), \
             patch.object(client,"availability",AsyncMock(return_value={"available":True,"retry_at":None})), \
             patch.object(client,"RESULT_TIMEOUT",.02), patch.object(client,"POLL_INTERVAL",.001):
            outcome = await client.wait_for_result(MagicMock())
        self.assertEqual(outcome["status"], "uncertain")

    async def test_all_five_account_attempts_are_available_after_blocks(self):
        browser = MagicMock()
        browser.tabs = [MagicMock()]
        with patch.object(client.core,"start_browser",AsyncMock(return_value=browser)) as start, \
             patch.object(client.core,"current_url",AsyncMock(return_value=page.VOTE_URL)), \
             patch.object(client.core,"close_browser_safely",AsyncMock(return_value=True)) as close, \
             patch.object(client,"authenticate",AsyncMock(return_value=(False,"blocked"))), \
             patch.object(client,"RETRY_DELAY",0):
            outcome = await client.process_account("PRIVATE", [])
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(start.await_count, 5)
        self.assertEqual(close.await_count, 5)

    async def test_exhausted_browser_startup_is_delegated_to_fresh_action(self):
        with patch.object(client.core,"start_browser",AsyncMock(side_effect=client.core.BrowserStartupError("fixture"))) as start:
            outcome = await client.process_account("PRIVATE", [])
        self.assertEqual(outcome["status"], "error")
        self.assertTrue(outcome["detail"].startswith("Browser startup failed:"))
        self.assertTrue(client.core.should_request_browser_startup_retry([[outcome]]))
        self.assertEqual(start.await_count, 1)

    async def test_completed_handoff_skips_browser_and_preserves_schedule(self):
        journal = MagicMock()
        journal.select.return_value = {"kind":"complete","until":int(time.time())+321}
        with patch.object(client.core,"start_browser",AsyncMock()) as start:
            outcome = await client.process_account("PRIVATE", [], journal)
        self.assertEqual(outcome["retry_at"], journal.select.return_value["until"])
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
