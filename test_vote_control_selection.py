"""Execute the production Vote selector against small, local DOM fixtures."""

import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

import vote


NODE = shutil.which("node")
NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const controls = input.controls.map((item, index) => {
    const attributes = {...(item.attributes || {})};
    const tagName = (item.tag || 'button').toLowerCase();
    const nativeButton = tagName === 'button' || tagName === 'input';
    return {
        index,
        tagName: tagName.toUpperCase(),
        textContent: item.text || 'Vote',
        // A disabled attribute does not create a disabled property on div/a.
        disabled: nativeButton ? Boolean(item.disabled || Object.hasOwn(attributes, 'disabled')) : undefined,
        offsetWidth: item.visible === false ? 0 : 100,
        offsetHeight: item.visible === false ? 0 : 20,
        getClientRects: () => item.visible === false ? [] : [{}],
        getAttribute: name => attributes[name] ?? null,
        hasAttribute: name => Object.hasOwn(attributes, name),
        setAttribute: (name, value) => { attributes[name] = String(value); },
        removeAttribute: name => { delete attributes[name]; },
        styleFixture: item.style || {},
    };
});
const matches = (node, selector) => {
    if (selector === node.tagName.toLowerCase()) return true;
    const attribute = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
    if (!attribute) return false;
    return attribute[2] === undefined ? node.hasAttribute(attribute[1]) :
        node.getAttribute(attribute[1]) === attribute[2];
};
const sandbox = {
    document: {
        // querySelectorAll always returns document order, independent of the
        // order of selectors in a comma-separated selector list.
        querySelectorAll: selector => controls.filter(node => selector.split(',').some(
            part => matches(node, part.trim())
        )),
    },
    getComputedStyle: node => ({
        display: 'block', visibility: 'visible', opacity: '1', ...node.styleFixture,
    }),
};
const result = vm.runInNewContext(input.expression, sandbox, {timeout: 1000});
process.stdout.write(JSON.stringify({
    result,
    marked: controls.filter(node => node.getAttribute('data-auto-vote') === '1')
        .map(node => ({index: node.index, tag: node.tagName.toLowerCase()})),
}));
"""


@unittest.skipUnless(NODE, "Node.js is needed to execute browser JavaScript fixtures")
class VoteControlSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def select(self, controls):
        with patch("vote.evaluate", new=AsyncMock(return_value=None)) as evaluate:
            await vote.mark_vote_button(None)
        expression = evaluate.await_args.args[1]
        process = await asyncio.to_thread(
            subprocess.run,
            [NODE, "-e", NODE_HARNESS],
            input=json.dumps({"expression": expression, "controls": controls}),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    async def test_public_vote_link_before_actual_button_is_not_the_click_target(self):
        observed = await self.select([
            {"tag": "a", "attributes": {"href": "/bot/111/vote"}},
            {"tag": "button"},
        ])
        self.assertTrue(observed["result"]["found"])
        self.assertFalse(observed["result"]["disabled"])
        self.assertEqual(observed["marked"], [{"index": 1, "tag": "button"}])
        self.assertEqual(observed["result"]["target_kind"], "native_button")
        self.assertEqual(observed["result"]["candidate_count"], 1)

    async def test_disabled_button_cannot_hide_a_later_enabled_vote_button(self):
        observed = await self.select([
            {"tag": "button", "disabled": True},
            {"tag": "button"},
        ])
        self.assertTrue(observed["result"]["found"])
        self.assertFalse(observed["result"]["disabled"])
        self.assertEqual(observed["marked"], [{"index": 1, "tag": "button"}])

    async def test_navigation_links_alone_do_not_supply_a_vote_click_target(self):
        observed = await self.select([
            {"tag": "a", "attributes": {"href": "/bot/111/vote", "data-auto-vote": "1"}},
            {"tag": "a", "attributes": {"href": "/bot/222/vote"}},
        ])
        self.assertFalse(observed["result"]["found"])
        self.assertTrue(observed["result"]["disabled"])
        self.assertEqual(observed["marked"], [])

    async def test_role_button_with_a_navigation_href_is_not_a_vote_action(self):
        observed = await self.select([
            {"tag": "a", "attributes": {"role": "button", "href": "/bot/111/vote"}},
        ])
        self.assertFalse(observed["result"]["found"])
        self.assertTrue(observed["result"]["disabled"])
        self.assertEqual(observed["marked"], [])

    async def test_role_button_with_a_placeholder_href_can_be_a_vote_action(self):
        observed = await self.select([
            {"tag": "a", "attributes": {"role": "button", "href": "#"}},
        ])
        self.assertTrue(observed["result"]["found"])
        self.assertFalse(observed["result"]["disabled"])
        self.assertEqual(observed["marked"], [{"index": 0, "tag": "a"}])
        self.assertEqual(observed["result"]["target_kind"], "role_button")

    async def test_role_button_disabled_attributes_are_respected_when_selecting(self):
        for disabled_attributes in ({"disabled": ""}, {"aria-disabled": "true"}):
            with self.subTest(attributes=disabled_attributes):
                disabled_control = {
                    "tag": "div",
                    "attributes": {"role": "button", **disabled_attributes},
                }
                only_disabled = await self.select([disabled_control])
                self.assertTrue(only_disabled["result"]["disabled"])

                observed = await self.select([
                    disabled_control,
                    {"tag": "div", "attributes": {"role": "button"}},
                ])
                self.assertTrue(observed["result"]["found"])
                self.assertFalse(observed["result"]["disabled"])
                self.assertEqual(observed["marked"], [{"index": 1, "tag": "div"}])


class VoteTargetDiagnosticTests(unittest.TestCase):
    def test_diagnostic_never_echoes_arbitrary_page_values(self):
        self.assertEqual(vote.vote_target_diagnostic({
            "target_kind": "private-value", "candidate_count": "private-value",
        }), "Vote target selected: kind=unknown, candidates=unknown")
        for value in (True, [], {}, None):
            self.assertEqual(vote.vote_target_diagnostic({
                "target_kind": value, "candidate_count": value,
            }), "Vote target selected: kind=unknown, candidates=unknown")

    def test_valid_categories_and_bounded_counts(self):
        for count, expected in ((2, 2), (100, 20), (-1, 0)):
            self.assertEqual(vote.vote_target_diagnostic({
                "target_kind": "native_button", "candidate_count": count,
            }), f"Vote target selected: kind=native_button, candidates={expected}")


if __name__ == "__main__":
    unittest.main()
