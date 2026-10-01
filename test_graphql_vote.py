"""GraphQL transport evidence, including the 403 and 200/error patterns seen overnight."""

import json
import unittest

import graphql_vote
from test_vote_network import CHALLENGE, JSON, event
from vote_network import VoteNetworkState, vote_operation


URL = "https://top.gg/api/graphql"
PAGE = "https://top.gg/bot/111/vote"


def payload(query='mutation Cast($bot: ID!) { castVote(botId: $bot) { ok } }', variables=None, name="Cast"):
    return json.dumps({"query": query, "variables": variables or {"bot": "111"}, "operationName": name})


class GraphQLRequestTests(unittest.TestCase):
    def operation(self, raw):
        return vote_operation(URL, "POST", PAGE, "111", raw)

    def test_linked_variable_alias_and_inline_input_are_recognized(self):
        for raw in (payload(), payload('mutation Cast { saved: castVote(botId: "111") { ok } }'),
                    payload('mutation Cast($input: VoteInput!) { vote(input: $input) { ok } }',
                            {"input": {"botId": "111", "captchaToken": "SECRET"}}),
                    payload('mutation Cast($bot: ID! = "111") { castVote(botId: $bot) { ok } }',
                            {"unrelated": "unused"})):
            with self.subTest(raw=raw):
                self.assertEqual(self.operation(raw), "vote_submission")

    def test_matching_id_in_unused_variables_comments_or_strings_is_not_target_evidence(self):
        for raw in (payload(variables={"bot": "222", "botId": "111"}),
                    payload('mutation Cast { castVote(token: "botId:111") { ok } }'),
                    payload('mutation Cast { castVote(token: "111") { ok } } # botId:111'),
                    payload('mutation Cast { analytics(botId: "111") { vote } }')):
            with self.subTest(raw=raw):
                self.assertEqual(self.operation(raw), "unrelated")

    def test_internal_entity_target_is_distinct_from_matching_discord_bot(self):
        result = graphql_vote.inspect_request(payload('mutation Cast { voteEntity(entityId: "999") { ok } }'), "111")
        self.assertEqual(result["operation"], "vote_submission")
        self.assertEqual(result["graphql_target"], "page_entity")
        self.assertNotIn("999", result["entity_key"])

    def test_single_batch_is_supported_but_mixed_batches_and_root_writes_are_unknown(self):
        self.assertEqual(self.operation("[" + payload() + "]"), "vote_submission")
        for raw in ("[" + payload() + "," + payload() + "]",
                    payload('mutation Cast { castVote(botId:"111") { ok } analytics { ok } }'),
                    payload('mutation Cast { a:castVote(botId:"111") { ok } b:castVote(botId:"222") { ok } }')):
            self.assertEqual(self.operation(raw), "unrelated")

    def test_query_state_is_not_a_mutation_even_when_its_operation_name_mentions_vote(self):
        for query in ('query Cast { canVote(botId:"111") }',
                      'query Cast { bot(id:"111") { canVote } }'):
            self.assertEqual(self.operation(payload(query)), "vote_state")
        self.assertEqual(self.operation(payload('query Cast { bot(id:"111") { description monthlyVotes } }')), "unrelated")

    def test_state_fields_nested_under_the_entity_are_reachable(self):
        self.assertEqual(self.operation(payload('query Cast { entity(id:"111") { voting { canVote } } }')), "vote_state")

    def test_unknown_root_exposes_a_fixed_gate_without_becoming_a_known_vote(self):
        result = graphql_vote.inspect_request(payload('mutation Cast { PRIVATE_ROOT(botId:"111") { ok } }'), "111")
        self.assertEqual(result["operation"], "unrelated")
        self.assertEqual(result["graphql_kind"], "mutation")
        self.assertEqual(result["graphql_target"], "matching_bot")
        self.assertEqual(result["graphql_gate"], "unrecognized_field")
        self.assertIsInstance(result["response_key"], str)
        self.assertNotIn("PRIVATE", repr(result))

    def test_nested_union_and_named_output_fragments_do_not_hide_the_root_operation(self):
        raw = payload('mutation Cast { castVote(botId:"111") { ... on VoteSuccess { ok } ...Error } } '
                      'fragment Error on VoteError { code }')
        self.assertEqual(self.operation(raw), "vote_submission")
        raw = payload('query Cast { bot(id:"111") { ...State } } fragment State on Bot { canVote }')
        self.assertEqual(self.operation(raw), "vote_state")

    def test_unsupported_documents_do_not_activate_retry(self):
        for query in ('mutation Cast { castVote(botId:"111") @skip(if:true) { ok } }',
                      'mutation Cast { ...Fields } fragment Fields on Mutation { castVote(botId:"111") { ok } }',
                      'mutation Cast { castVote(botId:"111") { ok } } query Other { canVote(botId:"111") }',
                      'mutation Cast { castVote(botId:"111", botId:"222") { ok } }',
                      'mutation Cast($bot: ID! ID!) { castVote(botId:$bot) { ok } }',
                      'mutation Cast { castVote(botId:"111") { ok }',
                      'mutation Cast { castVote(botId:true) { ok } }'):
            with self.subTest(query=query): self.assertEqual(self.operation(payload(query)), "unrelated")
        self.assertEqual(self.operation(payload(name="Wrong")), "unrelated")
        self.assertEqual(self.operation(payload(variables={"bot": "1" * 100})), "unrelated")

    def test_request_origin_document_and_payload_limits_remain_enforced(self):
        self.assertEqual(vote_operation(URL, "POST", "https://top.gg/bot/222/vote", "111", payload()), "unrelated")
        self.assertEqual(vote_operation(URL.replace("top.gg", "top.gg.evil.test"), "POST", PAGE, "111", payload()), "unrelated")
        for raw in (None, "broken", "[]", json.dumps({"variables": {"botId": "111"}}), payload() + " " * 32768):
            self.assertEqual(self.operation(raw), "unrelated")

    def test_alias_query_variable_and_credentials_are_never_retained(self):
        result = graphql_vote.inspect_request(payload(
            'mutation Cast { PRIVATE_ALIAS: castVote(botId:"111", token:"PRIVATE_TOKEN") { ok } }'), "111")
        self.assertNotIn("PRIVATE", repr(result))


