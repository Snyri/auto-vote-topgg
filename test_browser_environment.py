"""Headed Chrome, actual focus, press duration and isolated cross-site frames."""

import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nodriver as uc

import browser_environment as environment
import cloudflare_click
import native_mouse
import vote
from test_vote_pointer import CHROME


class ChromeConfigurationTests(unittest.TestCase):
    def test_default_isolation_is_retained_alongside_unrelated_launch_options(self):
        config = environment.ChromeConfig(browser_executable_path="/fixture/chrome", user_data_dir="/fixture/profile",
            headless=False, sandbox=False, browser_args=["--window-size=1280,720", "--disable-features=FixtureFeature,IsolateOrigins"])
        args = config()
        self.assertIn("--disable-features=FixtureFeature", args)
        self.assertIn("--user-data-dir=/fixture/profile", args)
        self.assertIn("--window-size=1280,720", args)
        self.assertFalse(any("IsolateOrigins" in arg or "site-per-process" in arg for arg in args))
        self.assertNotIn("--disable-site-isolation-trials", args)
        self.assertFalse(config.expert)


class NativeMouseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_during_the_hold_still_releases_the_pressed_button(self):
        commands = []
        holding = asyncio.Event()
        async def send(command):
            value = next(command); commands.append(value)
        async def hold(seconds):
            self.assertEqual(seconds, 10)
            holding.set()
            await asyncio.Event().wait()
        tab = SimpleNamespace(send=send)
        with (patch.object(native_mouse, "PRESS_HOLD_SEC", 10), patch("native_mouse.asyncio.sleep", new=hold)):
            task = asyncio.create_task(native_mouse.press_and_release(tab, 50, 60))
            await holding.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual([item["params"]["type"] for item in commands], ["mousePressed", "mouseReleased"])
        self.assertEqual(commands[-1]["params"]["buttons"], 0)

    async def test_foreground_failure_is_not_a_new_input_gate(self):
        await environment.foreground(SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("private-data"))))

    async def test_environment_log_rejects_extra_values_and_fake_booleans(self):
        secret = "PRIVATE_FIXTURE_VALUE"
        raw = {"focused": True, "webgl": secret, "canvas": 1, "webgl2": False, "visibility": "visible", "cookie": secret}
        with patch("builtins.print") as output:
            await environment.log_facts(None, AsyncMock(return_value=raw), "startup")
        message = output.call_args.args[0]
        self.assertNotIn(secret, message)
        self.assertEqual(json.loads(message.split(": ", 1)[1]), {"phase":"startup", "focused":True,
            "webgl":None, "canvas":None, "webgl2":False, "visibility":"visible"})


@unittest.skipUnless(CHROME, "Chrome required for headed environment fixtures")
class HeadedChromeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="headed-isolation-fixture-")
        folder = Path(self.profile.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem"), "-subj", "/CN=local-fixture"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(handler):
                port = handler.server.server_port
                if handler.path == "/widget":
                    html = '<!doctype html><style>body{margin:0}input{position:absolute;left:30px;top:30px;width:24px;height:24px}</style>' \
                        '<input id="checkbox" type="checkbox"><script>let down=null;' \
                        'checkbox.addEventListener("pointerdown",e=>{down=performance.now()});' \
                        'checkbox.addEventListener("click",e=>parent.postMessage({trusted:e.isTrusted,hold:performance.now()-down},"*"));</script>'
                elif handler.path == "/login-fixture":
                    html = '<!doctype html><title>Login fixture</title><p>You must be logged in to vote.</p>' \
                        '<button id="login" style="margin:100px;width:180px;height:50px">Login</button>' \
                        '<script>window.received=[];let down=null;login.addEventListener("pointerdown",e=>{down=performance.now()});' \
                        'login.addEventListener("click",e=>received.push({trusted:e.isTrusted,focused:document.hasFocus(),hold:performance.now()-down}));</script>'
                else:
                    html = '<!doctype html><title>Just a moment...</title><div id="host"></div><script>' \
                        'window.received=[];addEventListener("message",e=>{if(e.origin.startsWith("https://challenges.cloudflare.com:"))received.push(e.data)});' \
                        'let frame=document.createElement("iframe");frame.src="https://challenges.cloudflare.com:' + str(port) + '/widget";' \
                        'frame.style="position:absolute;left:60px;top:40px;width:300px;height:160px;border:0";' \
                        'host.attachShadow({mode:"closed"}).append(frame);</script>'
                body = html.encode()
                handler.send_response(200); handler.send_header("Content-Type", "text/html")
                handler.send_header("Content-Length", str(len(body))); handler.end_headers(); handler.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(folder / "cert.pem", folder / "key.pem")
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.browser = uc.Browser(environment.ChromeConfig(headless=False, browser_executable_path=CHROME,
            user_data_dir=str(folder / "profile"), sandbox=getattr(os,"geteuid",lambda:1)()!=0,
            # Certificate exception and host mappings belong only to this local fixture.
            browser_args=["--disable-dev-shm-usage", "--no-proxy-server", "--ignore-certificate-errors", "--site-per-process",
                "--host-resolver-rules=MAP top.gg 127.0.0.1,MAP challenges.cloudflare.com 127.0.0.1"]))
        try:
            try: await asyncio.wait_for(self.browser.start(), 20)
            except Exception:
                if not await vote.recover_slow_browser_start(self.browser): raise
            self.tab = await self.browser.get("about:blank")
        except BaseException:
            await self.close_fixture(); raise

    async def close_fixture(self):
        process = getattr(self.browser, "_process", None)
        with suppress(Exception): await self.browser.aclose()
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError: process.kill(); await process.wait()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close(); self.thread.join(timeout=2); self.profile.cleanup()

    async def asyncTearDown(self): await self.close_fixture()

    async def test_background_login_is_activated_and_receives_a_finite_native_press(self):
        await vote.navigate_page(self.tab, "https://top.gg:" + str(self.server.server_port) + "/login-fixture")
        other = await self.browser.get("about:blank", new_tab=True)
        await environment.foreground(other)
        self.assertTrue(await vote._click_exact_element(self.tab, "button", ["Login"], "data-auto-login"))
        receipts = await vote.evaluate(self.tab, "window.received")
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]["trusted"])
        self.assertTrue(receipts[0]["focused"])
        self.assertGreaterEqual(receipts[0]["hold"], 80)
        await environment.log_facts(self.tab, vote.evaluate, "startup")

    async def test_site_isolated_closed_shadow_provider_receives_native_checkbox_input(self):
        await vote.navigate_page(self.tab, "https://top.gg:" + str(self.server.server_port) + "/challenge-fixture")
        for _ in range(40):
            infos = await self.tab.send(uc.cdp.target.get_targets())
            if any(info.type_ == "iframe" and info.url.startswith("https://challenges.cloudflare.com:") for info in infos): break
            await asyncio.sleep(0.1)
        else: self.fail("Local cross-site widget did not become a separate iframe target")
        self.assertTrue((await cloudflare_click.widget_state(self.tab))["present"])
        self.assertEqual(await cloudflare_click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False)), "sent")
        for _ in range(20):
            receipts = await vote.evaluate(self.tab, "window.received")
            if receipts: break
            await asyncio.sleep(0.1)
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]["trusted"])
        self.assertGreaterEqual(receipts[0]["hold"], 80)
