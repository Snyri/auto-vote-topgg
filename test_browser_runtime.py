"""Regression coverage for the active AniGame browser and recovery runtime."""


import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import browser_runtime as vote


class LoaderTests(unittest.TestCase):

    def test_load_tokens_ignores_blank_lines(self):
        with patch.dict(os.environ, {'TOKENS': 'first\n\n second \n'}):
            self.assertEqual(vote.load_tokens(), ['first', 'second'])

    def test_consume_secret_file_unlinks_and_scrubs_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'secret')
            with open(path, 'w', encoding='utf-8') as file:
                file.write('sensitive-value')
            with patch.dict(os.environ, {'TOKENS': 'fallback', 'TOKENS_FILE': path}, clear=False):
                self.assertEqual(vote.consume_secret('TOKENS'), 'sensitive-value')
                self.assertNotIn('TOKENS', os.environ)
                self.assertNotIn('TOKENS_FILE', os.environ)
            self.assertFalse(os.path.exists(path))

    def test_consume_secret_environment_fallback_is_removed(self):
        with patch.dict(os.environ, {'TOKENS': 'sensitive-value'}, clear=False):
            self.assertEqual(vote.consume_secret('TOKENS'), 'sensitive-value')
            self.assertNotIn('TOKENS', os.environ)

    def test_scrub_browser_environment_removes_invalid_dbus_address(self):
        with patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'autolaunch:', 'TOKENS': 'secret'}, clear=False):
            vote.scrub_browser_environment()
            self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', os.environ)
            self.assertNotIn('TOKENS', os.environ)

    def test_scrub_browser_environment_preserves_valid_dbus_address(self):
        with patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/dbus-test'}, clear=False):
            vote.scrub_browser_environment()
            self.assertEqual(os.environ.get('DBUS_SESSION_BUS_ADDRESS'), 'unix:path=/tmp/dbus-test')


class ReportingTests(unittest.TestCase):

    def test_account_fingerprint_is_stable_and_hides_token(self):
        token = 'sensitive-token-value'
        fingerprint = vote.account_fingerprint(token)
        self.assertEqual(fingerprint, vote.account_fingerprint(token))
        self.assertEqual(len(fingerprint), 8)
        self.assertNotIn(token[:5], fingerprint)

    def test_short_report_stays_in_one_chunk(self):
        self.assertEqual(vote.split_telegram_message('line one\nline two', 50), ['line one\nline two'])

    def test_large_report_splits_in_order_under_limit(self):
        message = '\n'.join((f'line-{index}-' + 'x' * 30 for index in range(20)))
        chunks = vote.split_telegram_message(message, 100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all((len(chunk) <= 100 for chunk in chunks)))
        self.assertEqual('\n'.join(chunks), message)

    @patch('builtins.print')
    @patch('browser_runtime.requests.post')
    def test_telegram_report_requires_api_ok(self, post, _print):
        response = MagicMock(status_code=200)
        response.json.return_value = {'ok': False}
        post.return_value = response
        with patch.object(vote, 'TG_BOT_TOKEN', 'bot'), patch.object(vote, 'TG_CHAT_ID', 'chat'):
            self.assertFalse(vote.send_notification('report'))


class BusinessResultTests(unittest.TestCase):

    def test_success_and_cooldown_are_completed(self):
        results = [[{'status': 'success'}, {'status': 'cooldown'}]]
        self.assertFalse(vote.has_business_failure(results))

    def test_incomplete_statuses_fail_workflow(self):
        for status in ('error', 'auth_failed', 'uncertain', 'captcha_required', 'blocked'):
            with self.subTest(status=status):
                self.assertTrue(vote.has_business_failure([[{'status': status}]]))

    def test_empty_results_fail_workflow(self):
        self.assertTrue(vote.has_business_failure([]))
        self.assertTrue(vote.has_business_failure([[]]))


