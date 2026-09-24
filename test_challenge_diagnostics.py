"""Challenge diagnostics are bounded, credential-free and observational only."""

import ast
import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import vote


BOOLEAN_FIELDS = {
    "title_just_moment", "title_attention", "body_security", "body_human",
    "gate_present", "gate_visible", "widget_present", "response_present",
    "vote_visible", "vote_enabled", "login_visible",
}
DIAGNOSTIC_FIELDS = BOOLEAN_FIELDS | {"ready_state"}


def captured_output(printer):
    return "\n".join(" ".join(str(value) for value in call.args) for call in printer.call_args_list)


def diagnostic_payload(output):
    """Accept JSON or a printed dictionary without depending on its log prefix."""
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end < start:
        raise AssertionError("Expected one structured diagnostic record")
    payload = output[start:end + 1]
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return ast.literal_eval(payload)


class ChallengeDiagnosticLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_logging_allowlist_discards_secrets_and_requires_real_booleans(self):
        secret = "private-cookie-value-must-never-appear"
        raw = {
            "ready_state": "complete",
            "gate_present": True,
            "gate_visible": secret,
            "widget_present": 1,
            "response_present": "true",
            "vote_visible": [],
            "vote_enabled": {"secret": secret},
            "login_visible": False,
            "raw_title": secret,
            "url": "https://top.gg/?token=" + secret,
            "cookies": [secret],
            "body": secret,
        }
        with (
            patch("builtins.print") as printer,
            patch("vote.challenge_diagnostic", new=AsyncMock(return_value=raw)),
        ):
            await vote.log_challenge_diagnostic(MagicMock(), "timeout")

        output = captured_output(printer)
        self.assertNotIn(secret, output)
        payload = diagnostic_payload(output)
        self.assertLessEqual(set(payload), DIAGNOSTIC_FIELDS | {"phase"})
        self.assertIs(payload["gate_present"], True)
        self.assertEqual(payload["ready_state"], "complete")
        for field in BOOLEAN_FIELDS:
            if payload.get(field) is not None:
                self.assertIs(type(payload[field]), bool)
        for field in ("gate_visible", "widget_present", "response_present", "vote_visible", "vote_enabled"):
            self.assertIsNone(payload.get(field))

    async def test_unknown_ready_state_never_logs_its_original_value(self):
        secret = "account-name-and-session-value"
        for ready_state in (secret, True, 1, {"secret": secret}, [secret]):
            with self.subTest(type=type(ready_state).__name__):
                with (
                    patch("builtins.print") as printer,
                    patch("vote.challenge_diagnostic", new=AsyncMock(return_value={"ready_state": ready_state})),
                ):
                    await vote.log_challenge_diagnostic(MagicMock(), "timeout")
                output = captured_output(printer)
                self.assertNotIn(secret, output)
                self.assertEqual(diagnostic_payload(output)["ready_state"], "unknown")

    async def test_diagnostic_failure_does_not_interrupt_or_log_exception_details(self):
        secret = "sensitive-page-or-cookie-value"
        for error in (RuntimeError(secret), ValueError(secret), TimeoutError(secret)):
            with self.subTest(error=type(error).__name__):
                with (
                    patch("builtins.print") as printer,
                    patch("vote.challenge_diagnostic", new=AsyncMock(side_effect=error)),
                ):
                    await vote.log_challenge_diagnostic(MagicMock(), "error")
                self.assertNotIn(secret, captured_output(printer))

    async def test_malformed_payload_and_unexpected_phase_cannot_leak_values(self):
        secret = "private-diagnostic-value"
        for raw in (secret, [secret], True, None):
            with self.subTest(type=type(raw).__name__):
                with (
                    patch("builtins.print") as printer,
                    patch("vote.challenge_diagnostic", new=AsyncMock(return_value=raw)),
                ):
                    await vote.log_challenge_diagnostic(MagicMock(), secret)
                self.assertNotIn(secret, captured_output(printer))
        with (
            patch("builtins.print") as printer,
            patch("vote.challenge_diagnostic", new=AsyncMock(return_value={"ready_state": "complete"})),
        ):
            await vote.log_challenge_diagnostic(MagicMock(), secret)
        payload = diagnostic_payload(captured_output(printer))
        self.assertEqual(payload["phase"], "unknown")

    async def test_hung_diagnostic_is_cancelled_within_its_two_second_budget(self):
        original_wait_for = asyncio.wait_for
        cancelled = asyncio.Event()
        timeouts = []

        async def hanging_diagnostic(_tab):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def accelerated_wait_for(awaitable, timeout):
            timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=0.01)

        with (
            patch("builtins.print"),
            patch("vote.challenge_diagnostic", new=hanging_diagnostic),
            patch("vote.asyncio.wait_for", new=accelerated_wait_for),
        ):
            await original_wait_for(vote.log_challenge_diagnostic(MagicMock(), "timeout"), timeout=0.5)

        self.assertEqual(len(timeouts), 1)
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], 2)
        self.assertTrue(cancelled.is_set())


