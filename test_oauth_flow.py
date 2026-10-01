"""Production OAuth navigation and native clicks; every HTTP reply is local."""

import asyncio
import base64
import json
import os
import tempfile
import unittest
from contextlib import suppress
from urllib.parse import urlparse
from unittest.mock import AsyncMock, patch

import nodriver as uc

import request_diagnostics
import vote
from test_vote_pointer import CHROME


PAGE = "https://top.gg/bot/111/vote"
AUTHORIZE = "https://discord.com/oauth2/authorize?client_id=fixture"


@unittest.skipUnless(CHROME,"Chrome needed for local OAuth flow fixtures")
class OAuthBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile=tempfile.TemporaryDirectory(prefix="oauth-flow-fixture-")
        self.browser=uc.Browser(vote.browser_environment.ChromeConfig(
            headless=False,browser_executable_path=CHROME,user_data_dir=self.profile.name,
            sandbox=getattr(os,"geteuid",lambda:1)()!=0,
            browser_args=["--window-size=1280,720","--disable-dev-shm-usage","--no-proxy-server"],
        ))
        self.tracker=None
        self.automatic=False
        self.authenticated=False
        self.login_requests=0
        self.grant_requests=0
        self.fixture_errors=[]
        self.block_session=False
        self.vote_page_requests=0
        try:
            try:
                await asyncio.wait_for(self.browser.start(),20)
            except Exception:
                if not await vote.recover_slow_browser_start(self.browser): raise
            self.tab=await self.browser.get("about:blank")
            self.tracker=request_diagnostics.RequestDiagnostics(self.tab)
            self.assertTrue(await self.tracker.start())
            self.tab.add_handler(uc.cdp.fetch.RequestPaused,self.fulfill)
            await self.tab.send(uc.cdp.fetch.enable(patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))
            original_get=self.browser.get
            async def get_with_local_replies(url, *args, **kwargs):
                tab=await original_get(url,*args,**kwargs)
                if kwargs.get("new_tab"):
                    async def fulfill_in_new_tab(event):
                        await self.fulfill_on(tab,event)
                    tab.add_handler(uc.cdp.fetch.RequestPaused,fulfill_in_new_tab)
                    await tab.send(uc.cdp.fetch.enable(patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))
                return tab
            self.browser.get=get_with_local_replies
        except BaseException:
            await self.close_fixture()
            raise

    async def fulfill(self,event):
        await self.fulfill_on(self.tab,event)

    async def fulfill_on(self,tab,event):
        try:
            parsed=urlparse(event.request.url)
            status,headers,body=404,{"content-type":"text/plain"},"Local fixture only"
            if parsed.hostname=="top.gg" and parsed.path=="/bot/111/vote":
                self.vote_page_requests+=1
                if parsed.query=="fixture-granted=1":
                    self.authenticated=True
                    self.grant_requests+=1
                status,headers=200,{"content-type":"text/html"}
                if self.authenticated:
                    body='<!doctype html><title>Vote fixture</title><button>Vote</button>'
                else:
                    # The fixture ignores synthetic click() events.
                    body='<!doctype html><title>Login fixture</title><style>button{margin:100px;width:180px;height:50px}</style>' \
                        '<p>You must be logged in to vote.</p><button id="login">Login</button>' \
                        '<script>login.addEventListener("click",e=>{if(e.isTrusted)location.href='+repr(AUTHORIZE)+'})</script>'
            elif parsed.hostname=="top.gg" and parsed.path=="/api/auth/session":
                if self.block_session:
                    status,headers,body=403,{"content-type":"text/html", "cf-mitigated":"challenge"},'<title>Just a moment...</title>'
                else:
                    status,headers,body=200,{"content-type":"application/json"},'null'
            elif event.request.url==AUTHORIZE:
                self.login_requests+=1
                if self.automatic:
                    status,headers,body=302,{"location":PAGE+"?fixture-granted=1"},""
                else:
                    status,headers=200,{"content-type":"text/html"}
                    body='<!doctype html><title>Authorize fixture</title><style>button{margin:100px;width:180px;height:50px}</style>' \
                        '<button id="authorize">Authorize</button><script>' \
                        'authorize.addEventListener("click",e=>{if(e.isTrusted)location.href='+repr(PAGE+"?fixture-granted=1")+'})</script>'
            elif parsed.hostname=="discord.com" and parsed.path=="/login":
                status,headers,body=200,{"content-type":"text/html"},'<!doctype html><title>Discord fixture</title>Local login'
            await tab.send(uc.cdp.fetch.fulfill_request(event.request_id,status,
                response_headers=[uc.cdp.fetch.HeaderEntry(k,v) for k,v in headers.items()],
                body=base64.b64encode(body.encode()).decode()))
        except Exception as exc:
            self.fixture_errors.append(type(exc).__name__)

    async def close_fixture(self):
        if self.tracker: self.tracker.stop()
        process=getattr(self.browser,"_process",None)
        with suppress(Exception): await self.browser.aclose()
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(),5)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_fixture()

    async def exercise(self,automatic):
        self.automatic=automatic
        state=await asyncio.wait_for(vote.discord_oauth_login(self.tab,"local-only-fixture-token",["111"]),60)
        self.assertEqual(state,vote.AUTHENTICATED)
        self.assertEqual(self.fixture_errors,[])
        self.assertEqual(self.login_requests,1)
        self.assertEqual(self.grant_requests,1)
        self.assertEqual(await vote.current_url(self.tab),PAGE+"?fixture-granted=1")
        session=vote.AccountBrowserSession()
        session.authenticated,session.tab=True,self.tab
        self.assertTrue(await session.reusable())

    async def test_automatic_grant_can_return_without_showing_the_discord_dialog(self):
        await self.exercise(True)

    async def test_normal_discord_dialog_still_receives_native_authorize_input(self):
        await self.exercise(False)

    async def test_real_login_and_oauth_work_when_only_the_session_api_returns_403(self):
        self.block_session=True
        await self.exercise(False)

    async def test_cookie_first_account_reaches_oauth_after_a_session_api_403(self):
        self.block_session=True
        session=vote.AccountBrowserSession()
        session.browser,session.tab,session.diagnostics=self.browser,self.tab,self.tracker
        cookies=vote.load_topgg_cookies(raw=json.dumps([
            {"name":"authjs.session-token", "value":"local-only-cookie", "domain":".top.gg", "path":"/", "secure":True}]))[0]
        try:
            with patch("vote.vote_for_bot",new=AsyncMock(return_value={"bot_id":"111","status":"success"})) as cast:
                results=await asyncio.wait_for(vote._run_account("local-only-fixture-token",["111"],"fixture",
                    cookies,
                    session=session),75)
            self.assertEqual(results[0]["status"],"success")
            self.assertEqual(self.login_requests,1)
            self.assertEqual(self.grant_requests,1)
            self.assertEqual(self.fixture_errors,[])
            self.assertEqual(self.vote_page_requests,2)
            cast.assert_awaited_once()
        finally:
            # Fixture teardown owns this browser and the interception handler.
            session.browser=session.tab=session.diagnostics=None

    async def test_public_login_exception_requires_an_uncovered_enabled_control_and_no_challenge(self):
        self.block_session=True
        variants = [
            "login.disabled=true",
            "const cover=document.createElement('div');cover.style='position:fixed;inset:0;background:white';document.body.append(cover)",
            "const gate=document.createElement('div');gate.id='challenge-stage';gate.textContent='Verify you are human';document.body.append(gate)",
        ]
        for script in variants:
            with self.subTest(script=script):
                await vote.navigate_page(self.tab,PAGE)
                await vote.evaluate(self.tab,script)
                self.assertFalse(await vote.public_topgg_login_ready(self.tab))
        self.assertEqual(self.login_requests,0)