class GraphQLResponseTests(unittest.TestCase):
    def setUp(self):
        self.key = graphql_vote.inspect_request(payload(), "111")["response_key"]

    def test_200_with_graphql_errors_or_missing_data_is_not_usable(self):
        for body in ({"errors": [{"message": "private failure"}]}, {"data": {"castVote": None}},
                     {"data": {}}, {"data": {"different": True}}):
            self.assertNotEqual(graphql_vote.inspect_response(json.dumps(body), self.key), "usable")

    def test_captcha_errors_return_only_a_fixed_category(self):
        for error in ({"message": "captcha failed: PRIVATE_TOKEN"},
                      {"extensions": {"code": "TURNSTILE_REQUIRED"}}):
            self.assertEqual(graphql_vote.inspect_response(json.dumps({"errors": [error]}), self.key), "captcha_required")
        self.assertEqual(graphql_vote.inspect_response(
            '{"data":{"castVote":{"__typename":"VoteCaptchaRequired","token":"PRIVATE"}}}', self.key),
            "captcha_required")

    def test_usable_response_is_transport_evidence_not_vote_success(self):
        self.assertEqual(graphql_vote.inspect_response('{"data":{"castVote":{"ok":true}}}', self.key), "usable")
        self.assertEqual(graphql_vote.inspect_response('{"data":{"castVote":{"ok":false}}}', self.key), "error")
        self.assertEqual(graphql_vote.inspect_response('{"data":{"castVote":false}}', self.key, "vote_submission"), "error")
        self.assertEqual(graphql_vote.inspect_response('{"data":{"castVote":false}}', self.key, "vote_state"), "usable")

    def test_invalid_oversized_and_base_shapes_remain_unknown(self):
        for raw in (None, "broken", "null", "[]", '{"data":{}}' + " " * graphql_vote.MAX_RESPONSE):
            self.assertNotEqual(graphql_vote.inspect_response(raw, self.key), "usable")


