"""A fresh, stable application confirmation can avoid a disruptive reload."""

import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import vote


def raw_page(**overrides):
    return {
        "text": "Thanks for voting!",
        "exact_vote_page": True,
        "ready": True,
        "vote_enabled": False,
        "challenge": False,
        "login_required": False,
        "error_present": False,
        **overrides,
    }


def receipt(evidence="thanks for voting", *, confirmed=True, observed=True):
    return {"confirmed": confirmed, "evidence": evidence, "observed": observed}


BASELINE = receipt(None, confirmed=False)


class VotePageReceiptEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, raw):
        with patch("vote.evaluate", new=AsyncMock(return_value=raw)):
            return await vote.vote_page_confirmation(MagicMock(), "111")

    async def test_only_strong_application_confirmation_is_accepted(self):
        for text, evidence in (
            ("Thanks for voting!", "thanks for voting"),
            ("You have already voted.", "you have already voted"),
            ("You can vote again in 11 hours.", "bounded cooldown"),
        ):
            with self.subTest(text=text):
                actual = await self.inspect(raw_page(text=text))
                self.assertIs(actual["observed"], True)
                self.assertIs(actual["confirmed"], True)
                self.assertEqual(actual["evidence"], evidence)

    async def test_vague_text_or_plain_success_word_does_not_confirm(self):
        for text in ("Vote", "Success", "You can vote again later", "Voting helps this bot grow"):
            with self.subTest(text=text):
                actual = await self.inspect(raw_page(text=text))
                self.assertIs(actual["confirmed"], False)
                self.assertIsNone(actual["evidence"])

    async def test_success_text_with_a_conflicting_page_state_is_rejected(self):
        for conflict in (
            {"exact_vote_page": False}, {"ready": False},
            {"vote_enabled": True}, {"challenge": True},
            {"login_required": True}, {"error_present": True},
        ):
            with self.subTest(conflict=conflict):
                actual = await self.inspect(raw_page(**conflict))
                self.assertIs(actual["confirmed"], False)
                # Keep baseline evidence even while Vote remains enabled.
                self.assertEqual(actual["evidence"], "thanks for voting")

    async def test_malformed_browser_result_cannot_be_a_clean_baseline(self):
        for raw in (None, True, "Thanks for voting!", [], {}, {"text": "Thanks for voting!"}):
            with self.subTest(raw=raw):
                actual = await self.inspect(raw)
                self.assertIs(actual["confirmed"], False)
                self.assertIs(actual["observed"], False)

    async def test_wrong_page_or_loading_is_not_a_valid_pre_click_baseline(self):
        for conflict in ({"exact_vote_page": False}, {"ready": False}):
            with self.subTest(conflict=conflict):
                before = await self.inspect(raw_page(text="Loading", **conflict))
                self.assertIs(before["observed"], False)
                with patch("vote.vote_page_confirmation", new=AsyncMock(return_value=receipt())) as observe:
                    self.assertFalse(await vote.confirm_vote_without_reload(MagicMock(), "111", before))
                observe.assert_not_awaited()

    async def test_browser_signal_types_must_be_real_booleans(self):
        for field in ("exact_vote_page", "ready", "vote_enabled", "challenge", "login_required", "error_present"):
            for invalid in (1, 0, "false", None):
                with self.subTest(field=field, invalid=invalid):
                    actual = await self.inspect(raw_page(**{field: invalid}))
                    self.assertIs(actual["confirmed"], False)
                    self.assertIs(actual["observed"], False)

    async def test_failure_to_read_page_is_not_evidence(self):
        for error in (RuntimeError("private browser detail"), TimeoutError("private page detail")):
            with self.subTest(error=type(error).__name__):
                with patch("vote.evaluate", new=AsyncMock(side_effect=error)):
                    actual = await vote.vote_page_confirmation(MagicMock(), "111")
                self.assertIs(actual["observed"], False)
                self.assertIs(actual["confirmed"], False)

    async def test_returned_evidence_does_not_include_raw_private_page_text(self):
        private_text = "Thanks for voting! Private user and session material"
        actual = await self.inspect(raw_page(text=private_text))
        self.assertEqual(set(actual), {"observed", "confirmed", "evidence"})
        self.assertNotIn("Private user", json.dumps(actual))

    async def test_stalled_page_observation_is_cancelled_within_budget(self):
        original_wait_for = asyncio.wait_for
        cancelled = asyncio.Event()
        timeouts = []

        async def hanging_evaluate(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def accelerated_wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=0.01)

        with (
            patch("vote.evaluate", new=hanging_evaluate),
            patch("vote.asyncio.wait_for", new=accelerated_wait_for),
        ):
            actual = await original_wait_for(vote.vote_page_confirmation(MagicMock(), "111"), timeout=0.5)
        self.assertEqual(timeouts, [2])
        self.assertTrue(cancelled.is_set())
        self.assertIs(actual["observed"], False)
        self.assertIs(actual["confirmed"], False)


class StableVotePageReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def confirm(self, snapshots, before=None):
        tab = AsyncMock()
        with (
            patch("vote.vote_page_confirmation", new=AsyncMock(side_effect=snapshots)) as inspect,
            patch("vote.asyncio.sleep", new=AsyncMock()) as sleep,
            patch("vote.topgg_session_probe", new=AsyncMock()) as session_probe,
        ):
            actual = await vote.confirm_vote_without_reload(tab, "111", BASELINE if before is None else before)
        tab.reload.assert_not_awaited()
        tab.get.assert_not_awaited()
        session_probe.assert_not_awaited()
        return actual, inspect, sleep

    async def test_two_matching_new_confirmations_succeed_without_network_requests(self):
        actual, inspect, sleep = await self.confirm([receipt(), receipt()])
        self.assertIs(actual, True)
        self.assertEqual(inspect.await_count, 2)
        self.assertTrue(any(call.args == (2,) for call in sleep.await_args_list))

    async def test_existing_marker_is_not_reused_as_a_new_vote(self):
        actual, _, _ = await self.confirm(
            [receipt()] * 4,
            before=receipt(confirmed=False),
        )
        self.assertIs(actual, False)

    async def test_unobserved_baseline_cannot_establish_marker_freshness(self):
        actual, _, _ = await self.confirm(
            [receipt()] * 4,
            before=receipt(None, confirmed=False, observed=False),
        )
        self.assertIs(actual, False)

    async def test_one_transient_confirmation_is_not_enough(self):
        actual, inspect, _ = await self.confirm([receipt(), BASELINE, BASELINE, BASELINE])
        self.assertIs(actual, False)
        self.assertEqual(inspect.await_count, 4)

    async def test_consecutive_positive_snapshots_must_have_the_same_evidence(self):
        actual, _, _ = await self.confirm([
            receipt(), receipt("bounded cooldown"), receipt(), receipt("bounded cooldown"),
        ])
        self.assertIs(actual, False)

    async def test_conflict_between_positive_snapshots_resets_stability(self):
        actual, _, _ = await self.confirm([receipt(), receipt(confirmed=False), receipt(), BASELINE])
        self.assertIs(actual, False)

    async def test_page_may_settle_before_two_consecutive_confirmations(self):
        actual, inspect, _ = await self.confirm([BASELINE, BASELINE, receipt(), receipt()])
        self.assertIs(actual, True)
        self.assertEqual(inspect.await_count, 4)

    async def test_ambiguous_page_returns_false_for_existing_reload_fallback(self):
        actual, inspect, _ = await self.confirm([BASELINE] * 4)
        self.assertIs(actual, False)
        self.assertEqual(inspect.await_count, 4)


