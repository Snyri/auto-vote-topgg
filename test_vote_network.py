"""Only exact, completed, challenged vote submissions can permit resubmission."""

import json
import unittest
from types import SimpleNamespace as NS
from urllib.parse import quote

from vote_network import MAX_VOTE_REQUESTS, VoteNetworkState, safe_api_route, vote_operation


PAGE = "https://top.gg/bot/111/vote"
VOTE = "https://top.gg/api/bots/111/vote"
STATE = VOTE + "/status"
CHALLENGE = {"cloudflare_challenge": True, "content_kind": "html"}
JSON = {"cloudflare_challenge": False, "content_kind": "json"}


def event(url=VOTE, method="POST", request_id="1", started=101.0, page=PAGE, data=None):
    return NS(request_id=request_id, wall_time=started, document_url=page, redirect_response=None,
              request=NS(url=url, method=method, post_data=data))


class VoteRouteTests(unittest.TestCase):
    def operation(self, url=VOTE, method="POST", page=PAGE, data=None):
        return vote_operation(url, method, page, "111", data)

    def test_exact_bot_vote_and_readiness_routes(self):
        self.assertEqual(self.operation(), "vote_submission")
        self.assertEqual(self.operation(STATE, "GET"), "vote_state")
        self.assertEqual(self.operation("https://top.gg/api/client/bots/111/vote"), "vote_submission")
        self.assertEqual(self.operation("https://api.top.gg/api/entities/111/vote"), "vote_submission")

    def test_background_other_bot_origin_and_similar_routes_cannot_be_votes(self):
        for url in ("https://top.gg/api/analytics", "https://top.gg/api/bots/222/vote",
                    "https://top.gg/api/bots/111/votes", "https://top.gg/api/users/111/vote",
                    VOTE + "/preview", VOTE + "/vote", "https://top.gg.evil.example/api/bots/111/vote",
                    "https://user:SECRET@top.gg/api/bots/111/vote", VOTE.replace("https:", "http:")):
            with self.subTest(url=url): self.assertEqual(self.operation(url), "unrelated")
        self.assertEqual(self.operation(page="https://top.gg/bot/222/vote"), "unrelated")
        self.assertEqual(self.operation(page="https://top.gg/api/bots/111/vote"), "unrelated")
        self.assertEqual(self.operation(method="PUT"), "unrelated")

    def test_single_rpc_requires_an_explicit_matching_bot(self):
        url = "https://top.gg/api/trpc/votes.castVote?batch=1"
        data = json.dumps({"0": {"json": {"botId": "111", "token": "PRIVATE"}}})
        self.assertEqual(self.operation(url, data=data), "vote_submission")
        for bad in (None, "not-json", data.replace("111", "222"), json.dumps({"token": "111"}),
                    json.dumps({"0": {"botId": "111"}, "1": {"botId": "111"}}),
                    json.dumps({"botId": "111", "nested": {"botId": "222"}})):
            with self.subTest(data=bad): self.assertEqual(self.operation(url, data=bad), "unrelated")
        self.assertEqual(self.operation(url.replace("votes.castVote", "votes.castVote,analytics.track"), data=data), "unrelated")
        self.assertEqual(self.operation(url.replace("castVote", "getVotes"), data=data), "unrelated")

    def test_rpc_read_and_unrecognized_protocols_remain_separate(self):
        url = "https://top.gg/api/trpc/voting.canVote?input=" + quote(json.dumps({"json": {"botId": "111"}}))
        self.assertEqual(self.operation(url, "GET"), "vote_state")
        self.assertEqual(self.operation("https://top.gg/api/graphql", data=json.dumps({"botId": "111"})), "unrelated")
        self.assertEqual(self.operation(PAGE), "unrelated")

    def test_deep_or_oversized_rpc_input_cannot_enable_a_retry(self):
        url = "https://top.gg/api/trpc/bots.vote"
        data = {"botId": "111"}
        for _ in range(8): data = {"nested": data}
        for raw in (json.dumps(data), json.dumps({"botId": "111", "token": "X" * 32768})):
            self.assertEqual(self.operation(url, data=raw), "unrelated")

    def test_route_templates_exclude_identifiers_unknown_segments_and_query_secrets(self):
        self.assertEqual(safe_api_route(VOTE + "?token=PRIVATE"), "/api/bots/:id/vote")
        self.assertEqual(safe_api_route("https://top.gg/api/trpc/votes.castVote?input=PRIVATE"), "/api/trpc/votes.castVote")
        template = safe_api_route("https://top.gg/api/PRIVATE_UUID/bots/111/vote/PRIVATE_TOKEN")
        self.assertNotIn("PRIVATE", template)
        self.assertNotIn("111", template)
        self.assertIsNone(safe_api_route("https://private.example/api/PRIVATE"))


class VoteResponseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.state = VoteNetworkState(clock=lambda: self.now)
        self.state.select_bot("111")
        self.state.begin_input()
        self.state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100.0})

    def response(self, request=None, status=403, headers=CHALLENGE, finished=True):
        info = self.state.on_request(request or event())
        self.state.response(info, status, headers)
        if finished:
            self.state.finish(info)
        return info

    def test_completed_exact_challenge_establishes_rejection_not_success(self):
        self.response()
        self.assertTrue(self.state.definitely_rejected())
        self.assertTrue(self.state.protection_pending())

    def test_generic_forbidden_html_success_or_transport_failure_is_uncertain(self):
        for status, headers, finished in ((403, JSON, True), (200, CHALLENGE, True),
                                          (200, JSON, True), (403, CHALLENGE, False)):
            with self.subTest(status=status, headers=headers, finished=finished):
                self.state.begin_input()
                self.state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100.0})
                self.response(status=status, headers=headers, finished=finished)
                self.assertFalse(self.state.definitely_rejected())

    def test_other_api_denials_are_not_vote_rejections_or_readiness_blocks(self):
        self.response(event("https://top.gg/api/analytics"))
        self.assertFalse(self.state.definitely_rejected())
        self.assertFalse(self.state.protection_pending())

    def test_no_trusted_click_or_valid_press_time_keeps_submission_uncertain(self):
        self.response()
        for receipt in ({}, {"pressed": True, "released": True, "clicked": False, "pressed_at": 100.0},
                        {"pressed": True, "released": True, "clicked": True, "pressed_at": float("nan")},
                        {"pressed": True, "released": True, "clicked": True, "pressed_at": True}):
            self.state.receipt(receipt)
            self.assertFalse(self.state.definitely_rejected())

    def test_pre_press_request_cannot_prove_this_input_was_rejected(self):
        self.response(event(started=99.9))
        self.assertFalse(self.state.definitely_rejected())

    def test_redirect_or_mixed_success_never_allows_resubmission(self):
        info = self.response()
        info["redirected"] = True
        self.assertFalse(self.state.definitely_rejected())
        info["redirected"] = False
        self.response(event(request_id="2"), status=200, headers=JSON)
        self.assertFalse(self.state.definitely_rejected())

    def test_inflight_second_submission_keeps_rejection_uncertain(self):
        self.response()
        self.state.on_request(event(request_id="2"))
        self.assertFalse(self.state.definitely_rejected())

    def test_unknown_same_site_write_vetoes_retry_even_with_an_exact_denial(self):
        for url in ("https://top.gg/api/graphql", PAGE, "https://top.gg/api/analytics"):
            with self.subTest(url=url):
                self.state.begin_input()
                self.state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100.0})
                self.response()
                self.response(event(url=url, request_id="unknown"), 200, JSON)
                self.assertFalse(self.state.definitely_rejected())

    def test_known_vote_state_and_third_party_traffic_do_not_veto_exact_rejection(self):
        self.response()
        self.response(event(STATE, "GET", "state"), 200, JSON)
        self.response(event("https://analytics.example/track", request_id="external"), 200, JSON)
        self.assertTrue(self.state.definitely_rejected())

    def test_conflicting_response_statuses_are_uncertain(self):
        info = self.response()
        self.state.response(info, 200, JSON)
        self.assertFalse(self.state.definitely_rejected())

    def test_late_challenge_metadata_can_complete_an_existing_denial(self):
        info = self.response(headers=JSON)
        self.assertFalse(self.state.definitely_rejected())
        self.state.response(info, 403, CHALLENGE)
        self.assertTrue(self.state.definitely_rejected())

    def test_cdps_float_timestamp_subclass_is_supported(self):
        class Epoch(float): pass
        self.response(event(started=Epoch(101)))
        self.assertTrue(self.state.definitely_rejected())

    def test_new_input_or_new_bot_cannot_inherit_a_rejection(self):
        old = self.response()
        self.state.begin_input()
        self.state.response(old, 403, CHALLENGE)
        self.assertFalse(self.state.definitely_rejected())
        self.state.select_bot("222")
        self.state.response(old, 403, CHALLENGE)
        self.assertFalse(self.state.protection_pending())

    def test_only_fresh_vote_state_json_clears_recent_protection(self):
        self.response(event(started=101))
        self.response(event(STATE, "GET", "old", started=100), 200, JSON)
        self.assertTrue(self.state.protection_pending())
        self.response(event("https://top.gg/api/analytics", "GET", "ad", started=102), 200, JSON)
        self.assertTrue(self.state.protection_pending())
        self.response(event(STATE, "GET", "fresh", started=103), 200, JSON)
        self.assertFalse(self.state.protection_pending())

    def test_elapsed_time_does_not_clear_denial_and_storage_is_bounded(self):
        self.response()
        self.now += 3600
        self.assertTrue(self.state.protection_pending())
        for n in range(MAX_VOTE_REQUESTS + 1): self.response(event(request_id=str(n)))
        self.assertEqual(len(self.state.candidates), MAX_VOTE_REQUESTS)
        self.assertFalse(self.state.definitely_rejected())

    def test_vote_state_headers_do_not_clear_a_denial_before_completion(self):
        self.response()
        ready = self.response(event(STATE, "GET", "ready", started=102), 200, JSON, finished=False)
        self.assertTrue(self.state.protection_pending())
        self.state.finish(ready)
        self.assertFalse(self.state.protection_pending())

    def test_redirect_failed_or_conflicting_state_response_cannot_clear_denial(self):
        for problem in ("redirect", "failed", "conflicting"):
            with self.subTest(problem=problem):
                self.response(event(request_id="denied", started=101))
                ready = self.response(event(STATE, "GET", "ready", started=102), 200, JSON, finished=False)
                if problem == "redirect": ready["redirected"] = True
                if problem == "conflicting": self.state.response(ready, 403, JSON)
                self.state.finish(ready, failed=problem == "failed")
                self.assertTrue(self.state.protection_pending())

    def test_late_old_denial_cannot_weaken_the_newest_recovery_boundary(self):
        self.response(event(request_id="new-denial", started=105))
        self.response(event(request_id="old-denial", started=101))
        self.response(event(STATE, "GET", "stale-ready", started=103), 200, JSON)
        self.assertTrue(self.state.protection_pending())
        self.response(event(STATE, "GET", "new-ready", started=106), 200, JSON)
        self.assertFalse(self.state.protection_pending())

    def test_late_cloudflare_headers_restore_the_denial_after_apparent_recovery(self):
        self.response()
        ready = self.response(event(STATE, "GET", "ready", started=102), 200, JSON)
        self.assertFalse(self.state.protection_pending())
        self.state.response(ready, 403, CHALLENGE)
        self.assertTrue(self.state.protection_pending())

    def test_late_conflicting_status_or_html_revokes_this_response_recovery(self):
        for status, headers in ((403, JSON), (200, {"cloudflare_challenge": False, "content_kind": "html"})):
            with self.subTest(status=status, headers=headers):
                self.response(event(request_id="denied", started=101))
                ready = self.response(event(STATE, "GET", "ready", started=102), 200, JSON)
                self.assertFalse(self.state.protection_pending())
                self.state.response(ready, status, headers)
                self.assertTrue(self.state.protection_pending())

    def test_challenge_header_with_json_does_not_establish_recovery(self):
        self.response()
        self.response(event(STATE, "GET", "ready", started=102), 200,
                      {"cloudflare_challenge": True, "content_kind": "json"})
        self.assertTrue(self.state.protection_pending())

    def test_unknown_denial_timestamp_requires_a_fresh_context(self):
        self.response(event(started=None))
        self.response(event(STATE, "GET", "ready", started=102), 200, JSON)
        self.assertTrue(self.state.protection_pending())
        fresh = VoteNetworkState()
        fresh.select_bot("111")
        self.assertFalse(fresh.protection_pending())

    def test_no_urls_or_payload_credentials_are_retained(self):
        data = json.dumps({"botId": "111", "token": "PAYLOAD_SECRET"})
        self.response(event("https://top.gg/api/trpc/votes.vote?credential=URL_SECRET", data=data))
        saved = repr(self.state.__dict__)
        self.assertNotIn("SECRET", saved)
        self.assertNotIn("https://", saved)


if __name__ == "__main__":
    unittest.main()
