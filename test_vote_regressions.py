"""Regression coverage for partial progress and bounded authentication probes."""

import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import vote


BOT_IDS = ["111", "222", "333"]


def bot_result(bot_id, status, detail=None):
    return {
        "bot_id": bot_id,
        "status": status,
        "detail": detail or status,
        "account_id": "account",
    }


class PartialProgressRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_browser_failure_preserves_success_and_marks_unattempted_bots(self):
        tab = MagicMock()
        browser = MagicMock()
        browser.__iter__.side_effect = lambda: iter([tab])
        first = bot_result("111", "success")
        first["retry_at"] = 1234567890
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.start_browser", new=AsyncMock(return_value=browser)),
            patch("vote.close_browser_safely", new_callable=AsyncMock) as close,
            patch("vote.discord_oauth_login", new=AsyncMock(return_value=vote.AUTHENTICATED)),
            patch("vote.vote_for_bot", new=AsyncMock(side_effect=[
                first,
                RuntimeError("browser disconnected"),
            ])) as vote_for_bot,
        ):
            results = await vote._run_account("token", BOT_IDS, "account")

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertEqual([item["status"] for item in results], ["success", "error", "error"])
        self.assertEqual(results[0]["retry_at"], 1234567890)
        self.assertTrue(all(item["account_id"] == "account" for item in results))
        self.assertEqual([call.args[1] for call in vote_for_bot.await_args_list], ["111", "222"])
        close.assert_awaited_once_with(browser, "account attempt")
        self.assertTrue(vote.has_business_failure([results]))

    async def test_retry_after_mid_account_exception_visits_only_remaining_bots(self):
        tab = MagicMock()
        browser = MagicMock()
        browser.__iter__.side_effect = lambda: iter([tab])
        run_account = AsyncMock(wraps=vote._run_account)
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.start_browser", new=AsyncMock(return_value=browser)),
            patch("vote.close_browser_safely", new_callable=AsyncMock),
            patch("vote.discord_oauth_login", new=AsyncMock(return_value=vote.AUTHENTICATED)),
            patch("vote._run_account", new=run_account),
            patch("vote.vote_for_bot", new=AsyncMock(side_effect=[
                bot_result("111", "success"),
                RuntimeError("browser disconnected"),
                bot_result("222", "cooldown"),
                bot_result("333", "success"),
            ])) as vote_for_bot,
        ):
            results = await vote.process_account("token", BOT_IDS, 1, 1)

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertEqual([item["status"] for item in results], ["success", "cooldown", "success"])
        self.assertEqual([call.args[1] for call in run_account.await_args_list], [BOT_IDS, ["222", "333"]])
        self.assertEqual([call.args[1] for call in vote_for_bot.await_args_list], ["111", "222", "222", "333"])
        self.assertFalse(vote.has_business_failure([results]))

    async def test_terminal_auth_failure_updates_pending_bots_without_erasing_success(self):
        for status in ("blocked", "captcha_required"):
            with self.subTest(status=status):
                first = bot_result("111", "success")
                first["retry_at"] = 1234567890
                initial = [first, bot_result("222", "error", "old error"), bot_result("333", "uncertain")]
                terminal = bot_result("all", status, "latest authentication failure")
                with (
                    patch("builtins.print"),
                    patch("vote.asyncio.sleep", new_callable=AsyncMock),
                    patch("vote._run_account", new=AsyncMock(side_effect=[
                        initial, [terminal], [terminal],
                    ])) as run_account,
                ):
                    results = await vote.process_account("token", BOT_IDS, 1, 1)

                self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
                self.assertEqual([item["status"] for item in results], ["success", status, status])
                self.assertEqual(results[0]["retry_at"], 1234567890)
                self.assertTrue(all(item["detail"] == "latest authentication failure" for item in results[1:]))
                self.assertTrue(all(call.args[1] == ["222", "333"] for call in run_account.await_args_list[1:]))
                self.assertTrue(vote.has_business_failure([results]))

    async def test_startup_failure_replaces_pending_errors_but_keeps_completed_success(self):
        initial = [
            bot_result("111", "success"),
            bot_result("222", "error", "old error"),
            bot_result("333", "uncertain", "old uncertainty"),
        ]
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote._run_account", new=AsyncMock(side_effect=[
                initial, vote.BrowserStartupError("late browser refused"),
            ])) as run_account,
        ):
            results = await vote.process_account("token", BOT_IDS, 1, 1)

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertEqual([item["status"] for item in results], ["success", "error", "error"])
        self.assertTrue(all(item["detail"].startswith(vote.BROWSER_STARTUP_DETAIL_PREFIX) for item in results[1:]))
        self.assertTrue(all("late browser refused" in item["detail"] for item in results[1:]))
        self.assertEqual(run_account.await_count, 2)
        self.assertEqual(run_account.await_args_list[1].args[1], ["222", "333"])

    async def test_empty_results_cannot_report_completion_or_drop_requested_bots(self):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote._run_account", new=AsyncMock(return_value=[])),
        ):
            results = await vote.process_account("token", BOT_IDS, 1, 1)

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertTrue(all(item["status"] == "error" for item in results))
        self.assertTrue(vote.has_business_failure([results]))

    async def test_missing_results_preserve_completed_bot_and_report_every_missing_bot(self):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote._run_account", new=AsyncMock(side_effect=[
                [bot_result("111", "success")], [], [],
            ])) as run_account,
        ):
            results = await vote.process_account("token", BOT_IDS, 1, 1)

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertEqual([item["status"] for item in results], ["success", "error", "error"])
        self.assertTrue(all(call.args[1] == ["222", "333"] for call in run_account.await_args_list[1:]))
        self.assertTrue(vote.has_business_failure([results]))

    async def test_missing_bot_is_retried_without_repeating_completed_bots(self):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote._run_account", new=AsyncMock(side_effect=[
                [bot_result("111", "success"), bot_result("222", "cooldown")],
                [bot_result("333", "success")],
            ])) as run_account,
        ):
            results = await vote.process_account("token", BOT_IDS, 1, 1)

        self.assertEqual([item["bot_id"] for item in results], BOT_IDS)
        self.assertEqual([item["status"] for item in results], ["success", "cooldown", "success"])
        self.assertEqual(run_account.await_count, 2)
        self.assertEqual(run_account.await_args_list[1].args[1], ["333"])


class SessionProbeTimeoutRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_browser_probe_is_cancelled_and_classified_as_blocked(self):
        original_wait_for = asyncio.wait_for
        cancelled = asyncio.Event()
        requested_timeouts = []

        async def hanging_evaluate(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def short_wait_for(awaitable, timeout):
            requested_timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=0.01)

        with (
            patch("builtins.print"),
            patch("vote.evaluate", new=hanging_evaluate),
            patch("vote.asyncio.wait_for", new=short_wait_for),
        ):
            probe = await original_wait_for(vote.topgg_session_probe(MagicMock()), timeout=0.5)

        self.assertEqual(requested_timeouts, [vote.SESSION_PROBE_TIMEOUT_SEC + 2])
        self.assertTrue(cancelled.is_set())
        self.assertFalse(probe["authenticated"])
        self.assertTrue(probe["error"])
        self.assertTrue(vote.probe_looks_blocked(probe))


class AuthenticationEvidenceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_probe_values_never_establish_authentication(self):
        valid = {
            "status": 200,
            "ok": True,
            "jsonOk": True,
            "userPresent": True,
            "contentType": "application/json",
        }
        invalid_values = [True, False, 1, "authenticated", None, []]
        invalid_values.extend({**valid, field: value} for field, value in (
            ("ok", 1),
            ("ok", "true"),
            ("jsonOk", 1),
            ("jsonOk", "false"),
            ("userPresent", 1),
            ("userPresent", "false"),
            ("status", 403),
            ("cfMitigated", "challenge"),
        ))
        for value in invalid_values:
            with self.subTest(value=value):
                with (
                    patch("builtins.print"),
                    patch("vote.evaluate", new=AsyncMock(return_value=value)),
                ):
                    probe = await vote.topgg_session_probe(MagicMock())
                self.assertFalse(probe["authenticated"])

        with (
            patch("builtins.print"),
            patch("vote.evaluate", new=AsyncMock(return_value=valid)),
        ):
            self.assertTrue((await vote.topgg_session_probe(MagicMock()))["authenticated"])

    async def test_http_200_challenge_is_blocked_even_with_json_and_stale_login_hint(self):
        probe = {
            "authenticated": False,
            "status": 200,
            "json_ok": True,
            "cf_mitigated": "challenge",
        }
        with (
            patch("builtins.print"),
            patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
            patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value=vote.AUTH_INVALID)),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=False)),
            patch("vote.topgg_session_probe", new=AsyncMock(return_value=probe)),
        ):
            state = await vote.topgg_auth_state(MagicMock())
        self.assertEqual(state, vote.AUTH_BLOCKED)

    async def test_post_challenge_loading_can_settle_without_fetching_a_session(self):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch.object(vote, "AUTH_PAGE_SETTLE_POLLS", 3),
            patch("vote.topgg_page_auth_hint", new=AsyncMock(side_effect=[
                "unknown", "unknown", "unknown", vote.AUTHENTICATED,
            ])),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
            patch("vote.solve_turnstile", new=AsyncMock(return_value=True)),
            patch("vote.topgg_session_probe", new_callable=AsyncMock) as probe,
        ):
            state = await vote.topgg_auth_state(MagicMock())
        self.assertEqual(state, vote.AUTHENTICATED)
        probe.assert_not_awaited()

    async def test_post_challenge_unknown_page_defers_without_fetching_a_session(self):
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value="unknown")),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
            patch("vote.solve_turnstile", new=AsyncMock(return_value=True)),
            patch("vote.topgg_session_probe", new_callable=AsyncMock) as probe,
        ):
            state = await vote.topgg_auth_state(MagicMock())
        self.assertEqual(state, vote.AUTH_BLOCKED)
        probe.assert_not_awaited()


class PersistentChallengeClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_interstitial_detector_requires_a_boolean_signal(self):
        for result in (None, "true", "Just a moment...", {}, {"value": True}, 1):
            with (
                self.subTest(result=result),
                patch("vote.evaluate", new=AsyncMock(return_value=result)),
            ):
                self.assertFalse(await vote.is_cloudflare_challenge_page(MagicMock()))

    async def test_auth_timeout_distinguishes_interstitial_from_standalone_captcha(self):
        for managed, expected in ((True, vote.AUTH_BLOCKED), (False, vote.AUTH_CAPTCHA_REQUIRED)):
            with (
                self.subTest(managed=managed),
                patch("builtins.print"),
                patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
                patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value="unknown")),
                patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
                patch("vote.solve_turnstile", new=AsyncMock(return_value=False)) as solver,
                patch("vote.is_cloudflare_challenge_page", new=AsyncMock(return_value=managed), create=True),
                patch("vote.topgg_session_probe", new_callable=AsyncMock) as probe,
            ):
                self.assertEqual(await vote.topgg_auth_state(MagicMock()), expected)
                solver.assert_awaited_once()
                probe.assert_not_awaited()

    async def test_classification_preserves_existing_recovery_limits(self):
        async def run_account(*args, **kwargs):
            return [{"bot_id": "all", "status": await vote.topgg_auth_state(MagicMock())}]

        for managed, expected, attempts in (
            (True, vote.AUTH_BLOCKED, vote.MAX_BLOCKED_ATTEMPTS),
            (False, vote.AUTH_CAPTCHA_REQUIRED, 1),
        ):
            with (
                self.subTest(managed=managed),
                patch("builtins.print"),
                patch("vote.asyncio.sleep", new_callable=AsyncMock),
                patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock),
                patch("vote.topgg_page_auth_hint", new=AsyncMock(return_value="unknown")),
                patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
                patch("vote.solve_turnstile", new=AsyncMock(return_value=False)),
                patch("vote.is_cloudflare_challenge_page", new=AsyncMock(return_value=managed), create=True),
                patch("vote._run_account", new=AsyncMock(side_effect=run_account)) as account,
            ):
                results = await vote.process_account("test-token", ["111"], 1, 1)
                self.assertEqual(results[0]["status"], expected)
                self.assertEqual(account.await_count, attempts)
                self.assertEqual(vote.should_request_protection_retry([results]), managed)
                self.assertTrue(vote.has_business_failure([results]))

    async def test_oauth_dialog_propagates_interstitial_timeout(self):
        with (
            patch("builtins.print"),
            patch("vote.current_url", new=AsyncMock(return_value="https://discord.com/oauth2/authorize")),
            patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
            patch("vote.solve_turnstile", new=AsyncMock(return_value=False)),
            patch("vote.is_cloudflare_challenge_page", new=AsyncMock(return_value=True), create=True),
        ):
            self.assertEqual(await vote._handle_discord_oauth(MagicMock()), vote.AUTH_BLOCKED)

    async def test_oauth_login_preserves_blocked_dialog_result(self):
        tab = AsyncMock()
        with (
            patch("builtins.print"),
            patch("vote.asyncio.sleep", new_callable=AsyncMock),
            patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
            patch("vote.topgg_auth_state", new=AsyncMock(return_value=vote.AUTH_INVALID)),
            patch("vote.current_url", new=AsyncMock(side_effect=[
                "https://discord.com/login", "https://discord.com/oauth2/authorize",
            ])),
            patch("vote.evaluate", new_callable=AsyncMock),
            patch("vote._mark_exact_element", new=AsyncMock(return_value=True)),
            patch("vote._click_marked", new=AsyncMock(return_value=True)),
            patch("vote.wait_for_domain", new=AsyncMock(return_value=True)) as wait,
            patch("vote._handle_discord_oauth", new=AsyncMock(return_value=vote.AUTH_BLOCKED)),
        ):
            self.assertEqual(await vote.discord_oauth_login(tab, "test-token", ["111"]), vote.AUTH_BLOCKED)
            self.assertEqual(wait.await_count, 1)

    async def test_oauth_redirect_timeouts_distinguish_persistent_challenge(self):
        for waits in ([False], [True, False]):
            with (
                self.subTest(waits=waits),
                patch("builtins.print"),
                patch("vote.asyncio.sleep", new_callable=AsyncMock),
                patch("vote.settle_privacy_overlay", new_callable=AsyncMock),
                patch("vote.topgg_auth_state", new=AsyncMock(return_value=vote.AUTH_INVALID)),
                patch("vote.current_url", new=AsyncMock(side_effect=[
                    "https://discord.com/login", "https://discord.com/oauth2/authorize",
                ])),
                patch("vote.evaluate", new_callable=AsyncMock),
                patch("vote._mark_exact_element", new=AsyncMock(return_value=True)),
                patch("vote._click_marked", new=AsyncMock(return_value=True)),
                patch("vote.wait_for_domain", new=AsyncMock(side_effect=waits)),
                patch("vote._handle_discord_oauth", new=AsyncMock(return_value=vote.AUTHENTICATED)),
                patch("vote.is_turnstile_present", new=AsyncMock(return_value=True)),
                patch("vote.solve_turnstile", new=AsyncMock(return_value=False)),
                patch("vote.is_cloudflare_challenge_page", new=AsyncMock(return_value=True), create=True),
            ):
                result = await vote.discord_oauth_login(AsyncMock(), "test-token", ["111"])
                self.assertEqual(result, vote.AUTH_BLOCKED)