class CooldownSchedulingTests(unittest.TestCase):

    def test_earliest_retry_is_selected_across_accounts_and_bots(self):
        results = [[{'status': 'cooldown', 'retry_at': 300}, {'status': 'success'}], [{'status': 'cooldown', 'retry_at': 200}, {'status': 'cooldown', 'retry_at': 'bad'}]]
        self.assertEqual(vote.earliest_retry_at(results), 200)

    def test_state_file_contains_only_numeric_timestamp(self):
        results = [[{'bot_id': '111', 'account_id': 'private', 'status': 'cooldown', 'retry_at': 1786334400}]]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'next-vote.json')
            self.assertEqual(vote.write_next_vote_state(results, path), 1786334400)
            with open(path, encoding='utf-8') as file:
                self.assertEqual(json.load(file), {'next_vote_at': 1786334400})

    def test_unparsed_cooldown_uses_safe_fallback_schedule(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        retry_at = vote.earliest_retry_at([[{'status': 'cooldown'}]], now)
        self.assertEqual(retry_at, int(now.timestamp()) + vote.SUCCESS_NEXT_VOTE_DELAY_SEC)

    def test_transient_and_blocked_results_receive_backoff(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        base = int(now.timestamp())
        self.assertEqual(vote.earliest_retry_at([[{'status': 'error'}]], now), base + vote.TRANSIENT_RETRY_DELAY_SEC)
        self.assertEqual(vote.earliest_retry_at([[{'status': 'blocked'}]], now), base + vote.BLOCKED_RETRY_DELAY_SEC)
        self.assertEqual(vote.earliest_retry_at([[{'status': 'captcha_required'}]], now), base + vote.CAPTCHA_RETRY_DELAY_SEC)


class BrowserStartupRetryStateTests(unittest.TestCase):

    def browser_error(self, account_id='id'):
        return [{'account_id': account_id, 'bot_id': 'all', 'status': 'error', 'detail': 'Browser startup failed: Exception: Failed to connect to browser'}]

    def test_detects_account_level_browser_startup_failure_only(self):
        self.assertTrue(vote.is_browser_startup_result(self.browser_error()[0]))
        self.assertFalse(vote.is_browser_startup_result({'bot_id': '111', 'status': 'error', 'detail': 'Browser startup failed: Exception'}))
        self.assertFalse(vote.is_browser_startup_result({'bot_id': 'all', 'status': 'error', 'detail': 'TypeError: broken'}))

    def test_requests_retry_only_when_all_accounts_failed_browser_startup(self):
        self.assertTrue(vote.should_request_browser_startup_retry([self.browser_error('a'), self.browser_error('b')]))
        self.assertFalse(vote.should_request_browser_startup_retry([]))
        self.assertFalse(vote.should_request_browser_startup_retry([self.browser_error('a'), [{'bot_id': '111', 'status': 'success'}]]))
        self.assertFalse(vote.should_request_browser_startup_retry([[{'bot_id': 'all', 'status': 'captcha_required', 'detail': 'captcha'}]]))
        self.assertFalse(vote.should_request_browser_startup_retry([[{'bot_id': '111', 'status': 'cooldown'}]]))
        self.assertFalse(vote.should_request_browser_startup_retry([[{'bot_id': 'all', 'status': 'error', 'detail': 'RuntimeError: generic'}]]))

    def test_retry_marker_contains_only_reason_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'browser-startup-retry.json')
            self.assertTrue(vote.write_browser_startup_retry_state([self.browser_error()], path))
            with open(path, encoding='utf-8') as file:
                self.assertEqual(json.load(file), {'reason': 'browser_startup_failed'})

    def test_no_retry_marker_for_mixed_or_non_browser_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'browser-startup-retry.json')
            self.assertFalse(vote.write_browser_startup_retry_state([self.browser_error('a'), [{'bot_id': '111', 'status': 'success'}]], path))
            self.assertFalse(os.path.exists(path))


class ProtectionRetryStateTests(unittest.TestCase):

    def test_blocked_result_requests_fresh_run_marker(self):
        results = [[{'bot_id': '111', 'status': 'success'}, {'bot_id': '222', 'status': 'blocked'}]]
        self.assertTrue(vote.should_request_protection_retry(results))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'protection-retry.json')
            self.assertTrue(vote.write_protection_retry_state(results, path))
            with open(path, encoding='utf-8') as file:
                self.assertEqual(json.load(file), {'reason': vote.PROTECTION_RETRY_REASON})

    def test_non_blocked_results_do_not_request_marker(self):
        results = [[{'bot_id': '111', 'status': 'success'}, {'bot_id': '222', 'status': 'cooldown'}]]
        self.assertFalse(vote.should_request_protection_retry(results))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'protection-retry.json')
            self.assertFalse(vote.write_protection_retry_state(results, path))
            self.assertFalse(os.path.exists(path))