NODE = shutil.which("node")
DOM_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fixture = input.fixture || {};
const nodes = (fixture.elements || []).map(item => ({
    tagName: item.tag || 'div',
    id: item.id || '',
    classList: (item.className || '').split(/\s+/).filter(Boolean),
    textContent: item.text || '',
    innerText: item.text || '',
    value: item.value || '',
    dataset: item.dataset || {},
    diagnosticStyle: item.style || {},
    disabled: Boolean(item.disabled),
    offsetWidth: item.visible === false ? 0 : 100,
    offsetHeight: item.visible === false ? 0 : 20,
    getClientRects: () => item.visible === false ? [] : [{}],
    getBoundingClientRect: () => ({width: item.visible === false ? 0 : 100, height: item.visible === false ? 0 : 20}),
    getAttribute: name => (item.attrs || {})[name] ?? (name === 'id' ? item.id ?? null : null),
    hasAttribute: name => Object.hasOwn(item.attrs || {}, name) || (name === 'disabled' && Boolean(item.disabled)),
}));
function matches(node, selector) {
    selector = selector.trim();
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector.startsWith('.')) return node.classList.includes(selector.slice(1));
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
    title: fixture.title || 'Vote for a bot',
    readyState: fixture.ready_state || 'complete',
    body: {innerText: fixture.body || ''},
    querySelectorAll: selector => nodes.filter(node => selector.split(',').some(part => matches(node, part))),
    querySelector: selector => nodes.find(node => selector.split(',').some(part => matches(node, part))) || null,
};
const sandbox = {
    document,
    location: {protocol: 'https:', hostname: 'top.gg'},
    getComputedStyle: node => ({
        display: node.offsetWidth ? 'block' : 'none',
        visibility: node.offsetWidth ? 'visible' : 'hidden',
        opacity: '1',
        ...node.diagnosticStyle,
    }),
};
sandbox.window = sandbox;
(async () => {
    const result = await vm.runInNewContext(input.expression, sandbox, {timeout: 1000});
    process.stdout.write(JSON.stringify(result));
})().catch(error => {
    process.stderr.write(error.stack);
    process.exitCode = 1;
});
"""


@unittest.skipUnless(NODE, "Node.js is needed to execute browser diagnostic fixtures")
class ChallengeDiagnosticJavaScriptTests(unittest.IsolatedAsyncioTestCase):
    async def execute_diagnostic(self, fixture):
        with patch("vote.evaluate", new=AsyncMock(return_value={})) as evaluate:
            await vote.challenge_diagnostic(MagicMock())
        expression = evaluate.await_args.args[1]
        process = await asyncio.to_thread(
            subprocess.run,
            [NODE, "-e", DOM_HARNESS],
            input=json.dumps({"expression": expression, "fixture": fixture}),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(set(result), DIAGNOSTIC_FIELDS)
        self.assertTrue(all(type(result[field]) is bool for field in BOOLEAN_FIELDS))
        return result

    async def test_visible_gate_reports_independent_title_body_and_dom_evidence(self):
        result = await self.execute_diagnostic({
            "title": "Just a moment...",
            "ready_state": "interactive",
            "body": "Performing security verification. Verify you are human.",
            "elements": [{"id": "challenge-form", "visible": True}],
        })
        self.assertEqual(result["ready_state"], "interactive")
        for field in ("title_just_moment", "body_security", "body_human", "gate_present", "gate_visible"):
            self.assertTrue(result[field], field)
        self.assertFalse(result["title_attention"])
        self.assertFalse(result["vote_visible"])
        self.assertFalse(result["vote_enabled"])

    async def test_hidden_residual_gate_is_distinguishable_from_live_vote_controls(self):
        result = await self.execute_diagnostic({
            "elements": [
                {"id": "challenge-form", "visible": False},
                {"tag": "button", "text": "Vote", "visible": True},
                {"tag": "a", "text": "Login", "visible": True},
            ],
        })
        self.assertTrue(result["gate_present"])
        self.assertFalse(result["gate_visible"])
        self.assertTrue(result["vote_visible"])
        self.assertTrue(result["vote_enabled"])
        self.assertTrue(result["login_visible"])
        self.assertFalse(result["title_just_moment"])
        self.assertFalse(result["body_security"])

    async def test_widget_and_response_presence_never_return_the_response_value(self):
        secret = "private-challenge-response-value-123456789"
        result = await self.execute_diagnostic({
            "title": "User private-name voting page",
            "body": "private-name logged in",
            "elements": [
                {"tag": "iframe", "attrs": {"src": "https://challenges.cloudflare.com/turnstile"}},
                {"tag": "input", "attrs": {"name": "cf-turnstile-response"}, "value": secret},
            ],
        })
        self.assertTrue(result["widget_present"])
        self.assertTrue(result["response_present"])
        self.assertFalse(result["gate_present"])
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn("private-name", json.dumps(result))

    async def test_css_hidden_gate_is_not_reported_visible_even_with_layout_rectangles(self):
        for style in ({"display": "none"}, {"visibility": "hidden"}, {"visibility": "collapse"}, {"opacity": "0"}):
            with self.subTest(style=style):
                result = await self.execute_diagnostic({
                    "elements": [{"id": "challenge-stage", "visible": True, "style": style}],
                })
                self.assertTrue(result["gate_present"])
                self.assertFalse(result["gate_visible"])

    async def test_disabled_hidden_and_navigation_controls_do_not_look_vote_ready(self):
        for control, visible in (
            ({"tag": "button", "text": "Vote", "disabled": True}, True),
            ({"tag": "button", "text": "Vote", "attrs": {"aria-disabled": "true"}}, True),
            ({"tag": "button", "text": "Vote", "visible": False}, False),
            ({"tag": "a", "text": "Vote"}, False),
        ):
            with self.subTest(control=control):
                result = await self.execute_diagnostic({"elements": [control]})
                self.assertEqual(result["vote_visible"], visible)
                self.assertFalse(result["vote_enabled"])


if __name__ == "__main__":
    unittest.main()