NODE = shutil.which("node")
NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fixture = input.fixture || {};
const events = {timers: [], cleared: [], fetches: []};
const timers = [];
const controls = (fixture.controls || []).map(item => ({
    tagName: item.tagName || 'button',
    textContent: item.text || '',
    disabled: Boolean(item.disabled),
    offsetWidth: item.visible === false ? 0 : 100,
    offsetHeight: item.visible === false ? 0 : 20,
    getClientRects: () => item.visible === false ? [] : [{}],
    getAttribute: name => (item.attributes || {})[name] ?? null,
}));
const matches = (node, selector) => selector === node.tagName ||
    (selector === '[role="button"]' && node.getAttribute('role') === 'button');
const sandbox = {
    AbortController,
    document: {
        body: {innerText: fixture.body || ''},
        title: fixture.title || 'Vote for a bot',
        readyState: fixture.readyState || 'complete',
        querySelector: selector => selector.split(',').some(
            part => (fixture.selectors || []).includes(part.trim())
        ) ? {} : null,
        querySelectorAll: selector => controls.filter(node => selector.split(',').some(
            part => matches(node, part.trim())
        )),
    },
    location: {
        protocol: fixture.protocol || 'https:',
        hostname: fixture.hostname || 'top.gg',
    },
    setTimeout: (callback, delay) => {
        timers.push(callback);
        events.timers.push(delay);
        return timers.length;
    },
    clearTimeout: timer => events.cleared.push(timer),
    fetch: async (url, options) => {
        const request = {
            url,
            credentials: options.credentials,
            cache: options.cache,
            hasSignal: options.signal instanceof AbortSignal,
        };
        events.fetches.push(request);
        if (fixture.fetchMode === 'abort') {
            timers[0]();
            request.aborted = options.signal.aborted;
            const error = new Error('cancelled');
            error.name = 'AbortError';
            throw error;
        }
        if (fixture.fetchMode === 'error') throw new TypeError('network unavailable');
        const status = fixture.status ?? 200;
        return {
            ok: status >= 200 && status < 300,
            status,
            headers: {get: name => (fixture.headers || {})[name] || ''},
            json: async () => {
                if (fixture.invalidJson) throw new SyntaxError('invalid JSON');
                return fixture.session || {};
            },
        };
    },
};
(async () => {
    const value = await vm.runInNewContext(input.expression, sandbox, {timeout: 1000});
    process.stdout.write(JSON.stringify({value, events}));
})().catch(error => {
    process.stderr.write(error.stack);
    process.exitCode = 1;
});
"""


@unittest.skipUnless(NODE, "Node.js is needed to execute the browser JavaScript fixtures")
class BrowserJavaScriptRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def capture_expression(self, function):
        with (
            patch("builtins.print"),
            patch("vote.evaluate", new=AsyncMock(return_value=None)) as evaluate,
        ):
            await function(MagicMock())
        return evaluate.await_args.args[1]

    async def execute_expression(self, expression, fixture):
        process = await asyncio.to_thread(
            subprocess.run,
            [NODE, "-e", NODE_HARNESS],
            input=json.dumps({"expression": expression, "fixture": fixture}),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    async def test_full_page_challenge_is_distinct_from_embedded_captcha(self):
        expression = await self.capture_expression(vote.is_cloudflare_challenge_page)
        managed = [
            {"title": "Just a moment..."},
            {"title": "Attention Required! | Cloudflare"},
            {"selectors": ["#challenge-running"]},
            {"selectors": ["#challenge-stage"]},
            {"selectors": ["#challenge-form"]},
            {"body": "Performing security verification"},
            {"body": "top.gg needs to review the security of your connection"},
            {"title": "Just a moment...", "selectors": ['iframe[src*="challenges.cloudflare.com"]']},
        ]
        standalone = [
            {},
            {"body": "Please solve the captcha to continue"},
            {"selectors": ['iframe[src*="challenges.cloudflare.com"]']},
            {"selectors": ['iframe[src*="hcaptcha.com"]']},
            {"selectors": ['iframe[src*="recaptcha"]']},
            {"selectors": [".cf-turnstile"]},
            {"selectors": [".h-captcha"]},
            {"selectors": [".g-recaptcha"]},
        ]
        for expected, fixtures in ((True, managed), (False, standalone)):
            for fixture in fixtures:
                with self.subTest(expected=expected, fixture=fixture):
                    self.assertIs((await self.execute_expression(expression, fixture))["value"], expected)

    async def test_managed_challenge_titles_dom_and_body_are_detected(self):
        expression = await self.capture_expression(vote.is_turnstile_present)
        fixtures = [
            {"title": "Just a moment..."},
            {"title": "Attention Required! | Cloudflare"},
            {"selectors": ["#challenge-running"]},
            {"selectors": ["#challenge-stage"]},
            {"selectors": ["#challenge-form"]},
            {"body": "Performing security verification"},
            {"body": "top.gg needs to review the security of your connection"},
            {"selectors": ['iframe[src*="challenges.cloudflare.com"]']},
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertTrue((await self.execute_expression(expression, fixture))["value"])
        self.assertFalse((await self.execute_expression(expression, {"body": "Welcome to top.gg"}))["value"])

    async def test_vote_surface_on_challenge_loading_or_foreign_page_is_not_authenticated(self):
        expression = await self.capture_expression(vote.topgg_page_auth_hint)
        vote_surface = {"controls": [{"text": "Vote"}]}
        fixtures = [
            {"title": "Just a moment..."},
            {"title": "Attention Required! | Cloudflare"},
            {"selectors": ["#challenge-form"]},
            {"body": "Performing security verification"},
            {"body": "top.gg needs to review the security of your connection"},
            {"readyState": "loading"},
            {"hostname": "top.gg.example.com"},
            {"hostname": "other.top.gg"},
            {"protocol": "http:"},
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                result = (await self.execute_expression(expression, {**vote_surface, **fixture}))["value"]
                self.assertEqual(result, "unknown")
        self.assertEqual((await self.execute_expression(expression, vote_surface))["value"], vote.AUTHENTICATED)
        self.assertEqual((await self.execute_expression(expression, {
            "controls": [{"text": "Vote", "visible": False}],
        }))["value"], "unknown")
        self.assertEqual((await self.execute_expression(expression, {
            "controls": [{"text": "Vote", "disabled": True}],
        }))["value"], "unknown")
        self.assertEqual((await self.execute_expression(expression, {
            "controls": [{"text": "Login"}],
        }))["value"], vote.AUTH_INVALID)

    async def test_session_request_installs_abort_signal_and_clears_timer_on_all_paths(self):
        expression = await self.capture_expression(vote.topgg_session_probe)
        fixtures = [
            {"fetchMode": "abort"},
            {"fetchMode": "error"},
            {"invalidJson": True},
            {"status": 403, "headers": {"cf-mitigated": "challenge", "content-type": "text/html"}},
            {"headers": {"content-type": "application/json"}, "session": {"user": {"id": "private-user-value"}}},
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                output = await self.execute_expression(expression, fixture)
                events = output["events"]
                self.assertEqual(events["timers"], [vote.SESSION_PROBE_TIMEOUT_SEC * 1000])
                self.assertEqual(events["cleared"], [1])
                self.assertEqual(len(events["fetches"]), 1)
                request = events["fetches"][0]
                self.assertEqual(request["url"], "/api/auth/session")
                self.assertTrue(request["hasSignal"])
                self.assertEqual(request["credentials"], "include")
                self.assertEqual(request["cache"], "no-store")
                self.assertNotIn("private-user-value", json.dumps(output))
                if fixture.get("fetchMode") == "abort":
                    self.assertTrue(request["aborted"])
                    self.assertEqual(output["value"]["error"], "fetch:AbortError")
                elif fixture.get("fetchMode") == "error":
                    self.assertEqual(output["value"]["error"], "fetch:TypeError")
                elif fixture.get("invalidJson"):
                    self.assertEqual(output["value"]["error"], "json:SyntaxError")
                elif fixture.get("status") == 403:
                    self.assertFalse(output["value"]["userPresent"])
                    self.assertEqual(output["value"]["cfMitigated"], "challenge")
                else:
                    self.assertTrue(output["value"]["userPresent"])

    async def test_public_vote_navigation_link_does_not_establish_authentication(self):
        expression = await self.capture_expression(vote.topgg_page_auth_hint)
        public_vote_link = {"text": "Vote", "tagName": "a"}
        for login_control in (
            {"text": "Login"},
            {"text": "Login", "tagName": "a"},
        ):
            with self.subTest(login_control=login_control):
                result = await self.execute_expression(expression, {
                    "controls": [public_vote_link, login_control],
                })
                self.assertEqual(result["value"], vote.AUTH_INVALID)
        result = await self.execute_expression(expression, {"controls": [public_vote_link]})
        self.assertEqual(result["value"], "unknown")


if __name__ == "__main__":
    unittest.main()
