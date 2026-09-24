"""The inspection path never submits a vote or returns private page content."""

import asyncio
import json
import os
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import inspect_vote_page as inspect_page
import vote
from test_vote_page_receipt import DOM_HARNESS


class SnapshotPrivacyTests(unittest.TestCase):
    def test_allowlist_discards_raw_values_and_bounds_controls_and_numbers(self):
        secret = "private-token-or-username"
        raw = {"title_class": secret, "current_path": secret, "ready_state": secret,
               "strong_success": secret, "body": secret, "cooldown_seconds": 86401,
               "controls": [{"label": secret, "href_kind": secret, "tag": secret,
                             "disabled": secret, "role_button": 1, "href": secret}] * 20}
        actual = inspect_page.sanitize_snapshot(raw)
        self.assertNotIn(secret, json.dumps(actual))
        self.assertEqual(len(actual["controls"]), 12)
        self.assertIsNone(actual["cooldown_seconds"])
        self.assertIsNone(actual["strong_success"])
        self.assertIsNone(actual["controls"][0]["disabled"])

    def test_malformed_data_never_escapes(self):
        for raw in (None, True, [], "private-value"):
            self.assertEqual(inspect_page.sanitize_snapshot(raw), {"observed": False})
        self.assertIsNone(inspect_page.sanitize_snapshot({"cooldown_seconds": True})["cooldown_seconds"])


class InspectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_cookie_session_and_navigation_without_tokens_or_mutations(self):
        tab = AsyncMock()
        browser = MagicMock()
        browser.__iter__.return_value = iter([tab])
        cookies = [{"name": "authjs.session-token", "value": "secret-session"}]
        with ExitStack() as stack:
            for name in ("DEBUG", "SEND_ERROR_SCREENSHOTS", "TG_BOT_TOKEN", "TG_CHAT_ID", "PRIVACY_DISMISS_REPORTED", "SENSITIVE_VALUES"):
                stack.enter_context(patch.object(vote, name, getattr(vote, name)))
            stack.enter_context(patch.dict(os.environ, {"TOKENS": "must-not-be-read"}, clear=False))
            printer = stack.enter_context(patch("builtins.print"))
            consume = stack.enter_context(patch("vote.consume_secret", return_value="[]\nconfigured"))
            stack.enter_context(patch("vote.load_topgg_cookies", return_value=[[], cookies, cookies]))
            stack.enter_context(patch("vote.load_bot_ids", return_value=["111", "222"]))
            stack.enter_context(patch("vote.start_browser", new=AsyncMock(return_value=browser)))
            inject = stack.enter_context(patch("vote.inject_topgg_cookies", new=AsyncMock()))
            close = stack.enter_context(patch("vote.close_browser", new=AsyncMock()))
            sleep = stack.enter_context(patch("inspect_vote_page.asyncio.sleep", new=AsyncMock()))
            stack.enter_context(patch("vote.settle_privacy_overlay", new=AsyncMock()))
            snapshot = stack.enter_context(patch("inspect_vote_page.page_snapshot", new=AsyncMock(return_value={"observed": True})))
            stack.enter_context(patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value="private-unexpected")))
            stack.enter_context(patch("vote.vote_page_confirmation", new=AsyncMock(return_value={"evidence": "private-unexpected"})))
            probe = stack.enter_context(patch("vote.topgg_session_probe", new=AsyncMock(return_value={})))
            forbidden = [stack.enter_context(patch("vote." + name, new=AsyncMock())) for name in
                         ("vote_for_bot", "solve_turnstile", "discord_oauth_login", "browser_screenshot", "send_captcha_screenshots")]
            notifications = stack.enter_context(patch("vote.send_notification"))
            artifacts = stack.enter_context(patch("vote.write_next_vote_state"))
            self.assertEqual(await inspect_page.main(), 0)
            self.assertNotIn("TOKENS", os.environ)
        consume.assert_called_once_with("TOPGG_COOKIES_JSON")
        inject.assert_awaited_once_with(browser, cookies)
        tab.get.assert_awaited_once_with("https://top.gg/bot/111/vote")
        tab.reload.assert_not_awaited()
        tab.verify_cf.assert_not_awaited()
        close.assert_awaited_once_with(browser)
        self.assertEqual(snapshot.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3, 10, 20])
        probe.assert_awaited_once_with(tab)
        for operation in forbidden:
            operation.assert_not_awaited()
        notifications.assert_not_called()
        artifacts.assert_not_called()
        output = "\n".join(str(call.args) for call in printer.call_args_list)
        self.assertNotIn("secret-session", output)
        self.assertNotIn("private-unexpected", output)

    async def test_snapshot_exception_is_redacted(self):
        with patch("vote.evaluate", new=AsyncMock(side_effect=RuntimeError("private-value"))), patch("builtins.print") as printer:
            self.assertEqual(await inspect_page.page_snapshot(MagicMock(), "111"), {"observed": False})
        self.assertNotIn("private-value", str(printer.call_args_list))


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for actual DOM fixtures")
class SnapshotJavaScriptTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, fixture):
        with patch("vote.evaluate", new=AsyncMock(return_value=None)) as evaluate:
            await inspect_page.page_snapshot(MagicMock(), "111")
        process = await asyncio.to_thread(subprocess.run, [shutil.which("node"), "-e", DOM_HARNESS],
            input=json.dumps({"expression": evaluate.await_args.args[1], "fixture": fixture}),
            capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return inspect_page.sanitize_snapshot(json.loads(process.stdout))

    async def test_public_vote_link_is_identified_separately_from_an_actual_button(self):
        actual = await self.inspect({"text": "Private account", "elements": [
            {"tag": "a", "text": "Vote", "attrs": {"href": "/bot/111/vote"}},
            {"tag": "button", "text": "Vote", "disabled": True},
            {"tag": "a", "text": "Login", "attrs": {"href": "https://discord.com/oauth2/authorize?private=value"}},
        ]})
        self.assertEqual(actual["current_path"], "same_bot_vote")
        self.assertEqual([item["tag"] for item in actual["controls"]], ["a", "button", "a"])
        self.assertEqual([item["href_kind"] for item in actual["controls"]], ["same_bot_vote", "none", "discord_oauth"])
        self.assertTrue(actual["controls"][1]["disabled"])
        self.assertNotIn("private", json.dumps(actual).lower())

    async def test_cooldown_and_challenge_only_return_fixed_signals(self):
        actual = await self.inspect({"text": "You can vote again in 11 hours 30 minutes. private-user", "title": "Just a moment..."})
        self.assertTrue(actual["challenge"])
        self.assertTrue(actual["cooldown"])
        self.assertEqual(actual["cooldown_seconds"], 41400)
        self.assertNotIn("private-user", json.dumps(actual))


if __name__ == "__main__":
    unittest.main()
