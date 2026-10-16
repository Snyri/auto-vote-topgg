"""Inspection request cancellation, duplicate events and write prevention."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import nodriver as uc
from nodriver.core.connection import ProtocolException

import anigame_vote as client
import browser_runtime as core
from browser_test_support import CHROME


def request(request_id="request", *, method="GET", path="/vote"):
    return NS(request_id=uc.cdp.fetch.RequestId(request_id), request=NS(
        url="https://www.anigame.gg" + path, method=method))


class InspectionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_expired_interception_error_is_benign(self):
        expired = ProtocolException({"code": -32602, "message": "Invalid InterceptionId."})
        self.assertTrue(core.is_expired_interception(expired))
        for error in [RuntimeError("Invalid InterceptionId."),
                      ProtocolException({"code": -32000, "message": "Invalid InterceptionId."}),
                      ProtocolException({"code": -32602, "message": "Other protocol failure"})]:
            self.assertFalse(core.is_expired_interception(error))

    async def test_cancelled_request_does_not_escape_the_event_callback(self):
        tab = NS(send=AsyncMock(side_effect=ProtocolException(
            {"code": -32602, "message": "Invalid InterceptionId."})))
        guard = client.ReadOnlyGuard(tab)
        await guard.paused(request())
        self.assertEqual(guard.expired, 1)
        self.assertIsNone(guard.failure)

    async def test_duplicate_events_do_not_resume_a_request_twice(self):
        tab = NS(send=AsyncMock())
        guard = client.ReadOnlyGuard(tab)
        event = request()
        await asyncio.gather(guard.paused(event), guard.paused(event))
        tab.send.assert_awaited_once()

    async def test_an_application_write_stays_blocked_after_duplicate_delivery(self):
        commands = []
        async def send(command):
            commands.append(next(command))
        guard = client.ReadOnlyGuard(NS(send=send))
        event = request(method="POST", path="/api/vote/register")
        await asyncio.gather(guard.paused(event), guard.paused(event))
        self.assertEqual(guard.denied, 1)
        self.assertEqual([command["method"] for command in commands], ["Fetch.failRequest"])

    async def test_other_protocol_errors_are_reported_without_private_details(self):
        secret = "PRIVATE_FIXTURE_VALUE"
        tab = NS(send=AsyncMock(side_effect=ProtocolException({"code": -32602, "message": secret})))
        guard = client.ReadOnlyGuard(tab)
        with patch("builtins.print") as output:
            await guard.paused(request())
        self.assertEqual(guard.failure, "ProtocolException")
        self.assertNotIn(secret, str(output.call_args_list))

    async def test_unhealthy_inspection_cannot_return_a_successful_result(self):
        tab = MagicMock()
        browser = NS(tabs=[tab])
        guard = NS(start=AsyncMock(), failure="ProtocolException")
        with patch.object(client.core, "start_browser", AsyncMock(return_value=browser)), \
             patch.object(client.core, "current_url", AsyncMock(return_value=client.page.VOTE_URL)), \
             patch.object(client.core, "close_browser_safely", AsyncMock()), \
             patch.object(client, "ReadOnlyGuard", return_value=guard), \
             patch.object(client, "authenticate", AsyncMock(return_value=(True, None))), \
             patch.object(client, "vote_once", AsyncMock(return_value={"status": "inspected"})):
            result = await client.process_account("fixture-token", [], inspect_only=True)
        self.assertEqual(result["status"], "error")

    async def test_an_active_challenge_cannot_fall_through_to_vote_input(self):
        with patch.object(client, "snapshot", AsyncMock(return_value={"challenge": True})), \
             patch.object(client, "clear_challenge", AsyncMock(return_value=False)), \
             patch.object(client.browser_environment, "foreground", AsyncMock()), \
             patch.object(client.ui_click, "click_control", AsyncMock()) as click:
            result = await client.vote_once(MagicMock())
        self.assertEqual(result["status"], "blocked")
        click.assert_not_called()


@unittest.skipUnless(CHROME, "Chrome required for real inspection request interception")
class InspectionGuardBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        received = self.received
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                body = b'<!doctype html><title>Local inspection</title><main>Request fixture</main>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.profile = tempfile.TemporaryDirectory(prefix="inspection-guard-")
        self.browser = uc.Browser(uc.Config(headless=False, browser_executable_path=CHROME,
            user_data_dir=self.profile.name, sandbox=getattr(os, "geteuid", lambda: 1)() != 0,
            browser_args=["--disable-dev-shm-usage", "--no-proxy-server"]))
        try:
            try:
                await asyncio.wait_for(self.browser.start(), 20)
            except Exception:
                if not await core.recover_slow_browser_start(self.browser):
                    raise
            self.tab = await self.browser.get("about:blank")
        except BaseException:
            await self.close_fixture()
            raise

    async def close_fixture(self):
        await core.close_browser_safely(self.browser, "local inspection fixture")
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(2)
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_fixture()

    async def test_actual_guard_allows_reads_and_auth_but_blocks_registration(self):
        guard = client.ReadOnlyGuard(self.tab)
        await guard.start()
        url = "http://127.0.0.1:" + str(self.server.server_port) + "/fixture"
        with patch.object(client.page, "HOSTS", frozenset({"127.0.0.1"})):
            await core.navigate_page(self.tab, url)
            for _ in range(50):
                if await core.document_ready(self.tab):
                    break
                await asyncio.sleep(.05)
            observed = await core.evaluate(self.tab, """(async () => {
                const read = await fetch('/read');
                const auth = await fetch('/api/auth/activity', {method:'POST'});
                const voteBlocked = await fetch('/api/vote/register', {method:'POST'})
                    .then(() => false).catch(() => true);
                return {read:read.ok, auth:auth.ok, voteBlocked};
            })()""")
        self.assertEqual(observed, {"read": True, "auth": True, "voteBlocked": True})
        self.assertEqual(self.received, ["/api/auth/activity"])
        self.assertEqual(guard.denied, 1)
        self.assertIsNone(guard.failure)
