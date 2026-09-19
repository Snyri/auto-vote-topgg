import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import vote


class LoaderTests(unittest.TestCase):
    def test_load_tokens_ignores_blank_lines(self):
        with patch.dict(os.environ, {"TOKENS": "first\n\n second \n"}):
            self.assertEqual(vote.load_tokens(), ["first", "second"])

    def test_consume_secret_file_unlinks_and_scrubs_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "secret")
            with open(path, "w", encoding="utf-8") as file:
                file.write("sensitive-value")
            with patch.dict(
                os.environ,
                {"TOKENS": "fallback", "TOKENS_FILE": path},
                clear=False,
            ):
                self.assertEqual(vote.consume_secret("TOKENS"), "sensitive-value")
                self.assertNotIn("TOKENS", os.environ)
                self.assertNotIn("TOKENS_FILE", os.environ)
            self.assertFalse(os.path.exists(path))

    def test_consume_secret_environment_fallback_is_removed(self):
        with patch.dict(os.environ, {"TOKENS": "sensitive-value"}, clear=False):
            self.assertEqual(vote.consume_secret("TOKENS"), "sensitive-value")
            self.assertNotIn("TOKENS", os.environ)

    def test_load_bot_ids_accepts_discord_snowflakes(self):
        with patch.dict(os.environ, {"BOT_IDS": "830530156048285716\n12345678901234567"}):
            self.assertEqual(vote.load_bot_ids(), ["830530156048285716", "12345678901234567"])

    def test_load_bot_ids_rejects_invalid_values(self):
        with patch.dict(os.environ, {"BOT_IDS": "not-a-snowflake"}):
            with self.assertRaisesRegex(ValueError, "expected a 17-20 digit"):
                vote.load_bot_ids()


class UrlMatchingTests(unittest.TestCase):
    def test_matches_topgg_host_and_subdomain(self):
        self.assertTrue(vote.url_has_domain("https://top.gg/bot/123/vote", "top.gg"))
        self.assertTrue(vote.url_has_domain("https://www.top.gg/callback", "top.gg"))

    def test_rejects_topgg_text_outside_hostname(self):
        oauth_url = (
            "https://discord.com/oauth2/authorize?"
            "redirect_uri=https%3A%2F%2Ftop.gg%2Fapi%2Fauth%2Fcallback"
        )
        self.assertFalse(vote.url_has_domain(oauth_url, "top.gg"))
        self.assertFalse(vote.url_has_domain("https://top.gg.evil.example/path", "top.gg"))
        self.assertFalse(vote.url_has_domain("https://example.com/top.gg", "top.gg"))


