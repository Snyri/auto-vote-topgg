"""Real Chrome fixtures for the recorded AniGame UI; every request is local."""

import asyncio
import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import suppress
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

import nodriver as uc

import anigame_page as page
import anigame_vote as client
import vote as core


CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
FIXTURE_TOKEN = "local-only-discord-fixture-token"
AUTHORIZE = "https://discord.com/oauth2/authorize?" + urlencode({
    "client_id":page.BOT_ID, "redirect_uri":"https://anigame.gg/api/auth/discord/callback",
    "response_type":"code", "scope":"identify email guilds"})


@unittest.skipUnless(CHROME, "Chrome needed for AniGame native-input fixtures")
class AniGameBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="anigame-fixture-")
        self.browser = uc.Browser(uc.Config(
            headless=False, browser_executable_path=CHROME, user_data_dir=self.profile.name,
            sandbox=getattr(os, "geteuid", lambda: 1)() != 0,
            browser_args=["--window-size=1280,720", "--disable-dev-shm-usage", "--no-proxy-server"]))
        self.duration = .2
        self.registered = False
        self.optimistic = False
        self.spa = False
        self.cooldown_initial = False
        self.registration_requests = 0
        self.purchases = 0
        self.vote_documents = 0
        self.receipt_documents = 0
        self.deadline = int(time.time()) + 1200
        self.guard = None
        self.errors = []
        self.authenticated = True
        self.cookie_login = False
        self.discord_logins = 0
        self.oauth_requests = 0
        self.oauth_grants = 0
        self.new_tab_urls = []
        try:
            try:
                await asyncio.wait_for(self.browser.start(), 20)
            except Exception:
                if not await core.recover_slow_browser_start(self.browser):
                    raise
            self.tab = await self.browser.get("about:blank")
            self.tab.add_handler(uc.cdp.fetch.RequestPaused, self.fulfill)
            await self.tab.send(uc.cdp.fetch.enable(
                patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))
            original_get = self.browser.get
            async def get_with_local_replies(url, *args, **kwargs):
                if kwargs.get("new_tab"):
                    self.new_tab_urls.append(url)
                    # No new target may start an un-intercepted live request.
                    self.assertEqual(url, "about:blank")
                tab = await original_get(url, *args, **kwargs)
                if kwargs.get("new_tab"):
                    async def fulfill_new(event):
                        await self.fulfill_on(tab, event)
                    tab.add_handler(uc.cdp.fetch.RequestPaused, fulfill_new)
                    await tab.send(uc.cdp.fetch.enable(
                        patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))
                return tab
            self.browser.get = get_with_local_replies
        except BaseException:
            await self.close_fixture()
            raise

    async def close_fixture(self):
        process = getattr(self.browser, "_process", None)
        with suppress(Exception):
            await asyncio.wait_for(self.browser.aclose(), 5)
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_fixture()
        self.assertEqual(self.errors, [])

    def receipt(self):
        return """<main><div>Vote complete</div><h1>Vote confirmed</h1>
        <p>Thank you for supporting AniGame! Your rewards have been added to your game account.</p>
        <h2>Your vote rewards</h2><p>Gold +10,000</p><a href="/vote">Back to voting</a></main>"""

    def html(self):
        disabled = "disabled" if self.cooldown_initial else ""
        cooldown = "<p>Your next free vote is 4h 21m.</p>" if self.cooldown_initial else ""
        return r"""<!doctype html><title>AniGame voting fixture</title>
        <style>button{min-width:180px;min-height:48px;margin:15px} main{padding:20px}
        #spacer{height:850px} #area{border:1px solid #aaa;padding:15px}</style>
        <main><h1>AniGame voting</h1><div id="spacer">Advertisements</div>
        <button onclick="fetch('/_fixture/purchase',{method:'POST'})">Monthly vote options</button>
        <button>Vote now</button><fieldset disabled><button>Vote for AniGame</button></fieldset>
        <section id="area"><h2>Register your vote</h2><p>Stay on this page for 15 seconds to register your vote.</p>
        <button id="free" __DISABLED__><svg aria-hidden="true" width="12" height="12"><path d="M0 0L4 4"/></svg> Vote for AniGame</button></section>
        __COOLDOWN__<p id="progress"></p><p>Next vote: Ready</p></main>
        <script>
        window.nativeClicks=0;
        free.addEventListener('click', async event => {
            if (!event.isTrusted) return;
            window.nativeClicks++; sessionStorage.nativeClicks=String(window.nativeClicks);
            free.disabled=true; free.textContent='Registering vote...';
            let remaining=__DURATION__, last=performance.now();
            const timer=setInterval(async()=>{
                const now=performance.now();
                if (document.visibilityState === 'visible') remaining-= (now-last)/1000;
                last=now;
                progress.textContent='Please wait or explore this page while we register your vote: '+Math.max(0,Math.ceil(remaining))+'s';
                if (remaining>0) return;
                clearInterval(timer);
                const response=await fetch('/_fixture/register',{method:'POST',body:'fixture-only'});
                if (!response.ok) {progress.innerHTML='<div role="alert">Registration failed</div>'; return;}
                if (__SPA__) {
                    history.pushState({},'', '/vote/confirmation');
                    document.body.innerHTML=__RECEIPT__;
                } else location.assign('/vote/confirmation');
            }, 50);
        });
        </script>""".replace("__DISABLED__", disabled).replace("__COOLDOWN__", cooldown).replace(
            "__DURATION__", str(self.duration)).replace("__SPA__", json.dumps(self.spa)).replace(
            "__RECEIPT__", json.dumps(self.receipt()))

    async def fulfill(self, event):
        await self.fulfill_on(self.tab, event)

    async def fulfill_on(self, tab, event):
        try:
            parsed = urlsplit(event.request.url)
            path = parsed.path
            if self.guard is not None and event.request.method == "POST" and path.startswith("/_fixture/"):
                await self.guard.paused(event)
                return
            status, headers, body = 404, {"content-type":"text/plain"}, "Local fixture only"
            if self.cookie_login and not self.authenticated and parsed.hostname in page.HOSTS:
                self.authenticated = any(cookie.name == "anigame_fixture_session" and cookie.value == "local-session-only"
                    and cookie.domain.lstrip(".") in page.HOSTS for cookie in await self.browser.cookies.get_all())
            if parsed.hostname in {"discord.com", "www.discord.com"} and path == "/login":
                self.discord_logins += 1
                # Reproduce the broken JS branch while origin storage remains
                # writable through the real browser protocol.
                status, headers, body = 200, {"content-type":"text/html"}, '''<!doctype html><title>Discord fixture</title>
                    <p>Local login fixture</p><script>
                    Object.defineProperty(window, 'localStorage', {value:undefined, configurable:true});
                    </script>'''
            elif event.request.url == AUTHORIZE:
                self.oauth_requests += 1
                status, headers = 200, {"content-type":"text/html"}
                body = '''<!doctype html><style>button{margin:100px;width:180px;height:50px}</style><body><script>
                    const stored = localStorage.getItem('token') === __TOKEN__ && localStorage.getItem('tokens') === __TOKENS__;
                    document.body.innerHTML = stored ? '<button id="authorize">Authorize</button>' : '<p>Discord session missing</p>';
                    if(stored) authorize.addEventListener('click',e=>{if(e.isTrusted)location.href=
                    'https://anigame.gg/api/auth/discord/callback?code=local-fixture-only'});
                    </script>'''.replace("__TOKEN__", json.dumps(json.dumps(FIXTURE_TOKEN))).replace(
                        "__TOKENS__", json.dumps(json.dumps({"default":FIXTURE_TOKEN})))
            elif parsed.hostname in page.HOSTS and path == "/api/auth/discord/callback":
                self.oauth_grants += 1
                self.authenticated = True
                status, headers, body = 302, {"location":page.VOTE_URL}, ""
            elif path == "/vote":
                self.vote_documents += 1
                signed_out = '''<!doctype html><main><h1>Sign in to AniGame</h1>
                    <button id="login">Sign in with Discord</button></main>
                    <script>login.addEventListener('click',e=>{if(e.isTrusted)location.href=__AUTHORIZE__})</script>'''.replace(
                        "__AUTHORIZE__", json.dumps(AUTHORIZE))
                status, headers, body = 200, {"content-type":"text/html"}, self.html() if self.authenticated else signed_out
            elif path == "/vote/confirmation":
                self.receipt_documents += 1
                status, headers, body = 200, {"content-type":"text/html"}, self.receipt()
            elif path == "/api/auth/me":
                status, headers, body = 200, {"content-type":"application/json"}, json.dumps(
                    {"user":{"id":"196428430126870001"} if self.authenticated else None})
            elif path == "/api/vote/availability":
                on_cooldown = self.registered or self.cooldown_initial
                status, headers, body = 200, {"content-type":"application/json"}, json.dumps(
                    {"vote":{"available":not on_cooldown,
                             "nextEligibleAt":datetime.fromtimestamp(self.deadline,timezone.utc).isoformat() if on_cooldown else None}})
            elif path == "/_fixture/register":
                self.registration_requests += 1
                if not self.optimistic:
                    self.registered = True
                status, headers, body = 200, {"content-type":"application/json"}, '{"ok":true}'
            elif path == "/_fixture/purchase":
                self.purchases += 1
                status, headers, body = 200, {"content-type":"application/json"}, '{}'
            await tab.send(uc.cdp.fetch.fulfill_request(
                event.request_id, response_code=status,
                response_headers=[uc.cdp.fetch.HeaderEntry(name=k,value=v) for k,v in headers.items()],
                body=base64.b64encode(body.encode()).decode()))
        except Exception as exc:
            self.errors.append(type(exc).__name__)

    async def open_page(self):
        await core.navigate_page(self.tab, page.VOTE_URL)
        for _ in range(50):
            state = await client.snapshot(self.tab)
            if state.get("ready") and state.get("vote_present"):
                return state
            await asyncio.sleep(.05)
        self.fail("AniGame fixture did not expose its actual free-vote control")

    async def test_exact_anigame_label_is_enabled_without_legacy_vote_label(self):
        state = await self.open_page()
        self.assertTrue(state["vote_enabled"])
        self.assertTrue(await core.evaluate(self.tab, page.MARK_VOTE_JS))
        self.assertEqual(await core.evaluate(self.tab,
            "document.querySelector('[data-anigame-vote]').id"), "free")

    async def test_anigame_cookie_import_authenticates_without_discord_or_vote(self):
        self.authenticated, self.cookie_login = False, True
        await core.navigate_page(self.tab, page.VOTE_URL)
        cookies = client.load_cookies(json.dumps([{
            "name":"anigame_fixture_session", "value":"local-session-only", "domain":".anigame.gg",
            "path":"/", "httpOnly":True, "secure":True}]), 1)[0]
        self.assertIsNone(self.browser.connection)
        ok, reason = await client.authenticate(self.browser, self.tab, FIXTURE_TOKEN, cookies)
        self.assertTrue(ok, reason)
        self.assertTrue(self.authenticated)
        self.assertEqual(self.new_tab_urls, [])
        self.assertEqual(self.oauth_grants, 0)
        self.assertEqual(self.registration_requests, 0)

    async def test_full_signed_out_oauth_works_without_the_page_storage_accessor(self):
        self.authenticated = False
        await core.navigate_page(self.tab, page.VOTE_URL)
        with patch.object(client, "POLL_INTERVAL", .05):
            ok, reason = await client.authenticate(self.browser, self.tab, FIXTURE_TOKEN, [])
        self.assertTrue(ok, reason)
        self.assertEqual(self.new_tab_urls, ["about:blank"])
        self.assertEqual(self.discord_logins, 2)
        self.assertEqual(self.oauth_requests, 1)
        self.assertEqual(self.oauth_grants, 1)
        self.assertEqual(self.registration_requests, 0)
        self.assertFalse((await client.snapshot(self.tab))["login"])

    async def test_full_fifteen_seconds_native_input_and_automatic_receipt(self):
        self.duration = 15
        await self.open_page()
        ok, _ = await client.authenticate(self.browser, self.tab, "fixture-token", [])
        self.assertTrue(ok)
        started = time.monotonic()
        with patch.object(client, "POLL_INTERVAL", .1):
            outcome = await client.vote_once(self.tab)
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertGreaterEqual(time.monotonic()-started, 15)
        self.assertEqual(self.registration_requests, 1)
        self.assertEqual(self.vote_documents, 1)
        self.assertEqual(self.receipt_documents, 1)
        self.assertEqual(self.purchases, 0)
        self.assertEqual(await core.evaluate(self.tab, "sessionStorage.nativeClicks"), "1")
        self.assertEqual(outcome["retry_at"], self.deadline + 30)

    async def test_mobile_ratio_and_spa_receipt_need_no_reload_or_second_click(self):
        self.spa = True
        await self.tab.send(uc.cdp.emulation.set_device_metrics_override(510,732,1,True))
        await self.open_page()
        with patch.object(client,"POLL_INTERVAL",.05):
            outcome = await client.vote_once(self.tab)
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertEqual(self.registration_requests, 1)
        self.assertEqual(self.vote_documents, 1)
        self.assertEqual(self.receipt_documents, 0)
        self.assertEqual(self.purchases, 0)

    async def test_cooldown_preserves_server_timer_and_never_clicks(self):
        self.cooldown_initial = True
        await self.open_page()
        with patch.object(client,"POLL_INTERVAL",.05):
            outcome = await client.vote_once(self.tab)
        self.assertEqual(outcome["status"], "cooldown")
        self.assertEqual(outcome["retry_at"], self.deadline + 30)
        self.assertEqual(self.registration_requests, 0)
        self.assertEqual(await core.evaluate(self.tab, "window.nativeClicks"), 0)

    async def test_optimistic_200_and_receipt_do_not_override_available_server_state(self):
        self.optimistic, self.spa = True, True
        await self.open_page()
        with patch.object(client,"POLL_INTERVAL",.05), patch.object(client,"RESULT_TIMEOUT",2):
            outcome = await client.vote_once(self.tab)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(self.registration_requests, 1)
        self.assertEqual(self.vote_documents, 1)

    async def test_inspector_leaves_free_vote_untouched(self):
        await self.open_page()
        outcome = await client.vote_once(self.tab, inspect_only=True)
        self.assertEqual(outcome["status"], "inspected")
        self.assertEqual(self.registration_requests, 0)
        self.assertEqual(await core.evaluate(self.tab, "window.nativeClicks"), 0)

    async def test_inspection_network_guard_rejects_unexpected_registration_writes(self):
        await self.open_page()
        self.guard = client.ReadOnlyGuard(self.tab)
        observed = await core.evaluate(self.tab,
            "fetch('/_fixture/register',{method:'POST'}).then(()=>false).catch(()=>true)")
        self.assertTrue(observed)
        self.assertEqual(self.guard.denied, 1)
        self.assertEqual(self.registration_requests, 0)


if __name__ == "__main__":
    unittest.main()
