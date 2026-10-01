"""Application controls receive real input; fixtures never access live sites."""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import nodriver as uc

import ui_click
import vote
from test_vote_pointer import CHROME, FIXTURE


class ApplicationClickUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_reselection_is_cancelled_before_any_input(self):
        tab = MagicMock(); tab.send = AsyncMock()
        cancelled = asyncio.Event()
        async def reacquire():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch("builtins.print"):
            result = await ui_click.click_control(tab, AsyncMock(), "#login", timeout=0.01, reacquire=reacquire)
        self.assertFalse(result["input_sent"])
        self.assertTrue(cancelled.is_set())
        tab.send.assert_not_awaited()

    async def test_transient_reselection_failure_recovers_before_input(self):
        tab = MagicMock(); tab.send = AsyncMock()
        reacquire = AsyncMock(side_effect=[RuntimeError("document changed"), True, True])
        evaluate = AsyncMock(return_value={"ready": True, "x": 10, "y": 20, "clicked": True})
        with patch("builtins.print"):
            result = await ui_click.click_control(tab, evaluate, "#login", timeout=2, reacquire=reacquire)
        self.assertTrue(result["input_sent"])
        self.assertEqual(reacquire.await_count, 3)
        self.assertEqual([p["params"]["type"] for c in tab.send.await_args_list
                         if (p := next(c.args[0]))["method"] == "Input.dispatchMouseEvent"],
                         ["mouseMoved", "mousePressed", "mouseReleased"])

    async def test_missing_reselection_cannot_press_an_obsolete_marked_control(self):
        tab = MagicMock(); tab.send = AsyncMock()
        evaluate = AsyncMock(return_value={"ready": True, "x": 10, "y": 20})
        with patch("builtins.print"):
            result = await ui_click.click_control(tab, evaluate, "#login", timeout=0.01,
                                                 reacquire=AsyncMock(return_value=False))
        self.assertFalse(result["input_sent"])
        tab.send.assert_not_awaited()

    async def test_navigation_can_destroy_receipt_without_fabricating_a_click(self):
        tab = MagicMock(); tab.send = AsyncMock()
        async def evaluate(_tab, script):
            if "window.__autoUiPointer?.receipt" in script: raise RuntimeError("new document")
            return {"ready": True, "x": 10, "y": 20}
        with (patch("ui_click.asyncio.sleep", new_callable=AsyncMock), patch("builtins.print")):
            result = await ui_click.click_control(tab, evaluate, "#login", kind="login")
        self.assertEqual(result, {"input_sent": True, "clicked": None})
        self.assertEqual([p["params"]["type"] for c in tab.send.await_args_list
                         if (p := next(c.args[0]))["method"] == "Input.dispatchMouseEvent"],
                         ["mouseMoved", "mousePressed", "mouseReleased"])

    async def test_press_timeout_still_releases_and_reports_unknown_receipt(self):
        tab = MagicMock(); commands = []
        async def send(command):
            payload = next(command); commands.append(payload)
            if payload.get("params", {}).get("type") == "mousePressed": raise TimeoutError
        tab.send = AsyncMock(side_effect=send)
        evaluate = AsyncMock(return_value={"ready": True, "x": 10, "y": 20})
        with (patch("ui_click.asyncio.sleep", new_callable=AsyncMock), patch("builtins.print")):
            result = await ui_click.click_control(tab, evaluate, "#login")
        self.assertTrue(result["input_sent"])
        self.assertIsNone(result["clicked"])
        self.assertEqual(commands[-1]["params"]["type"], "mouseReleased")
        self.assertEqual(commands[-1]["params"]["buttons"], 0)


