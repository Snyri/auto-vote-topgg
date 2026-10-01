"""Real pinned container/CDP handoff and local OAuth/vote regression fixtures.

Run in the dedicated CI job, never against a live voting site. Every OAuth and
vote response is fulfilled locally by the existing production-flow fixtures.
"""

import asyncio
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import nodriver as uc

import flaresolverr_browser as provider
import vote
from test_oauth_flow import OAuthBrowserTests
from test_live_vote_flow import LiveVoteFlowTests


class RealSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_cookie_page_identity_and_destruction(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b'<!doctype html><title>Local session fixture</title><p id="identity">original page</p>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "fixture_session=preserved; Path=/; HttpOnly")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            url = f"http://127.0.0.1:{server.server_port}/fixture"
            browser = await provider.start(url)
            tab = next(iter(browser))
            self.assertEqual(await vote.current_url(tab), url)
            self.assertEqual(await vote.evaluate(tab, "document.querySelector('#identity').textContent"), "original page")
            self.assertIsNone(browser._process)
            cookies = await browser.cookies.get_all()
            self.assertTrue(any(item.name == "fixture_session" and item.value == "preserved" for item in cookies))
            profile = browser._security_profile_path
            session_id = browser._flare_client.session_id
            await browser.aclose()
            listed = await browser._flare_client.request("sessions.list")
            self.assertNotIn(session_id, listed["sessions"])
            self.assertFalse(os.path.exists(profile))
            # A second account receives a new Chrome/session, without prior auth.
            other = await provider.start()
            try:
                self.assertNotEqual(other._flare_client.session_id, session_id)
                self.assertFalse(any(item.name == "fixture_session" for item in await other.cookies.get_all()))
            finally:
                await other.aclose()
        finally:
            if browser:
                await browser.aclose()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(2)


class SameBrowserMixin:
    async def asyncSetUp(self):
        browser = await provider.start()
        self._service_browser = browser
        try:
            # Existing fixtures use the actual service Chrome; no local Chrome
            # process is launched. start() on this client is idempotent.
            with patch.object(uc, "Browser", return_value=browser):
                await super().asyncSetUp()
            self.assertIs(self.browser, browser)
            self.assertIsNone(self.browser._process)
        except BaseException:
            await browser.aclose()
            raise

    async def asyncTearDown(self):
        try:
            await super().asyncTearDown()
        finally:
            await self._service_browser.aclose()
            listed = await self._service_browser._flare_client.request("sessions.list")
            self.assertNotIn(self._service_browser._flare_client.session_id, listed["sessions"])


class ServiceOAuthTests(SameBrowserMixin, OAuthBrowserTests):
    pass


class ServiceVoteTests(SameBrowserMixin, LiveVoteFlowTests):
    pass


if __name__ == "__main__":
    os.environ["FLARESOLVERR_URL"] = "http://127.0.0.1:8191"
    suite = unittest.TestSuite()
    for cls in (RealSessionTests, ServiceOAuthTests, ServiceVoteTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Required service/browser fixtures were skipped")
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