NODE = shutil.which("node")
DOM_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fixture = input.fixture || {};
const nodes = (fixture.elements || []).map(item => ({
    tagName: (item.tag || 'button').toUpperCase(),
    id: item.id || '',
    classes: (item.className || '').split(/\s+/).filter(Boolean),
    textContent: item.text || '', innerText: item.text || '',
    disabled: Boolean(item.disabled),
    offsetWidth: item.visible === false ? 0 : 100,
    offsetHeight: item.visible === false ? 0 : 20,
    getClientRects: () => item.visible === false ? [] : [{}],
    getAttribute: name => (item.attrs || {})[name] ?? null,
    hasAttribute: name => Object.hasOwn(item.attrs || {}, name) || (name === 'disabled' && Boolean(item.disabled)),
}));
function matches(node, selector) {
    selector = selector.trim();
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector.startsWith('.')) return node.classes.includes(selector.slice(1));
    const bracket = selector.match(/^([a-z]*)\[([\w-]+)(\*=|=)"([^"\]]+)"\]$/i);
    if (bracket) {
        const [, tag, attribute, operator, wanted] = bracket;
        if (tag && node.tagName.toLowerCase() !== tag.toLowerCase()) return false;
        const value = node.getAttribute(attribute);
        return operator === '*=' ? String(value || '').includes(wanted) : value === wanted;
    }
    return node.tagName.toLowerCase() === selector.toLowerCase();
}
const document = {
    body: {innerText: fixture.text ?? 'Thanks for voting!'},
    title: fixture.title || 'Vote for a bot',
    readyState: fixture.readyState || 'complete',
    querySelectorAll: selector => nodes.filter(node => selector.split(',').some(part => matches(node, part))),
    querySelector: selector => nodes.find(node => selector.split(',').some(part => matches(node, part))) || null,
};
const sandbox = {
    document,
    location: new URL(fixture.url || 'https://top.gg/bot/111/vote'),
    URL,
    getComputedStyle: node => ({display: node.offsetWidth ? 'block' : 'none', visibility: 'visible', opacity: '1'}),
    fetch: () => {throw new Error('Receipt observation must not make network requests');},
};
sandbox.window = sandbox;
(async () => {
    const result = await vm.runInNewContext(input.expression, sandbox, {timeout: 1000});
    process.stdout.write(JSON.stringify(result));
})().catch(error => {process.stderr.write(error.stack); process.exitCode = 1;});
"""


@unittest.skipUnless(NODE, "Node.js is needed for real browser JavaScript fixtures")
class VotePageReceiptJavaScriptTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, fixture):
        with patch("vote.evaluate", new=AsyncMock(return_value=None)) as evaluate:
            await vote.vote_page_confirmation(MagicMock(), "111")
        expression = evaluate.await_args.args[1]
        process = await asyncio.to_thread(
            subprocess.run, [NODE, "-e", DOM_HARNESS],
            input=json.dumps({"expression": expression, "fixture": fixture}),
            text=True, capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        raw = json.loads(process.stdout)
        with patch("vote.evaluate", new=AsyncMock(return_value=raw)):
            return await vote.vote_page_confirmation(MagicMock(), "111")

    async def test_matching_page_without_conflicts_exposes_confirmation(self):
        self.assertTrue((await self.inspect({}))["confirmed"])

    async def test_wrong_bot_origin_protocol_and_loading_cannot_confirm(self):
        for fixture in (
            {"url": "https://top.gg/bot/222/vote"},
            {"url": "https://top.gg/bot/111"},
            {"url": "https://top.gg.example.com/bot/111/vote"},
            {"url": "http://top.gg/bot/111/vote"},
            {"readyState": "loading"},
        ):
            with self.subTest(fixture=fixture):
                self.assertFalse((await self.inspect(fixture))["confirmed"])

    async def test_active_vote_button_rejects_even_strong_success_text(self):
        for control in (
            {"tag": "button", "text": "Vote"},
            {"tag": "div", "text": "Vote", "attrs": {"role": "button"}},
        ):
            with self.subTest(control=control):
                self.assertFalse((await self.inspect({"elements": [control]}))["confirmed"])

    async def test_disabled_or_hidden_vote_button_does_not_look_actionable(self):
        for control in (
            {"text": "Vote", "disabled": True},
            {"text": "Vote", "visible": False},
            {"text": "Vote", "attrs": {"aria-disabled": "true"}},
        ):
            with self.subTest(control=control):
                self.assertTrue((await self.inspect({"elements": [control]}))["confirmed"])

    async def test_hidden_interstitial_shell_does_not_override_a_fresh_confirmation(self):
        actual = await self.inspect({
            "elements": [{"tag": "div", "id": "challenge-form", "visible": False}],
        })
        self.assertTrue(actual["confirmed"])

    async def test_challenge_or_login_requirements_override_success_copy(self):
        for fixture in (
            {"title": "Just a moment..."},
            {"elements": [{"tag": "div", "id": "challenge-form"}]},
            {"elements": [{"tag": "iframe", "attrs": {"src": "https://challenges.cloudflare.com/turnstile"}}]},
            {"text": "Thanks for voting! Please solve the captcha to continue"},
            {"text": "Thanks for voting! You must be logged in to vote"},
            {"text": "Thanks for voting! Login to vote"},
        ):
            with self.subTest(fixture=fixture):
                self.assertFalse((await self.inspect(fixture))["confirmed"])

    async def test_vote_error_cannot_be_mistaken_for_success(self):
        actual = await self.inspect({"text": "Thanks for voting! Failed to vote. Please try again."})
        self.assertFalse(actual["confirmed"])


if __name__ == "__main__":
    unittest.main()