@unittest.skipUnless(CHROME, "Chrome is needed for application input fixtures")
class ApplicationClickBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="application-pointer-test-")
        self.browser = uc.Browser(uc.Config(
            headless=True, browser_executable_path=CHROME, user_data_dir=self.profile.name,
            sandbox=getattr(os, "geteuid", lambda: 1)() != 0,
            browser_args=["--disable-dev-shm-usage", "--no-proxy-server"],
        ))
        try:
            try: await asyncio.wait_for(self.browser.start(), timeout=20)
            except Exception:
                if not await vote.recover_slow_browser_start(self.browser): raise
            self.tab = await self.browser.get("about:blank")
            await vote.evaluate(self.tab,
                "document.open(); document.write(" + json.dumps(FIXTURE) + "); document.close();")
        except BaseException:
            await self.close_browser(); raise

    async def close_browser(self):
        process = getattr(self.browser, "_process", None)
        with suppress(Exception): await self.browser.aclose()
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_browser()

    async def test_login_and_authorize_receive_trusted_input_instead_of_js_clicks(self):
        for label, marker in (("Login", "data-auto-login"), ("Authorize", "data-auto-oauth")):
            with self.subTest(label=label):
                await vote.evaluate(self.tab, "vote.textContent=" + json.dumps(label) + "; events=[]; votes=0; lastPress=null;")
                self.assertTrue(await vote._mark_exact_element(self.tab, "button", [label], marker))
                self.assertTrue(await vote._click_marked(self.tab, marker))
                self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)
                events = await vote.evaluate(self.tab, "window.events")
                self.assertEqual([e["type"] for e in events], ["pointerdown", "pointerup", "click"])
                self.assertTrue(all(e["trusted"] for e in events))

    async def test_hidden_disabled_duplicates_cannot_capture_login_selection(self):
        await vote.evaluate(self.tab, """(() => {
            vote.textContent='Login';
            const disabled=document.createElement('button'); disabled.textContent='Login'; disabled.disabled=true;
            const hidden=document.createElement('button'); hidden.textContent='Login'; hidden.style.display='none';
            document.body.prepend(disabled,hidden);
        })()""")
        self.assertTrue(await vote._mark_exact_element(self.tab, "button", ["Login"], "data-auto-login"))
        self.assertTrue(await vote._click_marked(self.tab, "data-auto-login"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)

    async def test_covered_and_inherited_hidden_login_copies_do_not_capture_selection(self):
        await vote.evaluate(self.tab, """(() => {
            vote.textContent='Login';
            const hidden=document.createElement('div'); hidden.style.opacity='0';
            hidden.innerHTML='<button>Login</button>';
            const covered=document.createElement('div');
            covered.style.cssText='position:fixed;left:0;top:0;width:80px;height:60px;z-index:2';
            covered.innerHTML='<button style="margin:0;width:80px;height:60px">Login</button>'+
                '<div style="position:absolute;inset:0;background:white;z-index:1"></div>';
            document.body.prepend(hidden,covered);
        })()""")
        self.assertTrue(await vote._click_exact_element(self.tab, "button", ["Login"], "data-auto-login"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)
        self.assertTrue(all(e["trusted"] for e in await vote.evaluate(self.tab, "window.events")))

    async def test_login_and_authorize_are_reselected_after_hover_remount(self):
        for label, marker in (("Login", "data-auto-login"), ("Authorize", "data-auto-oauth")):
            with self.subTest(label=label):
                await vote.evaluate(self.tab, """(() => {
                    const button=document.getElementById('vote'); button.textContent=__LABEL__;
                    events=[];votes=0;lastPress=null;
                    button.addEventListener('pointerenter',()=>{
                        const replacement=button.cloneNode(true);
                        replacement.removeAttribute(__MARKER__);
                        button.replaceWith(replacement); window.remounted=true;
                    },{once:true});
                    window.remounted=false;
                })()""".replace("__LABEL__", json.dumps(label)).replace("__MARKER__", json.dumps(marker)))
                # Move away so the second label also receives a new hover.
                await self.tab.send(uc.cdp.input_.dispatch_mouse_event("mouseMoved", x=0, y=0, buttons=0))
                self.assertTrue(await vote._click_exact_element(self.tab, "button", [label], marker))
                self.assertTrue(await vote.evaluate(self.tab, "window.remounted"))
                self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)

    async def test_login_can_appear_after_selection_starts(self):
        await vote.evaluate(self.tab, """(() => {
            const button=document.getElementById('vote');button.textContent='Login';button.remove();
            setTimeout(()=>document.body.append(button),300);
        })()""")
        self.assertTrue(await vote._click_exact_element(self.tab, "button", ["Login"], "data-auto-login"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)

    async def test_consent_requires_native_input_and_observed_overlay_disappearance(self):
        await vote.evaluate(self.tab, """(() => {
            const modal=document.createElement('section'); modal.id='consent';
            modal.innerHTML='<p>We value your privacy</p><button id="accept-btn">Accept</button>';
            document.body.append(modal);
            const accept=document.getElementById('accept-btn'); let down=false;
            accept.addEventListener('pointerdown',event=>{down=event.isTrusted});
            accept.addEventListener('click',event=>{if(down&&event.isTrusted)modal.remove()});
            accept.click();
        })()""")
        self.assertTrue(await vote.evaluate(self.tab, "Boolean(document.getElementById('consent'))"))
        self.assertTrue(await vote.dismiss_privacy_overlay(self.tab))
        self.assertFalse(await vote.evaluate(self.tab, "Boolean(document.getElementById('consent'))"))

    async def test_covered_preferred_consent_does_not_hide_a_clickable_accept(self):
        await vote.evaluate(self.tab, """(() => {
            const modal=document.createElement('section');modal.id='consent';
            modal.innerHTML='<p>We value your privacy</p>'+
                '<div style="position:relative"><button id="accept-btn">Accept</button>'+
                '<div style="position:absolute;inset:0;background:white;z-index:1"></div></div>'+
                '<button id="working-accept">Accept</button>';
            document.body.append(modal);let down=false;
            modal.addEventListener('pointerdown',e=>{down=e.isTrusted&&e.target.id==='working-accept'});
            modal.addEventListener('click',e=>{if(down&&e.isTrusted&&e.target.id==='working-accept')modal.remove()});
        })()""")
        self.assertTrue(await vote.dismiss_privacy_overlay(self.tab))
        self.assertFalse(await vote.evaluate(self.tab, "Boolean(document.getElementById('consent'))"))

    async def test_consent_is_reselected_after_hover_remount(self):
        await vote.evaluate(self.tab, """(() => {
            const modal=document.createElement('section');modal.id='consent';
            modal.innerHTML='<p>We value your privacy</p><button id="accept-btn">Accept</button>';
            document.body.append(modal);const button=modal.querySelector('button');
            button.addEventListener('pointerenter',()=>{
                const replacement=button.cloneNode(true);replacement.removeAttribute('data-auto-consent');
                button.replaceWith(replacement);window.consentRemounted=true;
            },{once:true});
            let down=false;modal.addEventListener('pointerdown',e=>{down=e.isTrusted});
            modal.addEventListener('click',e=>{if(down&&e.isTrusted)modal.remove()});
        })()""")
        self.assertTrue(await vote.dismiss_privacy_overlay(self.tab))
        self.assertTrue(await vote.evaluate(self.tab, "window.consentRemounted"))
        self.assertFalse(await vote.evaluate(self.tab, "Boolean(document.getElementById('consent'))"))

    async def test_hover_overlay_stops_generic_mouse_press(self):
        await vote.evaluate(self.tab, """(() => {
            vote.textContent='Login';
            vote.addEventListener('pointerenter',()=>{
                const cover=document.createElement('div');cover.id='cover';document.body.append(cover);
            },{once:true});
        })()""")
        await vote._mark_exact_element(self.tab, "button", ["Login"], "data-auto-login")
        result = await ui_click.click_control(self.tab, vote.evaluate, '[data-auto-login="1"]', timeout=1.5)
        self.assertFalse(result["input_sent"])
        self.assertEqual(await vote.evaluate(self.tab, "window.events"), [])

    async def test_residual_managed_title_does_not_block_an_actionable_vote(self):
        await vote.evaluate(self.tab, "document.title='Just a moment...'")
        self.assertFalse(await vote.is_turnstile_present(self.tab))
        self.assertTrue(await vote._click_marked(self.tab, "data-auto-vote"))
        self.assertEqual(await vote.evaluate(self.tab, "window.votes"), 1)

    async def test_real_human_verification_text_still_blocks_vote_input(self):
        await vote.evaluate(self.tab, "ad.textContent='Verify you are human'")
        with patch("vote.TIMEOUT_VOTE_SEC", 1):
            with self.assertRaisesRegex(vote.VoteClickNotReady, "protection_active"):
                await vote._click_marked(self.tab, "data-auto-vote")
        self.assertEqual(await vote.evaluate(self.tab, "window.events"), [])
