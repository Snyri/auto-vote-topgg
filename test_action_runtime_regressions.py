"""Production paths for late responses, document changes and fast OAuth."""

import asyncio
import json
import subprocess
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import request_diagnostics
import vote
from test_graphql_vote import payload
from test_vote_control_selection import NODE, NODE_HARNESS
from test_vote_page_receipt import DOM_HARNESS


PAGE = "https://top.gg/bot/111/vote"


class LateResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tab = MagicMock()
        self.tab.send = AsyncMock()
        self.tracker = request_diagnostics.RequestDiagnostics(self.tab)
        self.tracker.vote_network.select_bot("111")

    async def request(self, request_id, query, stamp):
        await self.tracker.on_request(NS(request_id=request_id, wall_time=stamp, document_url=PAGE,
            redirect_response=None, request=NS(url="https://top.gg/api/graphql", method="POST", post_data=payload(query))))

    async def test_json_headers_arriving_after_finish_release_a_recovered_api_gate(self):
        with patch("builtins.print"):
            await self.request("denied", 'query Cast { canVote(botId:"111") }', 100)
            await self.tracker.on_response(NS(request_id="denied", response=NS(status=403,
                headers={"content-type":"text/html", "cf-mitigated":"challenge"})))
            await self.tracker.on_finished(NS(request_id="denied"))
            await self.request("ready", 'query Cast { canVote(botId:"111") }', 101)
            await self.tracker.on_response(NS(request_id="ready", response=NS(status=200, headers={})))
            await self.tracker.on_finished(NS(request_id="ready"))
            self.assertTrue(self.tracker.vote_network.protection_pending())
            self.tab.send.assert_not_awaited()
            self.tab.send.return_value = ('{"data":{"canVote":true}}', False)
            await self.tracker.on_extra(NS(request_id="ready", status_code=200,
                headers={"content-type":"application/json"}))
        self.assertFalse(self.tracker.vote_network.protection_pending())
        self.tab.send.assert_awaited_once()

    async def test_late_json_submission_is_inspected_once_and_late_denial_still_vetoes_it(self):
        state = self.tracker.vote_network
        state.begin_input()
        state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100})
        self.tab.send.return_value = ('{"data":{"castVote":{"ok":true}}}', False)
        with patch("builtins.print"):
            await self.request("cast", 'mutation Cast { castVote(botId:"111") { ok } }', 101)
            await self.tracker.on_finished(NS(request_id="cast"))
            self.assertFalse(state.confirmation_covered())
            for _ in range(2):
                await self.tracker.on_extra(NS(request_id="cast", status_code=200,
                    headers={"content-type":"application/json"}))
            self.assertTrue(state.confirmation_covered())
            self.tab.send.assert_awaited_once()
            await self.tracker.on_extra(NS(request_id="cast", status_code=403,
                headers={"content-type":"text/html", "cf-mitigated":"challenge"}))
        self.assertFalse(state.confirmation_covered())

    async def test_previous_document_challenge_cannot_follow_a_successful_redirect(self):
        await self.tracker.on_navigated(NS(frame=NS(id_="main", loader_id="old", parent_id=None)))
        await self.tracker.on_request(NS(request_id="old", loader_id="old", frame_id="main",
            type_=NS(value="Document"), redirect_response=None,
            request=NS(url=PAGE, method="GET")))
        with patch("builtins.print"):
            await self.tracker.on_response(NS(request_id="old", response=NS(status=403,
                headers={"content-type":"text/html", "cf-mitigated":"challenge"})))
            self.assertTrue(self.tracker.document_challenged)
            await self.tracker.on_navigated(NS(frame=NS(id_="main", loader_id="discord", parent_id=None)))
            self.assertFalse(self.tracker.document_challenged)
            await self.tracker.on_extra(NS(request_id="old", status_code=403,
                headers={"content-type":"text/html", "cf-mitigated":"challenge"}))
        self.assertFalse(self.tracker.document_challenged)

    async def test_current_challenge_headers_survive_their_own_document_commit(self):
        await self.tracker.on_request(NS(request_id="current", loader_id="current", frame_id="main",
            type_=NS(value="Document"), redirect_response=None,
            request=NS(url=PAGE, method="GET")))
        with patch("builtins.print"):
            await self.tracker.on_response(NS(request_id="current", response=NS(status=403,
                headers={"content-type":"text/html", "cf-mitigated":"challenge"})))
        await self.tracker.on_navigated(NS(frame=NS(id_="main", loader_id="current", parent_id=None)))
        self.assertTrue(self.tracker.document_challenged)

    async def test_overlapping_finish_and_extra_handlers_inspect_the_body_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def body(_command):
            started.set()
            await release.wait()
            return ('{"data":{"canVote":true}}',False)
        self.tab.send.side_effect = body
        with patch("builtins.print"):
            await self.request("ready",'query Cast { canVote(botId:"111") }',101)
            await self.tracker.on_response(NS(request_id="ready",response=NS(status=200,
                headers={"content-type":"application/json"})))
            finishing = asyncio.create_task(self.tracker.on_finished(NS(request_id="ready")))
            try:
                await asyncio.wait_for(started.wait(),1)
                await self.tracker.on_extra(NS(request_id="ready",status_code=200,
                    headers={"content-type":"application/json"}))
                self.tab.send.assert_awaited_once()
            finally:
                release.set()
                await finishing
        self.assertEqual(self.tracker.completed["ready"]["vote_network"]["response_outcome"],"usable")


class FastOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_login_document_is_not_an_oauth_return(self):
        snapshot={"url":PAGE,"epoch":1}
        with (
            patch("vote.TIMEOUT_OAUTH_SEC",0.01),
            patch("vote.evaluate",new=AsyncMock(return_value=snapshot)),
            patch("vote.topgg_page_auth_hint",new=AsyncMock(return_value=vote.AUTH_INVALID)),
        ):
            self.assertIsNone(await vote.wait_for_oauth_start(None,snapshot))

    async def test_callback_wrong_origin_and_insecure_url_are_not_completed_returns(self):
        for url in ("https://top.gg/api/auth/callback/discord", "https://top.gg.example.com/bot/111/vote",
                    "http://top.gg/bot/111/vote","https://top.gg:444/bot/111/vote",
                    "https://private@top.gg/bot/111/vote"):
            with (
                self.subTest(url=url),patch("vote.TIMEOUT_OAUTH_SEC",0.01),
                patch("vote.evaluate",new=AsyncMock(return_value={"url":url,"epoch":2})),
                patch("vote.topgg_page_auth_hint",new=AsyncMock(return_value=vote.AUTHENTICATED)) as hint,
            ):
                self.assertIsNone(await vote.wait_for_oauth_start(None,{"url":PAGE,"epoch":1}))
                hint.assert_not_awaited()

    async def test_hanging_auth_hint_is_cancelled_within_the_redirect_budget(self):
        cancelled=asyncio.Event()
        async def hint(_tab):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with (
            patch("vote.TIMEOUT_OAUTH_SEC",0.01),patch("vote.topgg_page_auth_hint",new=hint),
            patch("vote.evaluate",new=AsyncMock(return_value={"url":PAGE,"epoch":1})),
        ):
            self.assertIsNone(await vote.wait_for_oauth_start(None,{"url":PAGE,"epoch":1}))
        self.assertTrue(cancelled.is_set())

    async def test_new_document_without_a_session_is_still_rejected(self):
        with (
            patch("builtins.print"),patch("vote.navigate_page",new=AsyncMock()),
            patch("vote.asyncio.sleep",new=AsyncMock()),patch("vote.settle_privacy_overlay",new=AsyncMock()),
            patch("vote.topgg_auth_state",new=AsyncMock(return_value=vote.AUTH_INVALID)),
            patch("vote.current_url",new=AsyncMock(side_effect=["https://discord.com/login",PAGE])),
            patch("vote.evaluate",new=AsyncMock()),patch("vote._click_exact_element",new=AsyncMock(return_value=True)),
            patch("vote.wait_for_oauth_start",new=AsyncMock(return_value="topgg")),
        ):
            self.assertEqual(await vote.discord_oauth_login(AsyncMock(),"fixture-token",["111"]),vote.AUTH_INVALID)

    async def test_automatic_discord_grant_can_return_to_topgg_before_the_first_poll(self):
        epochs = iter([1, 2])
        urls = iter(["https://discord.com/login"])
        async def evaluate(_tab, script, **_kwargs):
            return {"url": PAGE, "epoch": next(epochs)} if "performance.timeOrigin" in script else None
        with (
            patch("builtins.print"), patch("vote.TIMEOUT_OAUTH_SEC", 0.01),
            patch("vote.navigate_page", new=AsyncMock()), patch("vote.asyncio.sleep", new=AsyncMock()),
            patch("vote.evaluate", new=evaluate), patch("vote.settle_privacy_overlay", new=AsyncMock()),
            patch("vote.dismiss_privacy_overlay", new=AsyncMock()),
            patch("vote.topgg_auth_state", new=AsyncMock(side_effect=[vote.AUTH_INVALID, vote.AUTH_INVALID, vote.AUTHENTICATED])),
            patch("vote.current_url", new=AsyncMock(side_effect=lambda _: next(urls, PAGE))),
            patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value="unknown")),
            patch("vote._mark_exact_element", new=AsyncMock(return_value=True)),
            patch("vote._click_marked", new=AsyncMock(return_value=True)),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
            patch("vote._handle_discord_oauth", new=AsyncMock()) as authorize,
        ):
            state = await vote.discord_oauth_login(AsyncMock(), "local-fixture-token", ["111"])
        self.assertEqual(state, vote.AUTHENTICATED)
        authorize.assert_not_awaited()


