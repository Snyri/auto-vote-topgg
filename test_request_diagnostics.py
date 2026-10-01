"""Passive response classification, bounded storage and credential exclusion."""

import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from request_diagnostics import MAX_REQUESTS, RequestDiagnostics, request_kind, set_phase
from test_graphql_vote import payload as graphql_payload


class NetworkDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tab = MagicMock(); self.tab.send = AsyncMock()
        self.tracker = RequestDiagnostics(self.tab)

    async def request(self, url="https://top.gg/api/submit?token=secret", method="POST", request_id="1"):
        await self.tracker.on_request(NS(request_id=request_id, request=NS(url=url, method=method), redirect_response=None))

    async def test_request_categories_never_contain_url_credentials(self):
        expected = {
            "https://top.gg/api/auth/session?token=SECRET": "topgg_session",
            "https://top.gg/api/submit/SECRET": "topgg_api",
            "https://top.gg/_next/SECRET.js": "topgg_asset",
            "https://discord.com/oauth2/authorize?code=SECRET": "discord_oauth",
            "https://challenges.cloudflare.com/widget/SECRET": "cloudflare_challenge",
            "https://top.gg.evil.example/api/submit": "other",
        }
        for url, kind in expected.items(): self.assertEqual(request_kind(url), kind)

    async def test_vote_write_response_is_metadata_only_and_not_a_success_claim(self):
        self.tracker.phase = "vote_input"
        await self.request()
        with patch("builtins.print") as log:
            await self.tracker.on_response(NS(request_id="1", response=NS(status=200, headers={
                "Content-Type": "application/json", "Set-Cookie": "SESSION_SECRET", "Authorization": "TOKEN_SECRET",
            })))
        text = log.call_args.args[0]
        payload = json.loads(text.split(": ", 1)[1])
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["request_kind"], "topgg_api")
        self.assertNotIn("success", payload)
        for secret in ("SESSION_SECRET", "TOKEN_SECRET", "token=secret", "/api/submit"):
            self.assertNotIn(secret, text)
        self.assertEqual(payload["api_route"], "/api/:other")

    async def test_extra_info_records_cors_hidden_denial_and_deduplicates_response(self):
        await self.request()
        headers = {"cf-mitigated": "challenge", "cf-ray": "0123456789abcdef-ORD", "content-type": "text/html"}
        with patch("builtins.print") as log:
            await self.tracker.on_extra(NS(request_id="1", status_code=403, headers=headers))
            await self.tracker.on_response(NS(request_id="1", response=NS(status=403, headers=headers)))
        log.assert_called_once()
        value = json.loads(log.call_args.args[0].split(": ", 1)[1])
        self.assertTrue(value["cloudflare_challenge"])
        self.assertEqual(value["cf_ray"], "0123456789abcdef-ORD")

    async def test_untrusted_header_values_are_not_printed(self):
        await self.request(method="SECRET_METHOD")
        with patch("builtins.print") as log:
            await self.tracker.on_response(NS(request_id="1", response=NS(status=403, headers={
                "cf-mitigated": "SECRET_CHALLENGE", "cf-ray": "SECRET_RAY", "content-type": "SECRET_TYPE",
            })))
        self.assertNotIn("SECRET", log.call_args.args[0])
        self.assertIn('"method": "OTHER"', log.call_args.args[0])

    async def test_subresource_denials_are_separate_from_api_writes(self):
        self.tracker.phase = "vote_confirmation"
        await self.request("https://ads.example/SECRET", "GET")
        with patch("builtins.print") as log:
            await self.tracker.on_response(NS(request_id="1", response=NS(status=403, headers={})))
        self.assertIn('"request_kind": "other"', log.call_args.args[0])
        self.assertNotIn("SECRET", log.call_args.args[0])

    async def test_storage_is_bounded_and_finished_requests_are_removed(self):
        for n in range(MAX_REQUESTS + 2): await self.request(request_id=str(n))
        self.assertEqual(len(self.tracker.requests), MAX_REQUESTS)
        self.assertNotIn("0", self.tracker.requests)
        await self.tracker.on_finished(NS(request_id=str(MAX_REQUESTS + 1)))
        self.assertEqual(len(self.tracker.requests), MAX_REQUESTS - 1)

    async def test_failed_request_has_no_error_text_or_url_in_public_log(self):
        await self.request()
        with patch("builtins.print") as log:
            await self.tracker.on_failed(NS(request_id="1", error_text="SECRET_URL", canceled=False))
        self.assertNotIn("SECRET", log.call_args.args[0])
        self.assertEqual(len(self.tracker.requests), 0)

    async def test_late_extra_headers_can_still_identify_a_completed_or_failed_denial(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                await self.request()
                with patch("builtins.print") as log:
                    if failed: await self.tracker.on_failed(NS(request_id="1"))
                    else: await self.tracker.on_finished(NS(request_id="1"))
                    await self.tracker.on_extra(NS(request_id="1", status_code=403, headers={"cf-mitigated": "challenge"}))
                self.assertIn('"cloudflare_challenge": true', log.call_args.args[0])

    async def test_handlers_and_phase_are_scoped_to_one_browser_session(self):
        await self.tracker.start()
        self.assertEqual(self.tab.add_handler.call_count, 5)
        set_phase(self.tab, "vote_input")
        self.assertEqual(self.tracker.phase, "vote_input")
        set_phase(self.tab, "SECRET")
        self.assertEqual(self.tracker.phase, "vote_input")
        self.tracker.stop()
        self.assertEqual(self.tab.remove_handler.call_count, 5)
        self.assertIsNone(self.tab._topgg_diagnostics)

    async def test_diagnostic_failure_does_not_fail_a_vote_or_leave_handlers(self):
        self.tab.send.side_effect = TimeoutError
        with patch("builtins.print"):
            await self.tracker.start()
        self.assertEqual(self.tab.remove_handler.call_count, 5)

    async def test_graphql_response_body_is_inspected_without_retaining_or_printing_secrets(self):
        self.tracker.vote_network.select_bot("111")
        self.tracker.phase = "vote_confirmation"
        self.tracker.vote_network.begin_input()
        self.tracker.vote_network.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100.0})
        raw = graphql_payload(variables={"bot": "111", "token": "PRIVATE_REQUEST_TOKEN"})
        self.tab.send.return_value = ('{"errors":[{"message":"captcha: PRIVATE_RESPONSE_TOKEN"}]}', False)
        with patch("builtins.print") as log:
            await self.tracker.on_request(NS(request_id="gql", wall_time=101.0,
                document_url="https://top.gg/bot/111/vote", redirect_response=None,
                request=NS(url="https://top.gg/api/graphql", method="POST", post_data=raw)))
            await self.tracker.on_response(NS(request_id="gql", response=NS(status=200,
                headers={"content-type": "application/graphql-response+json"})))
            await self.tracker.on_finished(NS(request_id="gql"))
        self.assertEqual(self.tracker.vote_network.submission_outcome(), "captcha_required")
        self.assertNotIn("PRIVATE", repr(self.tracker.completed))
        self.assertNotIn("PRIVATE", repr(log.call_args_list))
        self.assertIn('"outcome": "captcha_required"', repr(log.call_args_list))

    async def test_unavailable_body_cannot_clear_graphql_denial(self):
        state = self.tracker.vote_network
        state.select_bot("111")
        self.tab.send.side_effect = TimeoutError("PRIVATE")
        with patch("builtins.print"):
            for request_id, query, status, headers in (
                ("denied", graphql_payload(), 403, {"cf-mitigated":"challenge", "content-type":"text/html"}),
                ("read", graphql_payload('query Cast { canVote(botId:"111") }'), 200, {"content-type":"application/json"}),
            ):
                await self.tracker.on_request(NS(request_id=request_id, wall_time=101.0,
                    document_url="https://top.gg/bot/111/vote", redirect_response=None,
                    request=NS(url="https://top.gg/api/graphql", method="POST", post_data=query)))
                await self.tracker.on_response(NS(request_id=request_id, response=NS(status=status, headers=headers)))
                await self.tracker.on_finished(NS(request_id=request_id))
        self.assertTrue(state.protection_pending())
