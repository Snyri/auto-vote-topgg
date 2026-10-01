"""Service contracts, failed-solver handoff, fallback and secret isolation."""

import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import flaresolverr_bridge as bridge
import flaresolverr_browser as provider
import vote


class BridgeTests(unittest.TestCase):
    def test_connection_uses_existing_driver_and_current_page_without_creating_a_session(self):
        driver = NS(capabilities={"goog:chromeOptions": {"debuggerAddress": "localhost:12345"}},
                    current_window_handle="CDwindow-" + "A" * 32)
        storage = NS(sessions={"existing": NS(driver=driver)}, get=MagicMock(), create=MagicMock())
        self.assertEqual(bridge.browser_connection(storage, "existing"),
                         {"debuggerAddress": "127.0.0.1:12345", "targetId": "A" * 32})
        storage.get.assert_not_called()
        storage.create.assert_not_called()
        with self.assertRaises(ValueError):
            bridge.browser_connection(storage, "missing")
        self.assertEqual(list(storage.sessions), ["existing"])

    def test_bridge_rejects_external_debugger_and_preserves_standard_commands(self):
        driver = NS(capabilities={"goog:chromeOptions": {"debuggerAddress": "external.invalid:12345"}},
                    current_window_handle="A" * 32)
        service = NS(SESSIONS_STORAGE=NS(sessions={"fixture": NS(driver=driver)}),
                     _controller_v1_handler=MagicMock(return_value="original"))
        original = service._controller_v1_handler
        bridge.install(service, lambda data: data)
        req = NS(cmd="sessions.create", session="fixture")
        self.assertEqual(service._controller_v1_handler(req), "original")
        original.assert_called_once_with(req)
        with self.assertRaises(ValueError):
            service._controller_v1_handler(NS(cmd="sessions.connect", session="fixture"))

    def test_service_endpoint_is_local_and_uses_no_paid_credential(self):
        for url in ("https://paid.invalid", "http://user:secret@127.0.0.1:8191",
                    "http://127.0.0.1:8191/?token=secret", "http://127.0.0.1", "http://localhost:8191"):
            with patch.dict(os.environ, {"FLARESOLVERR_URL": url}):
                with self.assertRaises(provider.ServiceError):
                    provider.service_url()
        with patch.dict(os.environ, {"FLARESOLVERR_URL": "http://127.0.0.1:8191"}):
            self.assertEqual(provider.service_url(), "http://127.0.0.1:8191")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_handoff(self, solver_status):
        selected = NS(target=NS(target_id="A" * 32, url="https://top.gg/redirected"), attach=AsyncMock())
        support = NS(target=NS(target_id="B" * 32, url="about:blank"), attach=AsyncMock())
        browser = NS(tabs=[support, selected], targets=[support, selected], start=AsyncMock(), aclose=AsyncMock())
        client = provider.Client("http://127.0.0.1:8191")
        client.request = AsyncMock(side_effect=[{"status": "ok", "session": client.session_id},
            {"status": solver_status, "solution": {"status": 200, "cookies": [{"name": "unused"}]}},
            {"status": "ok", "solution": {"debuggerAddress": "127.0.0.1:12345", "targetId": "A" * 32}}])
        client.destroy = AsyncMock()
        with (patch("flaresolverr_browser.service_url", return_value=client.base_url),
              patch("flaresolverr_browser.Client", return_value=client),
              patch("flaresolverr_browser.SessionBrowser", return_value=browser),
              patch("flaresolverr_browser.tempfile.mkdtemp", return_value="/fixture/client-profile"),
              patch("builtins.print")):
            self.assertIs(await provider.start("https://top.gg/bot/111/vote"), browser)
        self.assertIs(browser.targets[0], selected)
        selected.attach.assert_awaited_once()
        support.attach.assert_not_awaited()
        request = client.request.await_args_list[1]
        self.assertEqual(request.args[0], "request.get")
        self.assertEqual(request.kwargs["url"], "https://top.gg/bot/111/vote")
        self.assertFalse(request.kwargs["disableMedia"])
        self.assertNotIn("cookies", request.kwargs)
        self.assertNotIn("headers", request.kwargs)
        client.destroy.assert_not_awaited()

    async def test_failed_solver_keeps_the_existing_page_for_actual_application_checks(self):
        await self.exercise_handoff("error")

    async def test_solver_http_200_does_not_reimport_cookies_or_select_a_support_tab(self):
        await self.exercise_handoff("ok")

    async def test_bad_connection_response_destroys_only_the_new_session(self):
        client = provider.Client("http://127.0.0.1:8191")
        client.request = AsyncMock(side_effect=[{"status": "ok", "session": client.session_id},
                                               {"status": "ok", "solution": None}])
        client.destroy = AsyncMock()
        with (patch("flaresolverr_browser.service_url", return_value=client.base_url),
              patch("flaresolverr_browser.Client", return_value=client)):
            with self.assertRaises(provider.ServiceError):
                await provider.start()
        client.destroy.assert_awaited_once()
        self.assertNotEqual(client.session_id, provider.Client(client.base_url).session_id)

    async def test_service_failure_uses_normal_browser_instead_of_creating_a_new_gate(self):
        browser = MagicMock()
        browser.start = AsyncMock()
        browser.get = AsyncMock()
        browser.__iter__.return_value = iter([NS()])
        with (patch.dict(os.environ, {"FLARESOLVERR_URL": "http://127.0.0.1:8191"}),
              patch("vote.flaresolverr_browser.start", new=AsyncMock(side_effect=provider.ServiceError("failure"))),
              patch("vote.browser_environment.ChromeConfig"), patch("vote.uc.Browser", return_value=browser),
              patch("vote.browser_environment.log_facts", new=AsyncMock()),
              patch("vote.tempfile.mkdtemp", return_value="/fixture/normal-profile"), patch("builtins.print")):
            self.assertIs(await vote.start_browser(initial_url="https://top.gg/bot/111/vote"), browser)
        browser.start.assert_awaited_once()

    async def test_account_session_passes_public_vote_url_to_provider_before_authentication(self):
        browser = MagicMock()
        browser.__iter__.return_value = iter([NS()])
        session = vote.AccountBrowserSession()
        with (patch("vote.start_browser", new=AsyncMock(return_value=browser)) as start,
              patch("vote.request_diagnostics.RequestDiagnostics", return_value=NS(start=AsyncMock(return_value=True))),
              patch("vote.request_diagnostics.set_phase")):
            await session.acquire(initial_url="https://top.gg/bot/111/vote")
        start.assert_awaited_once_with(initial_url="https://top.gg/bot/111/vote")