class ReportingTests(unittest.TestCase):
    def test_account_fingerprint_is_stable_and_hides_token(self):
        token = "sensitive-token-value"
        fingerprint = vote.account_fingerprint(token)
        self.assertEqual(fingerprint, vote.account_fingerprint(token))
        self.assertEqual(len(fingerprint), 8)
        self.assertNotIn(token[:5], fingerprint)

    def test_notification_escapes_dynamic_html(self):
        message = vote.build_notification(
            [[{"account_id": "a&b", "bot_id": "<bot>", "status": "error", "detail": "x < y & z > q"}]],
            "2026-07-28 13:00 WIB",
        )
        self.assertIn("Account a&amp;b", message)
        self.assertIn("&lt;bot&gt;", message)
        self.assertIn("x &lt; y &amp; z &gt; q", message)

    def test_notification_marks_captcha_as_terminal(self):
        message = vote.build_notification(
            [[{"account_id": "account", "bot_id": "123", "status": "captcha_required", "detail": "Manual CAPTCHA"}]],
            "2026-08-07 06:00 WIB",
        )
        self.assertIn("🔒 123: Manual CAPTCHA", message)

    def test_short_report_stays_in_one_chunk(self):
        self.assertEqual(vote.split_telegram_message("line one\nline two", 50), ["line one\nline two"])

    def test_large_report_splits_in_order_under_limit(self):
        message = "\n".join(f"line-{index}-" + "x" * 30 for index in range(20))
        chunks = vote.split_telegram_message(message, 100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual("\n".join(chunks), message)

    @patch("builtins.print")
    @patch("vote.requests.post")
    def test_telegram_report_requires_api_ok(self, post, _print):
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": False}
        post.return_value = response
        with patch.object(vote, "TG_BOT_TOKEN", "bot"), patch.object(vote, "TG_CHAT_ID", "chat"):
            self.assertFalse(vote.send_notification("report"))


class BusinessResultTests(unittest.TestCase):
    def test_success_and_cooldown_are_completed(self):
        results = [[
            {"status": "success"},
            {"status": "cooldown"},
        ]]
        self.assertFalse(vote.has_business_failure(results))

    def test_incomplete_statuses_fail_workflow(self):
        for status in ("error", "auth_failed", "uncertain", "captcha_required"):
            with self.subTest(status=status):
                self.assertTrue(vote.has_business_failure([[{"status": status}]]))

    def test_empty_results_fail_workflow(self):
        self.assertTrue(vote.has_business_failure([]))
        self.assertTrue(vote.has_business_failure([[]]))


class CooldownSchedulingTests(unittest.TestCase):
    def test_parses_supplied_about_one_hour_text(self):
        text = "You have already voted\nYou can vote again in about 1 hour."
        self.assertEqual(vote.parse_cooldown_seconds(text), 3600)

    def test_parses_supported_units_words_case_and_whitespace(self):
        cases = {
            "YOU CAN VOTE AGAIN IN 37 MINUTES.": 2220,
            "You can vote again in one hour.": 3600,
            "You can   vote again in an hour.": 3600,
            "You can vote again in 1 day.": 86400,
            "You can vote again in 90 seconds.": 90,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(vote.parse_cooldown_seconds(text), expected)

    def test_rejects_unrelated_malformed_and_out_of_bounds_durations(self):
        for text in (
            "Ad ends in 5 minutes",
            "You can vote again tomorrow",
            "You can vote again in 0 minutes",
            "You can vote again in -1 hour",
            "You can vote again in 25 hours",
            "You can vote again in 30 seconds",
        ):
            with self.subTest(text=text):
                self.assertIsNone(vote.parse_cooldown_seconds(text))

    def test_retry_timestamp_includes_safety_buffer(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        retry_at = vote.cooldown_retry_at("You can vote again in about 1 hour", now)
        self.assertEqual(
            retry_at,
            int(now.timestamp()) + 3600 + vote.COOLDOWN_SAFETY_BUFFER_SEC,
        )

    def test_earliest_retry_is_selected_across_accounts_and_bots(self):
        results = [
            [
                {"status": "cooldown", "retry_at": 300},
                {"status": "success"},
            ],
            [
                {"status": "cooldown", "retry_at": 200},
                {"status": "cooldown", "retry_at": "bad"},
            ],
        ]
        self.assertEqual(vote.earliest_retry_at(results), 200)

    def test_state_file_contains_only_numeric_timestamp(self):
        results = [[{
            "bot_id": "111",
            "account_id": "private",
            "status": "cooldown",
            "retry_at": 1786334400,
        }]]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "next-vote.json")
            self.assertEqual(vote.write_next_vote_state(results, path), 1786334400)
            with open(path, encoding="utf-8") as file:
                self.assertEqual(json.load(file), {"next_vote_at": 1786334400})

    def test_no_state_file_without_parsed_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "next-vote.json")
            self.assertIsNone(vote.write_next_vote_state([[{"status": "cooldown"}]], path))
            self.assertFalse(os.path.exists(path))

    def test_cooldown_detail_reports_safe_retry_time(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        result = vote.cooldown_result("111", "You can vote again in 1 hour", now)
        self.assertEqual(result["status"], "cooldown")
        self.assertIn("2026-08-10 10:05 WIB", result["detail"])
        message = vote.build_notification([[{"account_id": "id", **result}]], "now")
        self.assertIn("retry after 2026-08-10 10:05 WIB", message)


class BrowserStartupRetryStateTests(unittest.TestCase):
    def browser_error(self, account_id="id"):
        return [{
            "account_id": account_id,
            "bot_id": "all",
            "status": "error",
            "detail": "Browser startup failed: Exception: Failed to connect to browser",
        }]

    def test_detects_account_level_browser_startup_failure_only(self):
        self.assertTrue(vote.is_browser_startup_result(self.browser_error()[0]))
        self.assertFalse(vote.is_browser_startup_result({
            "bot_id": "111",
            "status": "error",
            "detail": "Browser startup failed: Exception",
        }))
        self.assertFalse(vote.is_browser_startup_result({
            "bot_id": "all",
            "status": "error",
            "detail": "TypeError: broken",
        }))

    def test_requests_retry_only_when_all_accounts_failed_browser_startup(self):
        self.assertTrue(vote.should_request_browser_startup_retry([
            self.browser_error("a"),
            self.browser_error("b"),
        ]))
        self.assertFalse(vote.should_request_browser_startup_retry([]))
        self.assertFalse(vote.should_request_browser_startup_retry([
            self.browser_error("a"),
            [{"bot_id": "111", "status": "success"}],
        ]))
        self.assertFalse(vote.should_request_browser_startup_retry([
            [{"bot_id": "all", "status": "captcha_required", "detail": "captcha"}],
        ]))
        self.assertFalse(vote.should_request_browser_startup_retry([
            [{"bot_id": "111", "status": "cooldown"}],
        ]))
        self.assertFalse(vote.should_request_browser_startup_retry([
            [{"bot_id": "all", "status": "error", "detail": "RuntimeError: generic"}],
        ]))

    def test_retry_marker_contains_only_reason_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "browser-startup-retry.json")
            self.assertTrue(vote.write_browser_startup_retry_state([self.browser_error()], path))
            with open(path, encoding="utf-8") as file:
                self.assertEqual(json.load(file), {"reason": "browser_startup_failed"})

    def test_no_retry_marker_for_mixed_or_non_browser_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "browser-startup-retry.json")
            self.assertFalse(vote.write_browser_startup_retry_state([
                self.browser_error("a"),
                [{"bot_id": "111", "status": "success"}],
            ], path))
            self.assertFalse(os.path.exists(path))


class CooldownVotePageTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.evaluate", new_callable=AsyncMock, return_value="Voting for bot")
    @patch("vote.body_text", new_callable=AsyncMock)
    async def test_vote_page_cooldown_returns_retry_metadata(
        self, body_text, _evaluate, _sleep, _print
    ):
        body_text.return_value = (
            "You have already voted\nYou can vote again in about 1 hour."
        )
        tab = AsyncMock()

        result = await vote.vote_for_bot(tab, "111")

        self.assertEqual(result["status"], "cooldown")
        self.assertIsInstance(result.get("retry_at"), int)
        tab.get.assert_awaited_once_with("https://top.gg/bot/111/vote")


class PostVoteTurnstileTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.solve_turnstile", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_present", new_callable=AsyncMock)
    @patch("vote._click_marked", new_callable=AsyncMock, return_value=True)
    @patch("vote.mark_vote_button", new_callable=AsyncMock, return_value={"found": True, "disabled": False})
    @patch("vote.wait_for_ad", new_callable=AsyncMock, return_value=None)
    @patch("vote.evaluate", new_callable=AsyncMock, return_value="Voting for bot")
    @patch("vote.body_text", new_callable=AsyncMock)
    async def test_solves_turnstile_after_clicking_vote(
        self, body_text, _evaluate, _ad, _mark, _click, present, solver, _sleep, _print
    ):
        body_text.side_effect = [
            "ready to vote",
            "Please solve the captcha to continue",
            "Thanks for voting",
        ]
        present.side_effect = [False, False, True]
        tab = AsyncMock()

        result = await vote.vote_for_bot(tab, "111", "account")

        self.assertEqual(result["status"], "success")
        solver.assert_awaited_once_with(tab)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.browser_screenshot", new_callable=AsyncMock, return_value="screenshots/vote_account_111_captcha.png")
    @patch("vote.solve_turnstile", new_callable=AsyncMock, return_value=False)
    @patch("vote.is_turnstile_present", new_callable=AsyncMock)
    @patch("vote._click_marked", new_callable=AsyncMock, return_value=True)
    @patch("vote.mark_vote_button", new_callable=AsyncMock, return_value={"found": True, "disabled": False})
    @patch("vote.wait_for_ad", new_callable=AsyncMock, return_value=None)
    @patch("vote.evaluate", new_callable=AsyncMock, return_value="Voting for bot")
    @patch("vote.body_text", new_callable=AsyncMock)
    async def test_reports_captcha_only_after_post_vote_solver_fails(
        self, body_text, _evaluate, _ad, _mark, _click, present, solver, screenshot, _sleep, _print
    ):
        body_text.side_effect = [
            "ready to vote",
            "Please solve the captcha to continue",
        ]
        present.side_effect = [False, False, True]
        tab = AsyncMock()

        result = await vote.vote_for_bot(tab, "111", "account")

        self.assertEqual(result["status"], "captcha_required")
        self.assertIn("after solver attempt", result["detail"])
        solver.assert_awaited_once_with(tab)
        screenshot.assert_awaited_once_with(
            tab,
            "screenshots/vote_account_111_captcha.png",
            required=True,
        )

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.solve_turnstile", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_present", new_callable=AsyncMock)
    @patch("vote._click_marked", new_callable=AsyncMock, return_value=True)
    @patch("vote.mark_vote_button", new_callable=AsyncMock, return_value={"found": True, "disabled": False})
    @patch("vote.wait_for_ad", new_callable=AsyncMock, return_value=None)
    @patch("vote.evaluate", new_callable=AsyncMock, return_value="Voting for bot")
    @patch("vote.body_text", new_callable=AsyncMock)
    async def test_solves_turnstile_after_vote_verification_reload(
        self, body_text, _evaluate, _ad, _mark, _click, present, solver, _sleep, _print
    ):
        body_text.side_effect = [
            "ready to vote",
            "vote result unclear",
            "Please solve the captcha to continue",
            "You have already voted",
        ]
        present.side_effect = [False, False, False, True]
        tab = AsyncMock()

        result = await vote.vote_for_bot(tab, "111", "account")

        self.assertEqual(result["status"], "success")
        solver.assert_awaited_once_with(tab)
        tab.reload.assert_awaited_once()


class MainExitTests(unittest.IsolatedAsyncioTestCase):
    @patch("vote.consume_secret")
    @patch("builtins.print")
    @patch("vote.send_notification")
    @patch("vote.process_account", new_callable=AsyncMock)
    @patch("vote.load_topgg_cookies", return_value=[])
    @patch("vote.load_bot_ids", return_value=["111"])
    @patch("vote.load_tokens", return_value=["token"])
    async def test_main_notifies_before_returning_failure(
        self, _tokens, _bots, _cookies, process_account, send_notification, _print,
        consume_secret,
    ):
        consume_secret.side_effect = ["token", "", "", ""]
        process_account.return_value = [
            {"account_id": "id", "bot_id": "111", "status": "captcha_required", "detail": "captcha"}
        ]

        exit_code = await vote.main()

        self.assertEqual(exit_code, 1)
        send_notification.assert_called_once()


class RetryPolicyTests(unittest.TestCase):
    def test_final_statuses_are_not_retryable(self):
        for status in ("success", "cooldown"):
            with self.subTest(status=status):
                self.assertFalse(vote.is_retryable_result({"status": status}))

    def test_transient_statuses_are_retryable(self):
        for status in ("error", "auth_failed", "uncertain"):
            with self.subTest(status=status):
                self.assertTrue(vote.is_retryable_result({"status": status}))

    def test_retryable_bot_ids_excludes_final_and_account_results(self):
        results = [
            {"bot_id": "111", "status": "error"},
            {"bot_id": "222", "status": "success"},
            {"bot_id": "333", "status": "uncertain"},
            {"bot_id": "all", "status": "auth_failed"},
        ]
        self.assertEqual(vote.retryable_bot_ids(results), ["111", "333"])

    def test_captcha_required_is_terminal(self):
        result = {"bot_id": "111", "status": "captcha_required"}
        self.assertFalse(vote.is_retryable_result(result))
        self.assertEqual(vote.retryable_bot_ids([result]), [])


