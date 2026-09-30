"""Readiness, session recovery and browser ownership regressions."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import vote


BOT = "111"
URL = "https://top.gg/bot/111/vote"
DENIED = {"authenticated": False, "status": 403, "content_type": "text/html",
          "cf_mitigated": "challenge"}


class ClearanceReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_loading_document_cannot_clear_a_challenge(self):
        with (patch("vote.document_ready", new=AsyncMock(return_value=False)),
              patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)) as present,
              patch("vote.is_turnstile_solved", new=AsyncMock(return_value=True)) as solved):
            self.assertFalse(await vote.stable_challenge_clearance(AsyncMock()))
        present.assert_not_awaited()
        solved.assert_not_awaited()

    async def test_clearance_must_survive_the_second_document_observation(self):
        for ready in ([True, False], [True, True]):
            with (self.subTest(ready=ready),
                  patch("vote.document_ready", new=AsyncMock(side_effect=ready)),
                  patch("vote.is_turnstile_solved", new=AsyncMock(return_value=False)),
                  patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
                  patch("vote.asyncio.sleep", new_callable=AsyncMock)):
                self.assertEqual(await vote.stable_challenge_clearance(AsyncMock()), all(ready))

    async def test_returning_challenge_invalidates_a_clear_signal(self):
        with (patch("vote.document_ready", new=AsyncMock(return_value=True)),
              patch("vote.is_turnstile_solved", new=AsyncMock(return_value=False)),
              patch("vote.is_turnstile_present", new=AsyncMock(side_effect=[False, True])),
              patch("vote.asyncio.sleep", new_callable=AsyncMock)):
            self.assertFalse(await vote.stable_challenge_clearance(AsyncMock()))

    async def test_checkbox_wait_callback_does_not_accept_a_loading_document(self):
        async def click(_tab, _evaluate, cleared):
            self.assertFalse(await cleared())
            return "unavailable"
        with (patch("vote.document_ready", new=AsyncMock(return_value=False)),
              patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
              patch("vote.cloudflare_click.click_cloudflare_checkbox", new=AsyncMock(side_effect=click))):
            self.assertEqual(await vote._click_cloudflare_checkbox(AsyncMock()), "unavailable")


class SessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mocks = {}
        for name, value in {
            "dismiss_privacy_overlay": False, "settle_privacy_overlay": False,
            "current_url": URL, "document_ready": True, "is_turnstile_present": False,
            "topgg_page_auth_hint": "unknown", "topgg_session_probe": DENIED,
            "solve_turnstile": True, "asyncio.sleep": None,
        }.items():
            p = patch("vote." + name, new=AsyncMock(return_value=value))
            self.mocks[name] = p.start(); self.addCleanup(p.stop)
        p = patch("builtins.print"); p.start(); self.addCleanup(p.stop)

    async def test_late_application_controls_recover_without_reload_or_another_fetch(self):
        self.mocks["topgg_page_auth_hint"].side_effect = ["unknown", "unknown", vote.AUTHENTICATED]
        tab = AsyncMock()
        self.assertEqual(await vote.topgg_auth_state(tab), vote.AUTHENTICATED)
        tab.reload.assert_not_awaited()
        self.mocks["topgg_session_probe"].assert_awaited_once()

    async def test_fetch_only_challenge_can_render_through_one_normal_page_reload(self):
        self.mocks["topgg_session_probe"].side_effect = [DENIED, {"authenticated": True}]
        tab = AsyncMock()
        self.assertEqual(await vote.topgg_auth_state(tab), vote.AUTHENTICATED)
        tab.reload.assert_awaited_once()
        self.assertEqual(self.mocks["topgg_session_probe"].await_count, 2)

    async def test_persistent_fetch_denial_has_no_reload_loop(self):
        tab = AsyncMock()
        self.assertEqual(await vote.topgg_auth_state(tab), vote.AUTH_BLOCKED)
        tab.reload.assert_awaited_once()
        self.assertEqual(self.mocks["topgg_session_probe"].await_count, 2)

    async def test_api_callback_or_other_origin_is_never_reopened(self):
        for url in ["https://top.gg/api/auth/session", "https://top.gg/api/auth/callback/discord?code=secret",
                    "https://discord.com/oauth2/authorize", "http://top.gg/bot/111/vote"]:
            with self.subTest(url=url):
                self.mocks["current_url"].return_value = url
                tab = AsyncMock()
                self.assertEqual(await vote.topgg_auth_state(tab), vote.AUTH_BLOCKED)
                tab.reload.assert_not_awaited()

    async def test_challenge_appearing_after_fetch_gets_one_solver_attempt(self):
        self.mocks["is_turnstile_present"].side_effect = [False, True, False]
        self.mocks["topgg_session_probe"].side_effect = [DENIED, {"authenticated": True}]
        self.assertEqual(await vote.topgg_auth_state(AsyncMock()), vote.AUTHENTICATED)
        self.mocks["solve_turnstile"].assert_awaited_once()

    async def test_loading_probe_waits_for_document_before_retrying(self):
        self.mocks["topgg_session_probe"].side_effect = [
            {"authenticated": False, "status": 0, "error": "document-loading"},
            {"authenticated": True},
        ]
        tab = AsyncMock()
        self.assertEqual(await vote.topgg_auth_state(tab), vote.AUTHENTICATED)
        tab.reload.assert_not_awaited()


def browser_fixture():
    tab = MagicMock()
    tab.send = AsyncMock()
    browser = MagicMock()
    browser.__iter__.return_value = [tab]
    return browser, tab


class BrowserReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser, self.tab = browser_fixture()
        self.patches = {}
        for name, value in {
            "start_browser": self.browser, "login_with_cookies": vote.AUTHENTICATED,
            "topgg_auth_state": vote.AUTHENTICATED, "topgg_page_auth_hint": vote.AUTHENTICATED,
            "is_turnstile_present": False, "document_ready": True, "current_url": URL,
            "browser_screenshot": None, "close_browser_safely": True, "asyncio.sleep": None,
        }.items():
            p = patch("vote." + name, new=AsyncMock(return_value=value))
            self.patches[name] = p.start(); self.addCleanup(p.stop)
        p = patch("builtins.print"); p.start(); self.addCleanup(p.stop)

    async def test_recoverable_error_reuses_profile_and_retries_only_pending_bot(self):
        actions = [{"bot_id": "111", "status": "success"},
                   {"bot_id": "222", "status": "error", "vote_submitted": False},
                   {"bot_id": "222", "status": "success"}]
        with patch("vote.vote_for_bot", new=AsyncMock(side_effect=actions)) as cast:
            result = await vote.process_account("token", ["111", "222"], 1, 1, [{"name": "authjs"}])
        self.assertEqual([x["status"] for x in result], ["success", "success"])
        self.assertEqual([c.args[1] for c in cast.await_args_list], ["111", "222", "222"])
        self.patches["start_browser"].assert_awaited_once()
        self.patches["login_with_cookies"].assert_awaited_once()
        self.patches["close_browser_safely"].assert_awaited_once()
        self.patches["topgg_auth_state"].assert_awaited_once_with(self.tab)

    async def test_blocked_browser_is_replaced_even_when_it_was_authenticated(self):
        other, _ = browser_fixture()
        self.patches["start_browser"].side_effect = [self.browser, other]
        with patch("vote.vote_for_bot", new=AsyncMock(side_effect=[
            {"bot_id": BOT, "status": "blocked"}, {"bot_id": BOT, "status": "success"},
        ])):
            result = await vote.process_account("token", [BOT], 1, 1, [{"name": "authjs"}])
        self.assertEqual(result[0]["status"], "success")
        self.assertEqual(self.patches["start_browser"].await_count, 2)
        self.assertEqual(self.patches["close_browser_safely"].await_count, 2)

    async def test_unconfirmed_submission_is_not_repeated_when_another_bot_retries(self):
        other, _ = browser_fixture()
        self.patches["start_browser"].side_effect = [self.browser, other]
        with patch("vote.vote_for_bot", new=AsyncMock(side_effect=[
            {"bot_id": BOT, "status": "uncertain", "vote_submitted": True},
            {"bot_id": "222", "status": "error"}, {"bot_id": "222", "status": "success"},
        ])) as cast:
            result = await vote.process_account("token", [BOT, "222"], 1, 1, [{"name": "authjs"}])
        self.assertEqual([x["status"] for x in result], ["uncertain", "success"])
        self.assertEqual([c.args[1] for c in cast.await_args_list], [BOT, "222", "222"])
        self.assertEqual(self.patches["start_browser"].await_count, 2)

    async def test_lost_document_connection_prevents_reuse(self):
        self.patches["document_ready"].return_value = False
        session = vote.AccountBrowserSession()
        session.browser, session.tab, session.authenticated = self.browser, self.tab, True
        self.assertFalse(await session.reusable())
        await session.close()
        self.patches["close_browser_safely"].assert_awaited_once()

    async def test_final_cancellation_closes_the_owned_profile(self):
        import asyncio
        with patch("vote.vote_for_bot", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await vote.process_account("token", [BOT], 1, 1, [{"name": "authjs"}])
        self.patches["close_browser_safely"].assert_awaited_once()


class AuthCookieResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_auth_cookies_on_topgg_are_deleted(self):
        browser, tab = browser_fixture()
        values = [
            ("__Secure-authjs.session-token.0", ".top.gg"), ("__Host-authjs.csrf-token", "top.gg"),
            ("next-auth.callback-url", "www.top.gg"), ("cf_clearance", ".top.gg"),
            ("consent", ".top.gg"), ("authjs.session-token", "discord.com"),
        ]
        browser.cookies.get_all = AsyncMock(return_value=[
            SimpleNamespace(name=name, domain=domain, path="/", value="DO-NOT-LOG") for name, domain in values
        ])
        await vote.clear_topgg_auth_cookies(browser)
        commands = [next(call.args[0]) for call in tab.send.await_args_list]
        self.assertEqual([c["params"]["name"] for c in commands], [v[0] for v in values[:3]])
        self.assertTrue(all(c["method"] == "Network.deleteCookies" for c in commands))
        self.assertNotIn("DO-NOT-LOG", json.dumps(commands))
        browser.cookies.clear.assert_not_called()
