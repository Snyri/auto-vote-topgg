"""Exercise the Vote mouse input against local HTML, never a live voting site."""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import patch

import nodriver as uc

import vote


CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
FIXTURE = """<!doctype html><title>Local Vote fixture</title>
<style>
  body { margin: 0; }
  button { margin: 100px; width: 180px; height: 50px; }
  #cover { position: fixed; inset: 0; z-index: 10; background: white; }
</style>
<div id="ad"></div><button id="vote"><span>Vote</span></button>
<script>
window.events = [];
window.votes = 0;
window.lastPress = null;
for (const type of ['pointerdown', 'pointerup', 'click']) {
  document.addEventListener(type, event => {
    const button = event.target.closest('#vote');
    if (!button) return;
    events.push({type, trusted: event.isTrusted, time: performance.now()});
    if (type === 'pointerdown') lastPress = button;
    // Model a UI needing the normal input sequence, not just a JS click().
    if (type === 'click' && lastPress === button && event.isTrusted) votes++;
  });
}
</script>"""


@unittest.skipUnless(CHROME, "Chrome is needed for local mouse interaction regression tests")
class VotePointerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="vote-pointer-test-")
        self.browser = uc.Browser(uc.Config(
            headless=True,
            browser_executable_path=CHROME,
            user_data_dir=self.profile.name,
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
            await vote.evaluate(self.tab,
                "document.open(); document.write(" + json.dumps(FIXTURE) + "); document.close();")
        except BaseException:
            await self.close_browser()
            raise

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

    async def test_native_input_activates_control_that_ignores_js_click(self):
        element = await self.tab.select("#vote")
        await element.click()  # The previous production primitive.
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 0)
        await vote.evaluate(self.tab, "window.events = []")

        self.assertTrue(await vote._click_marked(self.tab, "data-auto-vote"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)
        events = await vote.evaluate(self.tab, "window.events")
        self.assertEqual([event["type"] for event in events], ["pointerdown", "pointerup", "click"])
        self.assertTrue(all(event["trusted"] for event in events))
        self.assertTrue(await vote.evaluate(self.tab, "window.__autoVotePointer === undefined"))

    async def test_ad_text_can_disappear_before_overlay_and_button_are_ready(self):
        await vote.evaluate(self.tab, """(() => {
            ad.textContent = 'You will be able to vote after this ad';
            vote.disabled = true;
            const cover = document.createElement('div'); cover.id = 'cover';
            document.body.append(cover);
            setTimeout(() => { ad.textContent = ''; vote.disabled = false; }, 200);
            setTimeout(() => {
                const old = document.getElementById('vote');
                old.replaceWith(old.cloneNode(true)); // Framework remount after ad.
                cover.remove();
                window.adFinishedAt = performance.now();
            }, 900);
        })()""")
        self.assertTrue(await vote._click_marked(self.tab, "data-auto-vote"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)
        self.assertTrue(await vote.evaluate(self.tab,
            "events.every(event => event.time >= window.adFinishedAt)"))

    async def test_hover_overlay_prevents_press_and_is_not_called_a_submission(self):
        await vote.evaluate(self.tab, """(() => {
            vote.addEventListener('pointerenter', () => {
                const cover = document.createElement('div'); cover.id = 'cover';
                document.body.append(cover);
            }, {once: true});
        })()""")
        with patch("vote.TIMEOUT_VOTE_SEC", 2):
            with self.assertRaises(vote.VoteClickNotReady):
                await vote._click_marked(self.tab, "data-auto-vote")
        self.assertEqual(await vote.evaluate(self.tab, "window.events"), [])
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 0)
        self.assertTrue(await vote.evaluate(self.tab, "window.__autoVotePointer === undefined"))

    async def test_fieldset_disabled_control_never_receives_mouse_press(self):
        await vote.evaluate(self.tab, """(() => {
            const field = document.createElement('fieldset'); field.disabled = true;
            document.body.append(field); field.append(vote);
        })()""")
        with patch("vote.TIMEOUT_VOTE_SEC", 1):
            with self.assertRaisesRegex(vote.VoteClickNotReady, "disabled"):
                await vote._click_marked(self.tab, "data-auto-vote")
        self.assertEqual(await vote.evaluate(self.tab, "window.events"), [])

    async def test_target_replaced_during_press_is_uncertain_without_second_click(self):
        await vote.evaluate(self.tab, """(() => {
            vote.addEventListener('pointerdown', event => {
                event.currentTarget.replaceWith(event.currentTarget.cloneNode(true));
            }, {once: true});
        })()""")
        self.assertFalse(await vote._click_marked(self.tab, "data-auto-vote"))
        events = await vote.evaluate(self.tab, "window.events")
        self.assertEqual(sum(event["type"] == "pointerdown" for event in events), 1)
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 0)


if __name__ == "__main__":
    unittest.main()