class CookieLoaderTests(unittest.TestCase):
    def test_cookie_lines_match_accounts_and_filter_non_authjs(self):
        first = [
            {
                "domain": ".top.gg",
                "name": "__Secure-authjs.session-token",
                "value": "one",
                "sameSite": "lax",
                "secure": True,
                "httpOnly": True,
            },
            {"domain": ".top.gg", "name": "_ga", "value": "tracking"},
        ]
        second = [
            {
                "domain": "top.gg",
                "name": "__Host-authjs.csrf-token",
                "value": "two",
                "sameSite": "no_restriction",
                "secure": True,
                "httpOnly": False,
            },
        ]
        raw = f"{json.dumps(first)}\n[]\n{json.dumps(second)}"
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": raw}):
            cookies = vote.load_topgg_cookies()
        self.assertEqual(len(cookies), 3)
        self.assertEqual([cookie["name"] for cookie in cookies[0]], ["__Secure-authjs.session-token"])
        self.assertEqual(cookies[0][0]["sameSite"], "Lax")
        self.assertEqual(cookies[1], [])
        self.assertEqual(cookies[2][0]["sameSite"], "None")

    def test_empty_cookie_secret_returns_empty_list(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": ""}):
            self.assertEqual(vote.load_topgg_cookies(), [])

    def test_invalid_cookie_line_reports_account_number(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": "[]\nnot-json"}):
            with self.assertRaisesRegex(ValueError, "line 2"):
                vote.load_topgg_cookies()

    def test_cookie_line_count_must_match_tokens(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": "[]"}):
            with self.assertRaisesRegex(ValueError, "line count must match TOKENS"):
                vote.load_topgg_cookies(2)

    def test_blank_cookie_account_requires_placeholder(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": "[]\n\n[]"}):
            with self.assertRaisesRegex(ValueError, "line 2 is blank"):
                vote.load_topgg_cookies(3)

    def test_cookie_items_must_be_objects(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": '[null]'}):
            with self.assertRaisesRegex(ValueError, "item 1 must be an object"):
                vote.load_topgg_cookies(1)

    def test_cookie_validation_rejects_invalid_expiry_without_value_leak(self):
        secret_value = "never-print-this-cookie"
        cookies = [{
            "domain": ".top.gg",
            "name": "__Secure-authjs.session-token",
            "value": secret_value,
            "secure": True,
            "httpOnly": True,
            "expirationDate": "tomorrow",
        }]
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": json.dumps(cookies)}):
            with self.assertRaises(ValueError) as context:
                vote.load_topgg_cookies(1)
        self.assertIn("invalid expiry", str(context.exception))
        self.assertNotIn(secret_value, str(context.exception))

    def test_authjs_cookie_is_always_injected_secure(self):
        for secure in (None, False, True):
            cookie = {
                "domain": ".top.gg",
                "name": "authjs.csrf-token",
                "value": "secret",
                "httpOnly": False,
            }
            if secure is not None:
                cookie["secure"] = secure
            with self.subTest(secure=secure):
                normalized = vote.load_topgg_cookies(1, json.dumps([cookie]))
                self.assertTrue(normalized[0][0]["secure"])

    def test_session_cookie_is_always_injected_httponly(self):
        for http_only in (None, False, True):
            cookie = {
                "domain": ".top.gg",
                "name": "__Secure-authjs.session-token",
                "value": "secret",
                "secure": True,
            }
            if http_only is not None:
                cookie["httpOnly"] = http_only
            with self.subTest(httpOnly=http_only):
                normalized = vote.load_topgg_cookies(1, json.dumps([cookie]))
                self.assertTrue(normalized[0][0]["httpOnly"])

    def test_cookie_security_flags_reject_invalid_types_without_value_leak(self):
        secret_value = "never-print-this-cookie"
        cookie = {
            "domain": ".top.gg",
            "name": "__Secure-authjs.session-token",
            "value": secret_value,
            "secure": "yes",
            "httpOnly": True,
        }
        with self.assertRaises(ValueError) as context:
            vote.load_topgg_cookies(1, json.dumps([cookie]))
        self.assertIn("invalid secure", str(context.exception))
        self.assertNotIn(secret_value, str(context.exception))

    def test_wrong_domain_export_is_rejected(self):
        cookies = [{
            "domain": ".example.com",
            "name": "__Secure-authjs.session-token",
            "value": "secret",
        }]
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": json.dumps(cookies)}):
            with self.assertRaisesRegex(ValueError, "contains no top.gg Auth.js cookies"):
                vote.load_topgg_cookies(1)


class RetryOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_only_transient_bots_are_retried(self, run_account, _sleep, _print):
        run_account.side_effect = [
            [
                {"bot_id": "111", "status": "success", "detail": "ok", "account_id": "id"},
                {"bot_id": "222", "status": "error", "detail": "temporary", "account_id": "id"},
            ],
            [{"bot_id": "222", "status": "cooldown", "detail": "done", "account_id": "id"}],
        ]

        results = await vote.process_account("token", ["111", "222"], 1, 1)

        self.assertEqual([result["status"] for result in results], ["success", "cooldown"])
        self.assertEqual(run_account.await_args_list[0].args[1], ["111", "222"])
        self.assertEqual(run_account.await_args_list[1].args[1], ["222"])

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_auth_failure_retries_account(self, run_account, _sleep, _print):
        run_account.side_effect = [
            [{"bot_id": "all", "status": "auth_failed", "detail": "auth", "account_id": "id"}],
            [{"bot_id": "111", "status": "success", "detail": "ok", "account_id": "id"}],
        ]

        results = await vote.process_account("token", ["111"], 1, 1)

        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(run_account.await_count, 2)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_auth_failure_screenshot_capture_only_on_final_attempt(
        self, run_account, _sleep, _print
    ):
        run_account.return_value = [
            {"bot_id": "all", "status": "auth_failed", "detail": "auth", "account_id": "id"}
        ]

        results = await vote.process_account("token", ["111"], 1, 1)

        self.assertEqual(results[0]["status"], "auth_failed")
        self.assertEqual(run_account.await_count, vote.MAX_RETRIES)
        self.assertEqual(
            [call.kwargs["capture_auth_failure"] for call in run_account.await_args_list],
            [False] * (vote.MAX_RETRIES - 1) + [True],
        )
    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_auth_captcha_is_not_retried(self, run_account, _sleep, _print):
        run_account.return_value = [
            {"bot_id": "all", "status": "captcha_required", "detail": "captcha", "account_id": "id"}
        ]

        results = await vote.process_account("token", ["111"], 1, 1)

        self.assertEqual(results[0]["status"], "captcha_required")
        self.assertEqual(run_account.await_count, 1)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_browser_startup_error_is_not_multiplied_by_account_retry(
        self, run_account, _sleep, _print
    ):
        run_account.side_effect = vote.BrowserStartupError("failed")

        results = await vote.process_account("token", ["111"], 1, 1)

        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(run_account.await_count, 1)


class AuthenticationStateTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.evaluate", new_callable=AsyncMock)
    async def test_session_probe_reports_authenticated_json_session(self, evaluate, _print):
        evaluate.return_value = {
            "ok": True,
            "status": 200,
            "contentType": "application/json",
            "jsonOk": True,
            "userPresent": True,
            "error": None,
        }

        self.assertTrue(await vote.is_topgg_authenticated(AsyncMock()))
        rendered = " ".join(str(arg) for call in _print.call_args_list for arg in call.args)
        self.assertIn("HTTP 200", rendered)
        self.assertIn("session-user=present", rendered)

    @patch("builtins.print")
    @patch("vote.evaluate", new_callable=AsyncMock)
    async def test_session_probe_reports_http_failure_without_exposing_body(self, evaluate, _print):
        evaluate.return_value = {
            "ok": False,
            "status": 403,
            "contentType": "text/html",
            "jsonOk": False,
            "userPresent": False,
            "error": None,
        }

        self.assertFalse(await vote.is_topgg_authenticated(AsyncMock()))
        rendered = " ".join(str(arg) for call in _print.call_args_list for arg in call.args)
        self.assertIn("HTTP 403", rendered)
        self.assertIn("content-type=text/html", rendered)
        self.assertNotIn("cookie", rendered.lower())
        self.assertNotIn("token", rendered.lower())

    @patch("builtins.print")
    @patch("vote.evaluate", new_callable=AsyncMock)
    async def test_session_probe_reports_user_absent_separately_from_http_failure(self, evaluate, _print):
        evaluate.return_value = {
            "ok": True,
            "status": 200,
            "contentType": "application/json",
            "jsonOk": True,
            "userPresent": False,
            "error": None,
        }

        self.assertFalse(await vote.is_topgg_authenticated(AsyncMock()))
        rendered = " ".join(str(arg) for call in _print.call_args_list for arg in call.args)
        self.assertIn("HTTP 200", rendered)
        self.assertIn("session-user=absent", rendered)

    @patch("builtins.print")
    @patch("vote.evaluate", new_callable=AsyncMock)
    async def test_session_probe_reports_fetch_exception_category_only(self, evaluate, _print):
        evaluate.return_value = {
            "ok": False,
            "status": 0,
            "contentType": "",
            "jsonOk": False,
            "userPresent": False,
            "error": "fetch:TypeError",
        }

        self.assertFalse(await vote.is_topgg_authenticated(AsyncMock()))
        rendered = " ".join(str(arg) for call in _print.call_args_list for arg in call.args)
        self.assertIn("HTTP 0", rendered)
        self.assertIn("error=fetch:TypeError", rendered)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.topgg_auth_state_details", new_callable=AsyncMock)
    @patch("vote.inject_topgg_cookies", new_callable=AsyncMock)
    async def test_cookie_auth_propagates_captcha(self, _inject, auth_state, _sleep, _print):
        auth_state.return_value = (vote.AUTH_CAPTCHA_REQUIRED, 403, "text/html")
        tab = AsyncMock()

        result = await vote.login_with_cookies(tab, [{"name": "authjs"}], ["111"])

        self.assertEqual(result, vote.AUTH_CAPTCHA_REQUIRED)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.topgg_auth_state_details", new_callable=AsyncMock)
    @patch("vote.inject_topgg_cookies", new_callable=AsyncMock)
    async def test_cookie_auth_marks_four_persistent_403_checks_for_fresh_browser_retry(
        self, _inject, auth_state, _sleep, _print
    ):
        auth_state.return_value = (vote.AUTH_INVALID, 403, "text/html")
        tab = AsyncMock()

        result = await vote.login_with_cookies(tab, [{"name": "authjs"}], ["111"])

        self.assertEqual(result, vote.AUTH_CLOUDFLARE_BLOCKED)
        self.assertEqual(auth_state.await_count, 4)

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.topgg_auth_state_details", new_callable=AsyncMock)
    @patch("vote.inject_topgg_cookies", new_callable=AsyncMock)
    async def test_cookie_auth_does_not_classify_json_403_as_cloudflare_html_block(
        self, _inject, auth_state, _sleep, _print
    ):
        auth_state.return_value = (vote.AUTH_INVALID, 403, "application/json")
        tab = AsyncMock()

        result = await vote.login_with_cookies(tab, [{"name": "authjs"}], ["111"])

        self.assertEqual(result, vote.AUTH_INVALID)
        self.assertEqual(auth_state.await_count, 4)

    @patch("builtins.print")
    @patch("vote.discord_oauth_login", new_callable=AsyncMock)
    @patch("vote.login_with_cookies", new_callable=AsyncMock)
    @patch("vote.start_browser", new_callable=AsyncMock)
    async def test_persistent_cloudflare_block_skips_oauth_and_returns_retryable_auth_failure(
        self, start_browser, cookie_login, oauth_login, _print
    ):
        browser = MagicMock()
        tab = AsyncMock()
        browser.__iter__.return_value = iter([tab])
        browser.cookies.clear = AsyncMock()
        browser.aclose = AsyncMock()
        start_browser.return_value = browser
        cookie_login.return_value = vote.AUTH_CLOUDFLARE_BLOCKED

        results = await vote._run_account("token", ["111"], "id", [{"name": "authjs"}])

        self.assertEqual(results[0]["status"], "auth_failed")
        self.assertIn("Cloudflare HTTP 403", results[0]["detail"])
        oauth_login.assert_not_awaited()
        browser.cookies.clear.assert_not_awaited()
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()

    @patch("builtins.print")
    @patch("vote.vote_for_bot", new_callable=AsyncMock)
    @patch("vote.discord_oauth_login", new_callable=AsyncMock)
    @patch("vote.login_with_cookies", new_callable=AsyncMock)
    @patch("vote.start_browser", new_callable=AsyncMock)
    async def test_invalid_cookie_falls_back_to_oauth(
        self, start_browser, cookie_login, oauth_login, vote_for_bot, _print
    ):
        browser = MagicMock()
        browser.__iter__.return_value = iter([AsyncMock()])
        browser.cookies.clear = AsyncMock()
        browser.aclose = AsyncMock()
        start_browser.return_value = browser
        cookie_login.return_value = vote.AUTH_INVALID
        oauth_login.return_value = vote.AUTHENTICATED
        vote_for_bot.return_value = {"bot_id": "111", "status": "success", "detail": "ok"}

        results = await vote._run_account("token", ["111"], "id", [{"name": "authjs"}])

        self.assertEqual(results[0]["status"], "success")
        oauth_login.assert_awaited_once()
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()

    @patch("builtins.print")
    @patch("vote.browser_screenshot", new_callable=AsyncMock)
    @patch("vote.discord_oauth_login", new_callable=AsyncMock)
    @patch("vote.login_with_cookies", new_callable=AsyncMock)
    @patch("vote.start_browser", new_callable=AsyncMock)
    async def test_auth_captcha_captures_before_browser_cleanup(
        self, start_browser, cookie_login, oauth_login, screenshot, _print
    ):
        browser = MagicMock()
        tab = AsyncMock()
        browser.__iter__.return_value = iter([tab])
        browser.aclose = AsyncMock()
        start_browser.return_value = browser
        cookie_login.return_value = vote.AUTH_CAPTCHA_REQUIRED
        screenshot.return_value = "screenshots/auth_id_captcha.png"

        results = await vote._run_account("token", ["111"], "id", [{"name": "authjs"}])

        self.assertEqual(results[0]["status"], "captcha_required")
        self.assertEqual(results[0]["screenshot_path"], "screenshots/auth_id_captcha.png")
        screenshot.assert_awaited_once_with(
            tab, "screenshots/auth_id_captcha.png", required=True
        )
        oauth_login.assert_not_awaited()
        browser.aclose.assert_awaited_once()
    @patch("builtins.print")
    @patch("vote.browser_screenshot", new_callable=AsyncMock)
    @patch("vote.discord_oauth_login", new_callable=AsyncMock)
    @patch("vote.start_browser", new_callable=AsyncMock)
    async def test_auth_failure_captures_screenshot_when_requested(
        self, start_browser, oauth_login, screenshot, _print
    ):
        browser = MagicMock()
        tab = AsyncMock()
        browser.__iter__.return_value = iter([tab])
        browser.aclose = AsyncMock()
        start_browser.return_value = browser
        oauth_login.return_value = vote.AUTH_INVALID
        screenshot.return_value = "screenshots/auth_id_failed.png"

        results = await vote._run_account(
            "token", ["111"], "id", capture_auth_failure=True
        )

        self.assertEqual(results[0]["status"], "auth_failed")
        self.assertEqual(results[0]["screenshot_path"], "screenshots/auth_id_failed.png")
        screenshot.assert_awaited_once_with(
            tab, "screenshots/auth_id_failed.png", required=True
        )
        browser.aclose.assert_awaited_once()


class PrivacyOverlayTests(unittest.IsolatedAsyncioTestCase):
    @patch("vote.evaluate", new_callable=AsyncMock, return_value={"present": True, "dismissed": True})
    async def test_privacy_overlay_dismiss_clicks_detected_consent(self, evaluate_mock):
        tab = AsyncMock()

        self.assertTrue(await vote.dismiss_privacy_overlay(tab))
        expression = evaluate_mock.call_args.args[1]
        self.assertIn("we value your privacy", expression)
        self.assertIn("#accept-btn", expression)
        self.assertIn("textContent", expression)
        self.assertIn("agree", expression)

    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock, side_effect=[False, True, False])
    async def test_settle_privacy_overlay_retries_after_page_open(self, dismiss, sleep):
        tab = AsyncMock()

        self.assertTrue(await vote.settle_privacy_overlay(tab, attempts=3, delay=0.25))
        self.assertEqual(dismiss.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        sleep.assert_awaited_with(0.25)

    @patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock)
    async def test_marked_click_dismisses_privacy_overlay_first(self, dismiss):
        element = AsyncMock()
        tab = AsyncMock()
        tab.select.return_value = element

        self.assertTrue(await vote._click_marked(tab, "data-auto-vote"))
        dismiss.assert_awaited_once_with(tab)
        tab.select.assert_awaited_once_with('[data-auto-vote="1"]', timeout=2)
        element.scroll_into_view.assert_awaited_once()
        element.click.assert_awaited_once()

    @patch("builtins.print")
    @patch("vote.send_telegram_photo", return_value=True)
    @patch("vote.browser_screenshot", new_callable=AsyncMock, return_value="screenshots/privacy_overlay_dismiss_failed.png")
    @patch("vote.evaluate", new_callable=AsyncMock, return_value={
        "present": True,
        "dismissed": False,
        "reason": "consent_button_not_found",
    })
    async def test_privacy_overlay_dismiss_failure_sends_screenshot(
        self, _evaluate, screenshot, send_photo, _print
    ):
        tab = AsyncMock()
        with (
            patch.object(vote, "TG_BOT_TOKEN", "token"),
            patch.object(vote, "TG_CHAT_ID", "chat"),
            patch.object(vote, "PRIVACY_DISMISS_REPORTED", False),
        ):
            self.assertFalse(await vote.dismiss_privacy_overlay(tab))

        screenshot.assert_awaited_once_with(
            tab,
            "screenshots/privacy_overlay_dismiss_failed.png",
            required=True,
        )
        send_photo.assert_called_once()
        self.assertIn("consent_button_not_found", send_photo.call_args.args[1])

    @patch("builtins.print")
    @patch("vote.dismiss_privacy_overlay", new_callable=AsyncMock)
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, side_effect=[False, True])
    async def test_solver_dismisses_privacy_overlay_before_click(
        self, _solved, _present, dismiss, _print
    ):
        tab = AsyncMock()

        self.assertTrue(await vote.solve_turnstile(tab))
        dismiss.assert_awaited_once_with(tab)
        tab.verify_cf.assert_awaited_once_with()


class TurnstileSolverTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, return_value=False)
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, return_value=False)
    async def test_no_challenge_succeeds_without_click(self, _solved, _present, _print):
        tab = AsyncMock()

        self.assertTrue(await vote.solve_turnstile(tab))
        tab.verify_cf.assert_not_awaited()

    @patch("builtins.print")
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, side_effect=[False, True])
    async def test_native_click_then_response_token_succeeds(self, _solved, _present, _print):
        tab = AsyncMock()

        self.assertTrue(await vote.solve_turnstile(tab))
        tab.verify_cf.assert_awaited_once_with()

    @patch("builtins.print")
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, side_effect=[True, False])
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, return_value=False)
    async def test_native_click_then_page_clearance_succeeds(self, _solved, _present, _print):
        tab = AsyncMock()

        self.assertTrue(await vote.solve_turnstile(tab))
        tab.verify_cf.assert_awaited_once_with()

    @patch("builtins.print")
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, return_value=False)
    async def test_click_exception_fails_immediately(self, _solved, _present, _print):
        tab = AsyncMock()
        tab.verify_cf.side_effect = TypeError("missing template coordinates")

        self.assertFalse(await vote.solve_turnstile(tab))
        tab.verify_cf.assert_awaited_once_with()

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote.is_turnstile_present", new_callable=AsyncMock, return_value=True)
    @patch("vote.is_turnstile_solved", new_callable=AsyncMock, return_value=False)
    async def test_persistent_challenge_times_out(
        self, _solved, _present, _sleep, _print
    ):
        tab = AsyncMock()
        times = iter([0.0, 0.0, 31.0])
        loop = MagicMock()
        loop.time.side_effect = lambda: next(times)

        with patch("vote.asyncio.get_running_loop", return_value=loop):
            self.assertFalse(await vote.solve_turnstile(tab))
        tab.verify_cf.assert_awaited_once_with()


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch.object(vote, "BROWSER_START_RETRIES", 1)
    @patch("vote.uc.Browser")
    @patch("vote.uc.Config")
    async def test_partial_start_failure_is_cleaned(
        self, config_class, browser_class, _sleep
    ):
        browser = browser_class.return_value
        browser.start = AsyncMock(side_effect=RuntimeError("connect failed"))
        browser.aclose = AsyncMock()
        browser._process = None
        with patch.dict(
            os.environ,
            {
                "TOKENS": "token",
                "TOPGG_COOKIES_JSON": "cookies",
                "TG_BOT_TOKEN": "telegram",
                "TG_CHAT_ID": "chat",
            },
            clear=False,
        ):
            with self.assertRaises(vote.BrowserStartupError):
                await vote.start_browser()
            for name in ("TOKENS", "TOPGG_COOKIES_JSON", "TG_BOT_TOKEN", "TG_CHAT_ID"):
                self.assertNotIn(name, os.environ)

        profile_path = config_class.call_args.kwargs["user_data_dir"]
        self.assertFalse(os.path.exists(profile_path))
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()

    async def test_chrome_process_diagnostics_redacts_and_truncates(self):
        class Stream:
            async def read(self, _limit):
                return ("secret " + "x" * vote.DIAGNOSTIC_DETAIL_LIMIT).encode()

        process = MagicMock(returncode=9, stderr=Stream(), stdout=None)
        with patch.object(vote, "SENSITIVE_VALUES", ["secret"]):
            detail = await vote.chrome_process_diagnostics(process)

        self.assertIn("exit=9", detail)
        self.assertIn("***", detail)
        self.assertNotIn("secret", detail)
        self.assertLessEqual(len(detail), vote.DIAGNOSTIC_DETAIL_LIMIT)

    async def test_close_browser_deletes_explicit_profile(self):
        browser = MagicMock()
        browser.aclose = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            profile_path = os.path.join(directory, "profile")
            os.mkdir(profile_path)
            browser._security_profile_path = profile_path
            await vote.close_browser(browser)
            self.assertFalse(os.path.exists(profile_path))
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()


class FullOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    async def test_cookie_fallback_captcha_reports_failure_without_retry(self, _print):
        browser = MagicMock()
        browser.__iter__.return_value = iter([AsyncMock()])
        browser.cookies.clear = AsyncMock()
        browser.aclose = AsyncMock()
        account_cookies = [[{"name": "__Secure-authjs.session-token"}]]
        captcha = {
            "bot_id": "111",
            "status": "captcha_required",
            "detail": "CAPTCHA appeared after clicking Vote",
        }

        with (
            patch("vote.consume_secret", side_effect=["token", "cookies", "", ""]),
            patch("vote.load_tokens", return_value=["token"]),
            patch("vote.load_bot_ids", return_value=["111"]),
            patch("vote.load_topgg_cookies", return_value=account_cookies),
            patch("vote.start_browser", new=AsyncMock(return_value=browser)),
            patch("vote.login_with_cookies", new=AsyncMock(return_value=vote.AUTH_INVALID)) as cookie_login,
            patch("vote.discord_oauth_login", new=AsyncMock(return_value=vote.AUTHENTICATED)) as oauth_login,
            patch("vote.vote_for_bot", new=AsyncMock(return_value=captcha)) as vote_for_bot,
            patch("vote.send_notification") as send_notification,
            patch("vote.asyncio.sleep", new=AsyncMock()),
        ):
            exit_code = await vote.main()

        self.assertEqual(exit_code, 1)
        cookie_login.assert_awaited_once()
        oauth_login.assert_awaited_once()
        vote_for_bot.assert_awaited_once_with(unittest.mock.ANY, "111", unittest.mock.ANY)
        send_notification.assert_called_once()
        self.assertIn("CAPTCHA appeared after clicking Vote", send_notification.call_args.args[0])
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()

    @patch("builtins.print")
    async def test_main_sends_report_before_captcha_photo(self, _print):
        events = []
        captcha = {
            "account_id": "safe-id",
            "bot_id": "111",
            "status": "captcha_required",
            "detail": "CAPTCHA <blocked>",
            "screenshot_path": "screenshots/captcha.png",
        }

        def record_report(_message):
            events.append("report")
            return True

        async def record_photos(results):
            self.assertEqual(results, [[captcha]])
            events.append("photo")
            return 1

        with (
            patch("vote.consume_secret", side_effect=["token", "", "telegram", "chat"]),
            patch("vote.load_tokens", return_value=["token"]),
            patch("vote.load_bot_ids", return_value=["111"]),
            patch("vote.load_topgg_cookies", return_value=[]),
            patch("vote.process_account", new=AsyncMock(return_value=[captcha])),
            patch("vote.send_notification", side_effect=record_report),
            patch("vote.send_captcha_screenshots", new=AsyncMock(side_effect=record_photos)),
        ):
            exit_code = await vote.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, ["report", "photo"])

    @patch("builtins.print")
    @patch("vote.browser_screenshot", new_callable=AsyncMock, return_value=None)
    async def test_captcha_capture_failure_preserves_result(self, screenshot, _print):
        result = await vote.captcha_result(AsyncMock(), "111", "CAPTCHA required")

        self.assertEqual(result, {
            "bot_id": "111",
            "status": "captcha_required",
            "detail": "CAPTCHA required",
        })
        screenshot.assert_awaited_once_with(
            unittest.mock.ANY,
            "screenshots/vote_unknown_111_captcha.png",
            required=True,
        )


class ScreenshotPrivacyTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(vote, "SEND_ERROR_SCREENSHOTS", False)
    async def test_error_screenshot_is_disabled_by_default(self):
        tab = AsyncMock()
        self.assertIsNone(await vote.error_screenshot(tab, "screenshots/error.png"))
        tab.save_screenshot.assert_not_awaited()

    @patch.object(vote, "SEND_ERROR_SCREENSHOTS", False)
    async def test_captcha_screenshot_bypasses_optional_flag(self):
        tab = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "captcha.png")
            self.assertEqual(
                await vote.browser_screenshot(tab, path, required=True),
                path,
            )
        tab.save_screenshot.assert_awaited_once_with(filename=path, format="png")

    @patch.object(vote, "SEND_ERROR_SCREENSHOTS", True)
    async def test_error_screenshot_runs_when_opted_in(self):
        tab = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "error.png")
            self.assertEqual(await vote.error_screenshot(tab, path), path)
        tab.save_screenshot.assert_awaited_once_with(filename=path, format="png")

    @patch("builtins.print")
    @patch("vote.requests.post")
    def test_telegram_photo_requires_api_ok(self, post, _print):
        response = MagicMock(status_code=200)
        response.json.return_value = {"ok": False}
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "captcha.png")
            with open(path, "wb") as file:
                file.write(b"png")
            with patch.object(vote, "TG_BOT_TOKEN", "bot"), patch.object(vote, "TG_CHAT_ID", "chat"):
                self.assertFalse(vote.send_telegram_photo(path, "caption"))

    @patch("builtins.print")
    @patch("vote.send_telegram_photo", return_value=True)
    async def test_captcha_photos_are_deduplicated_escaped_and_deleted(self, send_photo, _print):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "captcha.png")
            with open(path, "wb") as file:
                file.write(b"png")
            result = {
                "account_id": "a&b",
                "bot_id": "<all>",
                "status": "captcha_required",
                "detail": "CAPTCHA <blocked>",
                "screenshot_path": path,
            }

            sent = await vote.send_captcha_screenshots([[result, dict(result)]])

            self.assertEqual(sent, 1)
            self.assertFalse(os.path.exists(path))
        send_photo.assert_called_once()
        caption = send_photo.call_args.args[1]
        self.assertIn("Account a&amp;b", caption)
        self.assertIn("&lt;all&gt;", caption)
        self.assertIn("CAPTCHA &lt;blocked&gt;", caption)

    @patch("builtins.print")
    @patch("vote.send_telegram_photo", return_value=False)
    async def test_captcha_photo_failure_still_deletes_local_file(self, send_photo, _print):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "captcha.png")
            with open(path, "wb") as file:
                file.write(b"png")
            result = {
                "status": "captcha_required",
                "screenshot_path": path,
            }

            self.assertEqual(await vote.send_captcha_screenshots([[result]]), 0)
            self.assertFalse(os.path.exists(path))
        send_photo.assert_called_once()

    @patch("builtins.print")
    @patch("vote.send_telegram_photo", return_value=True)
    async def test_captcha_detail_under_generic_error_sends_screenshot(self, send_photo, _print):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "captcha.png")
            with open(path, "wb") as file:
                file.write(b"png")
            result = {
                "status": "error",
                "detail": "Unexpected CAPTCHA provider error",
                "screenshot_path": path,
            }

            self.assertTrue(vote.is_captcha_related_result(result))
            self.assertEqual(await vote.send_captcha_screenshots([[result]]), 1)
            self.assertFalse(os.path.exists(path))
        send_photo.assert_called_once()

    @patch("builtins.print")
    @patch("vote.send_telegram_photo", return_value=True)
    async def test_auth_failure_photos_are_deduplicated_escaped_and_deleted(self, send_photo, _print):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "auth.png")
            with open(path, "wb") as file:
                file.write(b"png")
            result = {
                "account_id": "a&b",
                "bot_id": "all",
                "status": "auth_failed",
                "detail": "Top.gg authentication <failed>",
                "screenshot_path": path,
            }

            sent = await vote.send_auth_failure_screenshots([[result, dict(result)]])

            self.assertEqual(sent, 1)
            self.assertFalse(os.path.exists(path))
        send_photo.assert_called_once()
        caption = send_photo.call_args.args[1]
        self.assertIn("Account a&amp;b", caption)
        self.assertIn("Top.gg authentication &lt;failed&gt;", caption)


if __name__ == "__main__":
    unittest.main()