class GraphQLNetworkTests(unittest.TestCase):
    def setUp(self):
        self.state = VoteNetworkState()
        self.state.select_bot("111")
        self.state.begin_input()
        self.state.receipt({"pressed": True, "released": True, "clicked": True, "pressed_at": 100.0})

    def request(self, raw=None, request_id="vote", status=403, headers=CHALLENGE, time=101):
        info = self.state.on_request(event(URL, request_id=request_id, data=raw or payload(), started=time))
        self.state.response(info, status, headers)
        self.state.finish(info)
        return info

    def test_graphql_403_after_trusted_click_is_a_retryable_rejection(self):
        self.request()
        self.assertTrue(self.state.definitely_rejected())
        self.assertTrue(self.state.protection_pending())

    def test_entity_vote_denial_also_requires_all_other_writes_to_be_denied(self):
        self.request(payload('mutation Cast { voteEntity(entityId:"999") { ok } }'))
        self.assertTrue(self.state.definitely_rejected())
        unknown = self.state.on_request(event(URL, request_id="unknown", data='{"query":"mutation { other }"}'))
        self.state.response(unknown, 200, JSON)
        self.state.finish(unknown)
        self.assertFalse(self.state.definitely_rejected())

    def test_200_error_body_or_headers_alone_cannot_clear_an_api_denial(self):
        self.request()
        read = self.request(payload('query Cast { canVote(botId:"111") }'), "read", 200, JSON, 102)
        self.assertTrue(self.state.protection_pending())
        self.state.response_body(read, "error")
        self.assertTrue(self.state.protection_pending())
        fresh = self.request(payload('query Cast { canVote(botId:"111") }'), "fresh", 200, JSON, 103)
        self.state.response_body(fresh, "usable")
        self.assertFalse(self.state.protection_pending())

    def test_another_entity_cannot_clear_or_establish_current_entity_rejection(self):
        self.request(payload('mutation Cast { voteEntity(entityId:"999") { ok } }'))
        read = self.request(payload('query Cast { canVote(entityId:"998") }'), "read", 200, JSON, 102)
        self.state.response_body(read, "usable")
        self.assertTrue(self.state.protection_pending())

    def test_late_conflicting_metadata_revokes_graphql_recovery(self):
        self.request()
        read = self.request(payload('query Cast { canVote(botId:"111") }'), "read", 200, JSON, 102)
        self.state.response_body(read, "usable")
        self.assertFalse(self.state.protection_pending())
        self.state.response(read, 403, CHALLENGE)
        self.assertTrue(self.state.protection_pending())

    def test_readonly_graphql_does_not_veto_an_explicit_vote_denial(self):
        self.request()
        self.request(payload('query Cast { entity(id:"111") { description } }'), "read", 200, JSON, 102)
        self.assertTrue(self.state.definitely_rejected())

    def test_application_error_is_scoped_to_latest_trusted_mutation(self):
        first = self.request(status=200, headers=JSON)
        self.state.response_body(first, "captcha_required")
        self.assertEqual(self.state.submission_outcome(), "captcha_required")
        next_request = self.request(request_id="second", status=200, headers=JSON, time=102)
        self.state.response_body(next_request, "usable")
        self.assertEqual(self.state.submission_outcome(), "usable")
        self.state.end_input()
        self.assertIsNone(self.state.submission_outcome())


if __name__ == "__main__":
    unittest.main()