class ElementMatchingTests(unittest.IsolatedAsyncioTestCase):

    @patch('browser_runtime.evaluate', new_callable=AsyncMock, return_value=True)
    async def test_exact_element_matching_is_case_insensitive(self, evaluate):
        self.assertTrue(await vote._mark_exact_element(AsyncMock(), 'a,button', ['Login', 'Log in'], 'data-test'))
        expression = evaluate.await_args.args[1]
        self.assertIn('toLowerCase', expression)
        self.assertIn('new Set', expression)


class PrivacyOverlayTests(unittest.IsolatedAsyncioTestCase):

    @patch('browser_runtime.asyncio.sleep', new_callable=AsyncMock)
    @patch('browser_runtime.ui_click.click_control', new_callable=AsyncMock, return_value={'input_sent': True, 'clicked': True})
    @patch('browser_runtime.evaluate', new_callable=AsyncMock, side_effect=[{'present': True, 'button_found': True}, {'present': False}])
    async def test_privacy_overlay_dismiss_clicks_detected_consent(self, evaluate_mock, click, _sleep):
        tab = AsyncMock()
        self.assertTrue(await vote.dismiss_privacy_overlay(tab))
        expression = evaluate_mock.call_args.args[1]
        self.assertIn('we value your privacy', expression)
        self.assertIn('#accept-btn', expression)
        self.assertIn('textContent', expression)
        self.assertIn('agree', expression)
        click.assert_awaited_once()

    @patch('browser_runtime.ui_click.click_control', new_callable=AsyncMock, return_value={'input_sent': True, 'clicked': True})
    @patch('browser_runtime.dismiss_privacy_overlay', new_callable=AsyncMock)
    async def test_marked_click_dismisses_privacy_overlay_first(self, dismiss, click):
        tab = AsyncMock()
        self.assertTrue(await vote._click_marked(tab, 'data-login'))
        dismiss.assert_awaited_once_with(tab)
        click.assert_awaited_once_with(tab, vote.evaluate, '[data-login="1"]', kind='login', reacquire=None)

    @patch('builtins.print')
    @patch('browser_runtime.send_telegram_photo', return_value=True)
    @patch('browser_runtime.browser_screenshot', new_callable=AsyncMock, return_value='screenshots/privacy_overlay_dismiss_failed.png')
    @patch('browser_runtime.evaluate', new_callable=AsyncMock, return_value={'present': True, 'dismissed': False, 'reason': 'consent_button_not_found'})
    async def test_privacy_overlay_dismiss_failure_sends_screenshot(self, _evaluate, screenshot, send_photo, _print):
        tab = AsyncMock()
        with patch.object(vote, 'TG_BOT_TOKEN', 'token'), patch.object(vote, 'TG_CHAT_ID', 'chat'), patch.object(vote, 'PRIVACY_DISMISS_REPORTED', False):
            self.assertFalse(await vote.dismiss_privacy_overlay(tab))
        screenshot.assert_awaited_once_with(tab, 'screenshots/privacy_overlay_dismiss_failed.png', required=True)
        send_photo.assert_called_once()
        self.assertIn('consent_button_not_found', send_photo.call_args.args[1])

    @patch('builtins.print')
    @patch('browser_runtime.stable_challenge_clearance', new_callable=AsyncMock, return_value=True)
    @patch('browser_runtime.dismiss_privacy_overlay', new_callable=AsyncMock)
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, return_value=True)
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, side_effect=[False, True])
    async def test_solver_dismisses_privacy_overlay_before_click(self, _solved, _present, dismiss, _clear, _print):
        tab = AsyncMock()
        with patch('browser_runtime._click_cloudflare_checkbox', new=AsyncMock(return_value='sent')) as click:
            self.assertTrue(await vote.solve_turnstile(tab))
        dismiss.assert_awaited_once_with(tab)
        click.assert_awaited_once_with(tab)


class TurnstileSolverTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        click = patch('browser_runtime._click_cloudflare_checkbox', new_callable=AsyncMock, return_value='sent')
        self.click = click.start()
        self.addCleanup(click.stop)
        clearance = patch('browser_runtime.stable_challenge_clearance', new=AsyncMock(return_value=True))
        clearance.start()
        self.addCleanup(clearance.stop)

    async def test_unavailable_target_is_not_reported_as_a_sent_click(self):
        self.click.return_value = 'unavailable'
        with patch('builtins.print'), patch('browser_runtime.is_turnstile_present', new=AsyncMock(return_value=True)), patch('browser_runtime.is_turnstile_solved', new=AsyncMock(return_value=False)), patch('browser_runtime.log_challenge_diagnostic', new_callable=AsyncMock):
            self.assertFalse(await vote.solve_turnstile(AsyncMock()))

    async def test_automatic_clearance_can_complete_without_a_click(self):
        self.click.return_value = 'cleared'
        with patch('builtins.print'), patch('browser_runtime.is_turnstile_present', new=AsyncMock(return_value=True)), patch('browser_runtime.is_turnstile_solved', new=AsyncMock(return_value=False)), patch('browser_runtime.log_challenge_diagnostic', new_callable=AsyncMock):
            self.assertTrue(await vote.solve_turnstile(AsyncMock()))

    @patch('builtins.print')
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, return_value=False)
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, return_value=False)
    async def test_no_challenge_succeeds_without_click(self, _solved, _present, _print):
        tab = AsyncMock()
        self.assertTrue(await vote.solve_turnstile(tab))
        self.click.assert_not_awaited()

    @patch('builtins.print')
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, return_value=True)
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, side_effect=[False, True])
    async def test_native_click_then_response_token_succeeds(self, _solved, _present, _print):
        tab = AsyncMock()
        self.assertTrue(await vote.solve_turnstile(tab))
        self.click.assert_awaited_once_with(tab)

    @patch('builtins.print')
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, side_effect=[True, False])
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, return_value=False)
    async def test_native_click_then_page_clearance_succeeds(self, _solved, _present, _print):
        tab = AsyncMock()
        self.assertTrue(await vote.solve_turnstile(tab))
        self.click.assert_awaited_once_with(tab)

    @patch('builtins.print')
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, return_value=True)
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, return_value=False)
    async def test_click_exception_fails_immediately(self, _solved, _present, _print):
        tab = AsyncMock()
        self.click.side_effect = TypeError('missing template coordinates')
        self.assertFalse(await vote.solve_turnstile(tab))
        self.click.assert_awaited_once_with(tab)

    @patch('builtins.print')
    @patch('browser_runtime.asyncio.sleep', new_callable=AsyncMock)
    @patch('browser_runtime.is_turnstile_present', new_callable=AsyncMock, return_value=True)
    @patch('browser_runtime.is_turnstile_solved', new_callable=AsyncMock, return_value=False)
    async def test_persistent_challenge_times_out(self, _solved, _present, _sleep, _print):
        tab = AsyncMock()
        times = iter([0.0, 0.0, 31.0])
        loop = MagicMock()
        loop.time.side_effect = lambda: next(times, 1000)
        with patch('browser_runtime.asyncio.get_running_loop', return_value=loop):
            self.assertFalse(await vote.solve_turnstile(tab))
        self.click.assert_awaited_once_with(tab)


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):

    @patch('browser_runtime.asyncio.sleep', new_callable=AsyncMock)
    @patch.object(vote, 'BROWSER_START_RETRIES', 1)
    @patch('browser_runtime.uc.Browser')
    @patch('browser_runtime.browser_environment.ChromeConfig')
    async def test_partial_start_failure_is_cleaned(self, config_class, browser_class, _sleep):
        browser = browser_class.return_value
        browser.start = AsyncMock(side_effect=RuntimeError('connect failed'))
        browser.aclose = AsyncMock()
        browser._process = None
        with patch.dict(os.environ, {'TOKENS': 'token', 'ANIGAME_COOKIES_JSON': 'cookies', 'TOPGG_COOKIES_JSON': 'retired-session', 'TG_BOT_TOKEN': 'telegram', 'TG_CHAT_ID': 'chat'}, clear=False):
            with self.assertRaises(vote.BrowserStartupError):
                await vote.start_browser()
            for name in ('TOKENS', 'ANIGAME_COOKIES_JSON', 'TOPGG_COOKIES_JSON', 'TG_BOT_TOKEN', 'TG_CHAT_ID'):
                self.assertNotIn(name, os.environ)
        profile_path = config_class.call_args.kwargs['user_data_dir']
        self.assertFalse(os.path.exists(profile_path))
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()

    @patch('browser_runtime.asyncio.sleep', new_callable=AsyncMock)
    async def test_recovers_browser_when_devtools_endpoint_appears_late(self, _sleep):
        browser = MagicMock()
        browser._process = MagicMock(returncode=None)
        browser._http = MagicMock()
        browser._http.get = AsyncMock(side_effect=[OSError('not ready'), {'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools/browser/test'}])
        browser.attach = AsyncMock()
        browser.update_targets = AsyncMock()
        browser.get = AsyncMock()
        self.assertTrue(await vote.recover_slow_browser_start(browser))
        self.assertEqual(browser.websocket_url, 'ws://127.0.0.1/devtools/browser/test')
        browser.attach.assert_awaited_once()
        browser.update_targets.assert_awaited_once()
        browser.get.assert_awaited_once_with('about:blank')

    async def test_slow_browser_recovery_skips_dead_or_missing_process(self):
        browser = MagicMock()
        browser._process = None
        browser._http = MagicMock()
        self.assertFalse(await vote.recover_slow_browser_start(browser))
        browser._http.get.assert_not_called()

    async def test_chrome_process_diagnostics_redacts_and_truncates(self):

        class Stream:

            async def read(self, _limit):
                return ('secret ' + 'x' * vote.DIAGNOSTIC_DETAIL_LIMIT).encode()
        process = MagicMock(pid=4321, returncode=9, stderr=Stream(), stdout=None)
        with patch.object(vote, 'SENSITIVE_VALUES', ['secret']):
            detail = await vote.chrome_process_diagnostics(process)
        self.assertIn('pid=4321', detail)
        self.assertIn('exit=9', detail)
        self.assertIn('***', detail)
        self.assertNotIn('secret', detail)
        self.assertLessEqual(len(detail), vote.DIAGNOSTIC_DETAIL_LIMIT)

    async def test_chrome_process_diagnostics_reports_running_process(self):
        process = MagicMock(pid=99, returncode=None, stderr=None, stdout=None)
        detail = await vote.chrome_process_diagnostics(process)
        self.assertIn('pid=99', detail)
        self.assertIn('state=running', detail)

    @patch('browser_runtime.subprocess.run')
    @patch('browser_runtime.shutil.disk_usage')
    def test_browser_startup_environment_diagnostics_is_bounded_and_safe(self, disk_usage, run):
        disk_usage.return_value = MagicMock(free=512 * 1024 * 1024, total=1024 * 1024 * 1024)
        run.return_value = MagicMock(stdout='Google Chrome 140.0.0.0\n', stderr='', returncode=0)
        with patch.dict(os.environ, {'DISPLAY': ':99', 'CHROME_BIN': '/usr/bin/google-chrome'}, clear=False), patch('browser_runtime.Path.exists', return_value=True):
            detail = vote.browser_startup_environment_diagnostics()
        self.assertIn('display=:99', detail)
        self.assertIn('x11_socket=present', detail)
        self.assertIn('chrome=google-chrome', detail)
        self.assertIn('chrome_version=Google Chrome 140.0.0.0', detail)
        self.assertIn('shm_free_mb=512/1024', detail)
        self.assertIn('tmp_free_mb=512/1024', detail)
        self.assertLessEqual(len(detail), vote.DIAGNOSTIC_DETAIL_LIMIT)

    @patch('builtins.print')
    @patch('browser_runtime.recover_slow_browser_start', new_callable=AsyncMock, return_value=True)
    @patch.object(vote, 'BROWSER_START_RETRIES', 1)
    @patch('browser_runtime.uc.Browser')
    @patch('browser_runtime.browser_environment.ChromeConfig')
    async def test_start_browser_keeps_process_when_late_attach_recovers(self, _config, browser_class, recover, _print):
        browser = browser_class.return_value
        browser.start = AsyncMock(side_effect=RuntimeError('connect failed'))
        browser._process = MagicMock(returncode=None)
        result = await vote.start_browser()
        self.assertIs(result, browser)
        recover.assert_awaited_once_with(browser)
        browser.stop.assert_not_called()

    @patch('builtins.print')
    @patch('browser_runtime.close_browser', new_callable=AsyncMock)
    async def test_safe_cleanup_does_not_raise_or_force_vote_retry(self, close_browser, _print):
        close_browser.side_effect = vote.BrowserCleanupError('cleanup failed')
        self.assertFalse(await vote.close_browser_safely(MagicMock(), 'account attempt'))

    async def test_close_browser_force_wait_is_bounded(self):
        browser = MagicMock()
        browser.aclose = AsyncMock()
        process = MagicMock(returncode=None)
        process.wait = AsyncMock()
        process.kill = MagicMock()
        browser._process = process
        timeout_error = asyncio.TimeoutError()
        outcomes = iter([None, timeout_error, timeout_error])

        async def consume_then_timeout(coroutine, timeout):
            await coroutine
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome
        with patch('browser_runtime.asyncio.wait_for', side_effect=consume_then_timeout):
            with self.assertRaises(vote.BrowserCleanupError):
                await vote.close_browser(browser)
        process.kill.assert_called_once()

    async def test_forced_termination_still_deletes_profile(self):
        browser = MagicMock()
        browser.aclose = AsyncMock()
        browser._process = MagicMock(returncode=None)
        browser._process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        with tempfile.TemporaryDirectory() as directory:
            profile_path = os.path.join(directory, 'profile')
            os.mkdir(profile_path)
            browser._security_profile_path = profile_path
            with self.assertRaisesRegex(vote.BrowserCleanupError, 'profile deleted'):
                await vote.close_browser(browser)
            self.assertFalse(os.path.exists(profile_path))
        browser._process.kill.assert_called_once()

    @patch('builtins.print')
    @patch('browser_runtime.close_browser', new_callable=AsyncMock, side_effect=OSError('private-detail'))
    async def test_unexpected_cleanup_error_cannot_discard_votes(self, _close, log):
        self.assertFalse(await vote.close_browser_safely(MagicMock(), 'account attempt'))
        self.assertNotIn('private-detail', str(log.call_args_list))

    async def test_close_browser_deletes_explicit_profile(self):
        browser = MagicMock()
        browser.aclose = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            profile_path = os.path.join(directory, 'profile')
            os.mkdir(profile_path)
            browser._security_profile_path = profile_path
            await vote.close_browser(browser)
            self.assertFalse(os.path.exists(profile_path))
        browser.aclose.assert_awaited_once()
        browser.stop.assert_called_once()


class ScreenshotPrivacyTests(unittest.IsolatedAsyncioTestCase):

    @patch.object(vote, 'SEND_ERROR_SCREENSHOTS', False)
    async def test_captcha_screenshot_bypasses_optional_flag(self):
        tab = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'captcha.png')
            self.assertEqual(await vote.browser_screenshot(tab, path, required=True), path)
        tab.save_screenshot.assert_awaited_once_with(filename=path, format='png')

    @patch('builtins.print')
    @patch('browser_runtime.requests.post')
    def test_telegram_photo_requires_api_ok(self, post, _print):
        response = MagicMock(status_code=200)
        response.json.return_value = {'ok': False}
        post.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'captcha.png')
            with open(path, 'wb') as file:
                file.write(b'png')
            with patch.object(vote, 'TG_BOT_TOKEN', 'bot'), patch.object(vote, 'TG_CHAT_ID', 'chat'):
                self.assertFalse(vote.send_telegram_photo(path, 'caption'))
