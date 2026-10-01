"""Real CDP events and native input; all HTTP requests are fulfilled locally.

These fixtures exercise the production diagnostic installation and vote path.
No request reaches Top.gg, Cloudflare, or any other remote server.
"""

import asyncio
import base64
import json
import os
import shutil
import tempfile
import unittest
from contextlib import suppress
from unittest.mock import AsyncMock, patch

import nodriver as uc

import request_diagnostics
import vote
import cloudflare_click
from recovery_state import SubmissionJournal


CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
PAGE = "https://top.gg/bot/111/vote"


@unittest.skipUnless(CHROME, "Chrome needed for real CDP vote flow fixtures")
class LiveVoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.profile = tempfile.TemporaryDirectory(prefix="live-vote-fixture-")
        self.browser = uc.Browser(uc.Config(
            headless=False, browser_executable_path=CHROME, user_data_dir=self.profile.name,
            sandbox=getattr(os, "geteuid", lambda: 1)() != 0,
            browser_args=["--window-size=1280,720", "--disable-dev-shm-usage", "--no-proxy-server"],
        ))
        self.session = None
        self.documents = 0
        self.mutations = 0
        self.block_reads = "never"
        self.omit_reloaded_read = False
        self.mutation_field = "castVote"
        self.mutation_error = False
        self.mutation_status = 200
        self.solved_widget = False
        self.pre_solved_widget = False
        self.hidden_widget = False
        self.late_error = False
        self.audit_error = False
        self.cloud_widget = False
        self.ad_duration = 0
        self.hydration_delay = 0
        self.stale_cooldown = False
        self.already_voted = False
        self.persist_vote = False
        self.unusable_controls = False
        self.session_delay = 0
        self.provider_documents = 0
        self.fixture_errors = []
        try:
            try:
                await asyncio.wait_for(self.browser.start(), 20)
            except Exception:
                if not await vote.recover_slow_browser_start(self.browser):
                    raise
            await self.browser.get("about:blank")
            self.session = vote.AccountBrowserSession()
            with patch("vote.start_browser", return_value=self.browser):
                _, self.tab = await self.session.acquire()
            self.tracker = self.session.diagnostics
            request_diagnostics.select_vote_bot(self.tab, "111")
            self.tab.add_handler(uc.cdp.fetch.RequestPaused, self.fulfill)
            await self.tab.send(uc.cdp.fetch.enable(patterns=[uc.cdp.fetch.RequestPattern(url_pattern="*")]))
        except BaseException:
            await self.close_fixture()
            raise

    def html(self):
        if self.persist_vote and self.mutations:
            return '<!doctype html><title>Voting fixture</title><div>Thanks for voting!</div><script>window.readDone=true;</script>'
        read = not (self.omit_reloaded_read and self.documents > 1)
        query = json.dumps({"query": 'query State { canVote(botId:"111") }'})
        mutation = json.dumps({"query": 'mutation Cast { ' + self.mutation_field + '(botId:"111") { ok } }'})
        widget = ('<div class="cf-turnstile">Verification complete</div>'
                  '<input type="hidden" name="cf-turnstile-response" value="PRIVATE_FIXTURE_RESPONSE">') if self.solved_widget else ""
        frame = ('<iframe src="https://challenges.cloudflare.com/local-fixture" '
                 'style="position:absolute;left:40px;top:30px;width:400px;height:280px;border:0"></iframe>'
                 '<script>window.widgetClick=null;addEventListener("message",e=>{'
                 'if(e.origin==="https://challenges.cloudflare.com") widgetClick=e.data;});</script>') if self.cloud_widget else ""
        return """<!doctype html><title>Voting fixture</title>
            <style>button { margin:100px;width:180px;height:50px }</style>
            __BEFORE_WIDGET__<section id="surface">__STALE____UNUSABLE__<div id="ad"></div><button id="vote">Vote</button></section>
            <script>
            window.readDone=false; window.nativeClicks=0; window.unusableClicks=0;
            const button = document.getElementById('vote');
            if (__AD_DURATION__) {
                ad.textContent='You will be able to vote after this ad'; button.disabled=true;
                setTimeout(()=>{ad.textContent=''; button.disabled=false; window.adFinishedAt=performance.now();}, __AD_DURATION__);
            }
            if (__HYDRATION_DELAY__) {
                button.style.display='none';
                setTimeout(()=>{button.style.display='';}, __HYDRATION_DELAY__);
            }
            if (__ALREADY_VOTED__) button.disabled=true;
            document.addEventListener('click',e=>{if(e.target.closest('[data-unusable]')) unusableClicks++;});
            const post = body => fetch('/api/graphql', {method:'POST',
                headers:{'content-type':'application/json'},body:JSON.stringify(body)});
            if (__READ__) post(__QUERY__).then(()=>readDone=true).catch(()=>readDone=true);
            else readDone=true;
            vote.addEventListener('click', async e => {
                if (!e.isTrusted) return;
                nativeClicks++;
                window.clickedAt=performance.now();
                await post(__MUTATION__);
                // Model optimistic UI as well as normal acknowledgements.
                if (__AUDIT__) await post({query:'mutation Audit { auditBotVote(botId:"111") { ok } }'});
                surface.innerHTML='Thanks for voting!'+__WIDGET__;
                if (__LATE_ERROR__) setTimeout(()=>post(__MUTATION__), 6000);
            });
            </script>__FRAME__""".replace("__READ__", json.dumps(read)).replace("__QUERY__", query).replace(
                "__MUTATION__", mutation).replace("__WIDGET__", json.dumps(widget)).replace(
                    "__AUDIT__", json.dumps(self.audit_error)).replace("__FRAME__", frame).replace(
                    "__LATE_ERROR__", json.dumps(self.late_error)).replace("__BEFORE_WIDGET__",
                    ('Verify you are human' + widget if self.pre_solved_widget else '') +
                    ('<iframe src="https://challenges.cloudflare.com/local-fixture" style="display:none;width:200px;height:100px"></iframe>' if self.hidden_widget else '')).replace(
                        "__AD_DURATION__", str(self.ad_duration)).replace("__HYDRATION_DELAY__", str(self.hydration_delay)).replace(
                        "__STALE__", '<div>You can vote again in 11 hours. Something went wrong.</div>' if self.stale_cooldown else
                        '<div>You can vote again in 1 hour.</div>' if self.already_voted else '').replace(
                        "__UNUSABLE__", ('<fieldset disabled><button data-unusable>Vote</button></fieldset>'
                            '<div inert><button data-unusable>Vote</button></div>'
                            '<div style="position:relative"><button data-unusable>Vote</button>'
                            '<div style="position:absolute;inset:0;background:white;z-index:1"></div></div>') if self.unusable_controls else '').replace(
                            "__ALREADY_VOTED__", json.dumps(self.already_voted))

    async def fulfill(self, event):
        try:
            url = event.request.url
            status, headers, body = 404, {"content-type": "text/plain"}, "Local fixture only"
            if url == PAGE:
                self.documents += 1
                status, headers, body = 200, {"content-type": "text/html"}, self.html()
            elif url == "https://challenges.cloudflare.com/local-fixture":
                self.provider_documents += 1
                status, headers = 200, {"content-type":"text/html"}
                body = '''<!doctype html><div id="host"></div><script>
                    const root=host.attachShadow({mode:'closed'});
                    root.innerHTML='<label style="display:block;margin:60px"><input type="checkbox" style="width:30px;height:30px">Verify</label>';
                    root.querySelector('input').addEventListener('click',e=>parent.postMessage({trusted:e.isTrusted},'https://top.gg'));
                    parent.postMessage({ready:true},'https://top.gg');
                </script>'''
            elif url == "https://top.gg/api/graphql":
                raw = event.request.post_data or ""
                if "query State" in raw:
                    blocked = self.block_reads == "always" or (self.block_reads == "first" and self.documents == 1)
                    if blocked:
                        status, headers, body = 403, {"content-type": "text/html", "cf-mitigated": "challenge"}, "Denied"
                    else:
                        status, headers, body = 200, {"content-type": "application/json"}, json.dumps({"data": {"canVote": not self.already_voted}})
                elif "auditBotVote" in raw:
                    status, headers, body = 200, {"content-type":"application/json"}, '{"errors":[{"extensions":{"code":"INTERNAL_SERVER_ERROR"}}]}'
                else:
                    self.mutations += 1
                    status, headers = self.mutation_status, {"content-type": "application/json"}
                    body = ('{"errors":[{"extensions":{"code":"CAPTCHA_REQUIRED"},"message":"PRIVATE_FIXTURE_ERROR"}]}'
                            if self.mutation_error else json.dumps({"data": {self.mutation_field: {"ok": True}}}))
                    if self.late_error and self.mutations > 1:
                        body = '{"errors":[{"extensions":{"code":"INTERNAL_SERVER_ERROR"}}]}'
            elif url == "https://top.gg/api/auth/session":
                await asyncio.sleep(self.session_delay)
                status, headers, body = 200, {"content-type": "application/json"}, '{"user":{"id":"LOCAL_FIXTURE"}}'
            await self.tab.send(uc.cdp.fetch.fulfill_request(
                event.request_id, status,
                response_headers=[uc.cdp.fetch.HeaderEntry(k, v) for k, v in headers.items()],
                body=base64.b64encode(body.encode()).decode(),
            ))
        except Exception as exc:
            self.fixture_errors.append(type(exc).__name__)

    async def wait(self, predicate):
        async def observe():
            while not predicate():
                await asyncio.sleep(0.05)
        await asyncio.wait_for(observe(), 10)
        self.assertEqual(self.fixture_errors, [])

    async def load(self):
        session_id = self.tab.session_id
        # Setup itself uses the existing session; the cookie test below exercises
        # production navigation separately, including its original get() bug.
        await self.tab.send(uc.cdp.page.navigate(PAGE))
        self.assertEqual(self.tab.session_id, session_id)
        for _ in range(100):
            if await vote.evaluate(self.tab, "window.readDone === true"):
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("Local fixture did not initialize: " + repr(self.fixture_errors))
        await self.wait(lambda: any(x["vote_network"]["operation"] == "vote_state"
                                   for x in self.tracker.completed.values()))

    async def exercise_vote(self):
        with patch("vote.VOTE_NETWORK_PREFLIGHT_DELAY_SEC", 0.02):
            return await asyncio.wait_for(vote.vote_for_bot(self.tab, "111", "fixture"), 30)

    async def close_fixture(self):
        if self.session and self.session.diagnostics:
            self.session.diagnostics.stop()
        process = getattr(self.browser, "_process", None)
        with suppress(Exception):
            await self.browser.aclose()
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.profile.cleanup()

    async def asyncTearDown(self):
        await self.close_fixture()

    async def test_production_installation_reads_real_graphql_events_and_bodies(self):
        await self.load()
        await self.wait(lambda: any(x["vote_network"]["response_outcome"] == "usable"
                                   for x in self.tracker.completed.values()))
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.tracker.vote_network.submission_outcome(), "usable")
        self.assertEqual(await vote.evaluate(self.tab, "window.nativeClicks"), 1)

    async def recover_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "vote-recovery.json")
            journal = SubmissionJournal(path, ["local-token"], ["111"])
            journal.select("local-token", "111")
            journal.before_press()
            recovered = SubmissionJournal(path, ["local-token"], ["111"])
            self.session.authenticated = True
            with (patch("vote.RECOVERY_JOURNAL", recovered),
                  patch("vote.topgg_auth_state", new=AsyncMock(return_value=vote.AUTHENTICATED))):
                result = (await asyncio.wait_for(vote._run_account(
                    "local-token", ["111"], "fixture", session=self.session), 45))[0]
            self.assertEqual(recovered.records[recovered.active_key]["kind"], "complete")
            return result

    async def test_pending_handoff_waits_a_ten_second_ad_then_votes_once(self):
        self.ad_duration = 10000
        await self.load()
        result = await self.recover_pending()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 2, "Recovery must retain the new document through the ad")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(await vote.evaluate(self.tab, "window.nativeClicks"), 1)
        self.assertTrue(await vote.evaluate(self.tab, "clickedAt >= adFinishedAt && adFinishedAt >= 10000"))

    async def test_pending_handoff_waits_for_the_app_to_hydrate_without_reload_loops(self):
        self.hydration_delay = 8000
        await self.load()
        result = await self.recover_pending()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 2)
        self.assertEqual(self.mutations, 1)

    async def test_stale_cooldown_with_a_vote_and_error_does_not_finish_without_input(self):
        self.stale_cooldown = self.persist_vote = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.documents, 2, "Stale evidence still needs independent confirmation")

    async def test_multiple_vote_controls_choose_the_unobstructed_enabled_one(self):
        self.unusable_controls = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(await vote.evaluate(self.tab, "window.unusableClicks"), 0)
        self.assertEqual(await vote.evaluate(self.tab, "window.nativeClicks"), 1)

    async def test_genuine_cooldown_with_a_disabled_vote_is_accepted_without_input(self):
        self.already_voted = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "cooldown")
        self.assertEqual(self.mutations, 0)
        self.assertEqual(await vote.evaluate(self.tab, "window.nativeClicks"), 0)
        self.assertEqual(self.documents, 1)

    async def test_nine_second_session_response_is_not_cancelled_by_the_cdp_default(self):
        self.session_delay = 9
        await self.load()
        self.assertTrue((await vote.topgg_session_probe(self.tab))["authenticated"])
        self.assertEqual(self.fixture_errors, [])

    async def test_cookie_navigation_preserves_the_session_owning_network_observers(self):
        session_id = self.tab.session_id
        result = await vote.login_with_cookies(self.tab, [vote._normalize_cookie({
            "name":"authjs.session-token", "value":"LOCAL_FIXTURE_COOKIE",
            "domain":".top.gg", "path":"/", "secure":True,
        })], ["111"])
        self.assertEqual(result, vote.AUTHENTICATED)
        self.assertEqual(self.tab.session_id, session_id)
        await self.wait(lambda: any(x["vote_network"]["response_outcome"] == "usable"
                                   for x in self.tracker.completed.values()))

    async def test_unrelated_mutation_error_does_not_override_an_identified_healthy_vote(self):
        self.audit_error = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 1)
        self.assertEqual(self.mutations, 1)

    async def load_widget(self):
        self.cloud_widget = True
        await self.load()
        for _ in range(100):
            if await vote.evaluate(self.tab, "window.widgetClick?.ready === true"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.provider_documents, 1, "Provider document must be fulfilled locally")
        self.assertTrue(await vote.evaluate(self.tab, "window.widgetClick?.ready === true"),
                        "Cross-origin fixture must finish loading before input")
        self.assertTrue((await cloudflare_click.widget_state(self.tab))["present"])

    async def test_cross_origin_closed_shadow_control_is_reachable_without_image_matching(self):
        from unittest.mock import AsyncMock
        await self.load_widget()
        session_id = self.tab.session_id
        await vote.evaluate(self.tab, """(() => {
            const frame = document.querySelector('iframe');
            frame.style.transform = 'scale(0.75)';
            frame.style.transformOrigin = 'top left';
        })()""")
        with (patch.object(cloudflare_click, "match_checkbox", side_effect=AssertionError("semantic target required")),
              patch("builtins.print") as output):
            result = await cloudflare_click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False))
        self.assertEqual(result, "sent", repr([str(x.args[0]) for x in output.call_args_list]))
        self.assertEqual(self.tab.session_id, session_id)
        for _ in range(100):
            value = await vote.evaluate(self.tab, "window.widgetClick")
            if isinstance(value, dict) and "trusted" in value:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(value, {"trusted":True})
        receipts = [json.loads(str(x.args[0]).split(': ',1)[1]) for x in output.call_args_list
                    if str(x.args[0]).startswith('  → Cloudflare mouse input: ')]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["target_source"], "frame_dom")
        self.assertTrue(all(receipts[0]["trusted_events"].values()))

    async def test_parent_overlay_blocks_cross_origin_checkbox_input(self):
        from unittest.mock import AsyncMock
        await self.load_widget()
        await vote.evaluate(self.tab, """(() => {
            const cover = document.createElement('div');
            cover.style.cssText='position:fixed;inset:0;background:white;z-index:1000';
            document.body.append(cover);
        })()""")
        with (patch.object(cloudflare_click, "match_checkbox", side_effect=AssertionError("semantic target required")),
              patch.object(cloudflare_click, "TARGET_WAIT_SEC", 2)):
            result = await cloudflare_click.click_cloudflare_checkbox(self.tab, vote.evaluate, AsyncMock(return_value=False))
        self.assertEqual(result, "unavailable")
        self.assertEqual(await vote.evaluate(self.tab, "window.widgetClick"), {"ready":True})

    async def test_a_new_document_does_not_inherit_an_old_api_denial(self):
        self.block_reads, self.omit_reloaded_read = "first", True
        await self.load()
        self.assertTrue(self.tracker.vote_network.protection_pending())
        # Use the production reload/settle/input path, rather than calling its
        # lowest input primitive 250ms after a fresh visible-Chrome navigation.
        # The reopened page deliberately omits its state query: only the new
        # committed document can release the old denial, not a healthy read.
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 2)
        self.assertEqual(self.mutations, 1)
        self.assertEqual(await vote.evaluate(self.tab, "window.nativeClicks"), 1)

    async def test_preflight_has_one_real_recovery_before_giving_up(self):
        self.block_reads = "first"
        await self.load()
        self.assertTrue(self.tracker.vote_network.protection_pending())
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 2)
        self.assertEqual(self.mutations, 1)

    async def test_persistent_api_denial_is_bounded_and_never_clicked(self):
        self.block_reads = "always"
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["vote_submitted"])
        self.assertEqual(self.documents, 2)
        self.assertEqual(self.mutations, 0)

    async def test_unknown_bot_mutation_error_cannot_accept_optimistic_ack(self):
        self.mutation_field, self.mutation_error = "submitEntityBallot", True
        await self.load()
        result = await self.exercise_vote()
        self.assertNotEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.tracker.vote_network.submission_outcome(), "captcha_required")
        self.assertNotIn("PRIVATE", repr(self.tracker.completed))

    async def test_http_denial_cannot_accept_an_optimistic_acknowledgement(self):
        self.mutation_field, self.mutation_status = "submitEntityBallot", 403
        await self.load()
        result = await self.exercise_vote()
        self.assertNotEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertGreater(self.documents, 1)

    async def test_solved_widget_does_not_force_reload_after_fresh_ack(self):
        self.solved_widget = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.documents, 1)
        self.assertEqual(self.mutations, 1)

    async def test_missing_observer_forces_independent_document_verification(self):
        await self.load()
        self.tracker.stop()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.mutations, 1)
        self.assertGreater(self.documents, 1)

    async def test_hidden_provider_frame_does_not_block_native_vote(self):
        self.hidden_widget = True
        await self.load()
        self.assertFalse(await vote.is_turnstile_present(self.tab))
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.documents, 1)

    async def test_solved_widget_instruction_copy_does_not_block_native_vote(self):
        self.solved_widget = self.pre_solved_widget = True
        await self.load()
        self.assertTrue(await vote.is_turnstile_solved(self.tab))
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.documents, 1)

    async def test_late_application_error_during_stable_acknowledgement_is_not_success(self):
        self.late_error = True
        await self.load()
        result = await self.exercise_vote()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.mutations, 2)
        self.assertGreater(self.documents, 1)


if __name__ == "__main__":
    unittest.main()