@unittest.skipUnless(NODE, "Node.js needed for production DOM observers")
class ApplicationSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, script, harness, fixture):
        result = await asyncio.to_thread(subprocess.run, [NODE, "-e", harness],
            input=json.dumps({"expression":script, **fixture}), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    async def test_covered_login_copy_does_not_hide_the_clickable_login(self):
        with patch("vote.evaluate", new=AsyncMock(return_value=False)) as evaluate:
            await vote._mark_exact_element(None, "button", ["Login"], "data-auto-vote")
        observed = await self.execute(evaluate.await_args.args[1], NODE_HARNESS,
            {"controls":[{"text":"Login", "covered":True}, {"text":"Login"}]})
        self.assertEqual(observed["marked"], [{"index":1,"tag":"button"}])

    async def test_strong_vote_acknowledgement_preserves_an_authenticated_profile(self):
        tab = MagicMock()
        async def evaluate(_tab, script, **_kwargs):
            return await self.execute(script, DOM_HARNESS, {"fixture":{"text":"Thanks for voting!"}})
        session = vote.AccountBrowserSession()
        session.browser, session.tab, session.authenticated = MagicMock(), tab, True
        with (
            patch("vote.evaluate", new=evaluate), patch("vote.current_url", new=AsyncMock(return_value=PAGE)),
            patch("vote.document_ready", new=AsyncMock(return_value=True)),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)), patch("vote.asyncio.sleep", new=AsyncMock()),
        ):
            self.assertTrue(await session.reusable())

    async def test_conflicting_success_text_does_not_preserve_a_profile(self):
        for fixture in ({"text":"Thanks for voting! Something went wrong."},
                        {"text":"Thanks for voting! You must be logged in to vote."},
                        {"text":"Thanks for voting!","elements":[{"tag":"iframe","attrs":{"src":"https://challenges.cloudflare.com/fixture"}}]},
                        {"text":"Thanks for voting!","url":"https://top.gg.example.com/bot/111/vote"}):
            async def evaluate(_tab,script,**_kwargs):
                return await self.execute(script,DOM_HARNESS,{"fixture":fixture})
            with self.subTest(fixture=fixture), patch("vote.evaluate",new=evaluate), patch("vote.current_url",new=AsyncMock(return_value=PAGE)):
                self.assertNotEqual(await vote.topgg_page_auth_hint(MagicMock()),vote.AUTHENTICATED)

    async def test_existing_authenticated_vote_surface_does_not_gain_an_acknowledgement_gate(self):
        async def evaluate(_tab,script,**_kwargs):
            return await self.execute(script,DOM_HARNESS,{"fixture":{"text":"Thanks for voting!","elements":[{"text":"Vote"}]}})
        with patch("vote.evaluate",new=evaluate):
            self.assertEqual(await vote.topgg_page_auth_hint(MagicMock()),vote.AUTHENTICATED)


if __name__ == "__main__":
    unittest.main()
