"""Checkbox image matching, false-target rejection and real input receipts."""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nodriver as uc

import cloudflare_click as click
import vote


# Probe in a child so a broken native OpenCV installation cannot crash unittest.
CV_AVAILABLE = subprocess.run([
    sys.executable, "-c",
    "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); import cv2",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")


@unittest.skipUnless(CV_AVAILABLE, "OpenCV native runtime unavailable")
class CheckboxImageTests(unittest.TestCase):
    def test_match_maps_device_pixels_to_css_coordinates(self):
        import cv2
        import numpy as np

        template = click._template_gray()
        th, tw = template.shape[:2]
        image = np.full((240, 400), 230, dtype=np.uint8)
        image[60:60 + th, 80:80 + tw] = template
        for scale in (1, 2):
            with self.subTest(scale=scale):
                scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
                _, encoded = cv2.imencode(".png", scaled)
                target = click.match_checkbox(encoded.tobytes(), 400, 240)
                self.assertTrue(target["ready"])
                self.assertGreaterEqual(target["score"], click.MATCH_THRESHOLD)
                self.assertAlmostEqual(target["x"], 80 + tw / 2)
                self.assertAlmostEqual(target["y"], 60 + th / 2)

    def test_absent_checkbox_has_no_click_coordinates(self):
        import cv2
        import numpy as np

        image = np.full((240, 400), 255, dtype=np.uint8)
        _, encoded = cv2.imencode(".png", image)
        target = click.match_checkbox(encoded.tobytes(), 400, 240)
        self.assertFalse(target["ready"])
        self.assertNotIn("x", target)
        self.assertNotIn("y", target)

    def test_distorted_viewport_is_rejected(self):
        import cv2
        import numpy as np

        _, encoded = cv2.imencode(".png", np.zeros((240, 400), dtype=np.uint8))
        self.assertEqual(click.match_checkbox(encoded.tobytes(), 200, 240)["reason"], "viewport_changed")


class CheckboxTargetTests(unittest.IsolatedAsyncioTestCase):
    def target(self, backend=11):
        return {"ready": True, "backend": uc.cdp.dom.BackendNodeId(backend),
                "target": "checkbox", "score": 0.99, "x": 50, "y": 40}

    async def test_waits_past_weak_match_and_sends_only_one_click(self):
        tab = AsyncMock()
        target = self.target()
        with (
            patch("builtins.print"),
            patch.object(click, "POLL_SEC", 0),
            patch.object(click, "TARGET_STABLE_SEC", 0),
            patch.object(click, "checkbox_target", new=AsyncMock(side_effect=[
                {"ready": False, "reason": "weak_match"}, target, target,
            ])),
            patch.object(click, "_click_target", new_callable=AsyncMock) as native,
        ):
            self.assertEqual(await click.click_cloudflare_checkbox(tab, AsyncMock(), AsyncMock(return_value=False)), "sent")
        native.assert_awaited_once_with(tab, target)

    async def test_cover_after_hover_does_not_receive_a_press(self):
        tab = AsyncMock()
        cleared = AsyncMock(side_effect=[False, False, True])
        with (
            patch("builtins.print"),
            patch.object(click, "POLL_SEC", 0),
            patch.object(click, "TARGET_STABLE_SEC", 0),
            patch.object(click, "checkbox_target", new=AsyncMock(side_effect=[
                self.target(), {"ready": False, "reason": "unrelated_target"},
            ])),
            patch.object(click, "_click_target", new_callable=AsyncMock) as native,
        ):
            self.assertEqual(await click.click_cloudflare_checkbox(tab, AsyncMock(), cleared), "cleared")
        native.assert_not_awaited()

    async def test_automatic_clearance_needs_no_mouse_input(self):
        tab = AsyncMock()
        with patch.object(click, "checkbox_target", new_callable=AsyncMock) as target:
            self.assertEqual(await click.click_cloudflare_checkbox(tab, AsyncMock(), AsyncMock(return_value=True)), "cleared")
        target.assert_not_awaited()
        tab.send.assert_not_awaited()

    async def test_unrelated_iframe_is_not_a_checkbox_target(self):
        backend = uc.cdp.dom.BackendNodeId(11)
        frame = uc.cdp.page.FrameId("child")
        tab = AsyncMock()
        tab.send.side_effect = [
            (backend, frame, None), SimpleNamespace(node_name="INPUT", attributes=["type", "checkbox"]),
            SimpleNamespace(frame=SimpleNamespace(id_=uc.cdp.page.FrameId("root"))),
            (uc.cdp.dom.BackendNodeId(12), None),
            SimpleNamespace(node_name="IFRAME", attributes=["src", "https://unrelated.example/"]),
        ]
        self.assertEqual(await click._hit_target(tab, 50, 40), {"ready": False, "reason": "unrelated_target"})

    async def test_opaque_child_hit_can_be_validated_by_its_frame_owner(self):
        owner = uc.cdp.dom.BackendNodeId(12)
        tab = AsyncMock()
        tab.send.side_effect = [
            (uc.cdp.dom.BackendNodeId(11), uc.cdp.page.FrameId("child"), None),
            RuntimeError("Node is in another process"), (owner, None),
            SimpleNamespace(node_name="IFRAME", attributes=["src", "https://challenges.cloudflare.com/widget"]),
        ]
        self.assertEqual(await click._hit_target(tab, 50, 40), {
            "ready": True, "backend": owner, "target": "cloudflare_frame",
        })

    async def test_mouse_release_is_attempted_after_press_timeout(self):
        methods = []

        async def send(command):
            request = next(command)
            methods.append(request)
            if request["params"]["type"] == "mousePressed":
                raise TimeoutError("private-browser-data")

        tab = SimpleNamespace(send=send)
        with self.assertRaises(TimeoutError):
            await click._click_target(tab, {**self.target(), "target": "cloudflare_frame"})
        self.assertEqual([m["params"]["type"] for m in methods], ["mousePressed", "mouseReleased"])
        self.assertEqual(methods[-1]["params"]["buttons"], 0)

    async def test_opaque_frame_does_not_claim_target_received_events(self):
        with patch("builtins.print") as output:
            await click._click_target(AsyncMock(), {**self.target(), "target": "cloudflare_frame"})
        payload = json.loads(output.call_args.args[0].split(": ", 1)[1])
        self.assertTrue(payload["input_sent"])
        self.assertEqual(payload["trusted_events"], {"pressed": None, "released": None, "clicked": None})

    def test_frame_host_matching_rejects_lookalike_domains(self):
        for url in ("https://challenges.cloudflare.com.attacker.example/", "http://challenges.cloudflare.com/"):
            with self.subTest(url=url):
                self.assertFalse(click._cloudflare_frame(SimpleNamespace(node_name="IFRAME", attributes=["src", url])))


@unittest.skipUnless(CHROME and CV_AVAILABLE, "Chrome and OpenCV needed for checkbox fixtures")
class CheckboxBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="checkbox-fixture-")
        self.browser = uc.Browser(uc.Config(
            headless=True, browser_executable_path=CHROME, user_data_dir=self.profile.name,
            sandbox=getattr(os, "geteuid", lambda: 1)() != 0,
            browser_args=["--disable-dev-shm-usage", "--no-proxy-server"],
        ))
        try:
            try:
                await asyncio.wait_for(self.browser.start(), timeout=20)
            except Exception:
                if not await vote.recover_slow_browser_start(self.browser):
                    raise
            self.tab = await self.browser.get("about:blank")
            image = base64.b64encode(click.get_cf_template()).decode("ascii")
            self.widget = f"""<style>
            body {{ margin:0; }}
            img {{ position:absolute; left:100px; top:100px; }}
            input {{ position:absolute; left:143px; top:125px; margin:0; width:24px; height:24px;
                appearance:none; background:transparent; border:0; }}
            </style><img src="data:image/png;base64,{image}"><input type="checkbox" id="checkbox">
            <script>window.received=[]; checkbox.addEventListener('click', e => received.push(e.isTrusted));</script>"""
            await self.load("<!doctype html><title>Just a moment...</title>" + self.widget)
        except BaseException:
            await self.close_browser()
            raise

    async def load(self, html):
        await vote.evaluate(self.tab, "document.open(); document.write(" + json.dumps(html) + "); document.close();")
        await vote.evaluate(self.tab, "Promise.all([...document.images].map(image => image.decode()))")

    async def close_browser(self):
        process = getattr(self.browser, "_process", None)
        with suppress(Exception):
            await self.browser.aclose()
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_browser()

    async def test_native_checkbox_input_with_device_scale_two(self):
        await self.tab.send(uc.cdp.emulation.set_device_metrics_override(
            width=800, height=600, device_scale_factor=2, mobile=False,
        ))
        with patch("builtins.print") as output:
            result = await click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False))
        self.assertEqual(result, "sent")
        self.assertEqual(await vote.evaluate(self.tab, "window.received"), [True])
        payloads = [str(c.args[0]) for c in output.call_args_list if "Cloudflare mouse input:" in str(c.args[0])]
        payload = json.loads(payloads[0].split(": ", 1)[1])
        self.assertEqual(payload["trusted_events"], {"pressed": True, "released": True, "clicked": True})
        self.assertTrue(await vote.evaluate(self.tab, "checkbox.__autoCfReceipt === undefined"))

    async def test_closed_shadow_iframe_receives_the_checkbox_click(self):
        # srcdoc keeps this fixture entirely local. The src attribute models a
        # Cloudflare frame owner, including a closed shadow root and frame offset.
        await self.load("<!doctype html><title>Just a moment...</title><div id='host'></div>")
        await vote.evaluate(self.tab, """new Promise(resolve => {
            const frame = document.createElement('iframe');
            frame.src = 'https://challenges.cloudflare.com/local-fixture';
            frame.srcdoc = __WIDGET__;
            frame.style.cssText = 'position:absolute;left:60px;top:40px;width:400px;height:300px;border:0';
            frame.onload = resolve;
            window.fixtureFrame = frame;
            const shadow = host.attachShadow({mode:'closed'}); shadow.append(frame);
        })""".replace("__WIDGET__", json.dumps(self.widget)))
        await vote.evaluate(self.tab, "fixtureFrame.contentDocument.images[0].decode()")
        self.assertEqual(await click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False)), "sent")
        self.assertEqual(await vote.evaluate(self.tab, "fixtureFrame.contentWindow.received"), [True])

    async def test_overlay_created_on_hover_blocks_the_click(self):
        await vote.evaluate(self.tab, """checkbox.addEventListener('pointerenter', () => {
            const cover = document.createElement('div');
            cover.style.cssText = 'position:fixed;inset:0;background:white;z-index:10';
            document.body.append(cover);
        }, {once:true})""")
        with patch.object(click, "TARGET_WAIT_SEC", 2):
            self.assertEqual(await click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False)), "unavailable")
        self.assertEqual(await vote.evaluate(self.tab, "window.received"), [])


if __name__ == "__main__":
    unittest.main()
