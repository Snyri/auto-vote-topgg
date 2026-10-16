"""Inspect public AniGame client code in an empty browser; never sign in or vote."""

import asyncio
import hashlib
import json
import re
from urllib.parse import urlparse

import nodriver as uc
import vote


async def main():
    browser = await vote.start_browser(initial_url="https://www.anigame.gg/vote")
    pending, results, seen = set(), [], set()

    async def inspect(event):
        parsed = urlparse(event.url)
        if parsed.hostname not in {"anigame.gg", "www.anigame.gg"} or not parsed.path.startswith("/_next/static/chunks/") or event.script_id in seen:
            return
        seen.add(event.script_id)
        task = asyncio.current_task()
        pending.add(task)
        try:
            source = await tab.send(uc.cdp.debugger.get_script_source(event.script_id))
            text = source[0] if isinstance(source, tuple) else source
            spans = []
            for match in re.finditer(r'/api/|cooldown|vote|claim|turnstile|nextVote|canVote|top\.gg', text, re.I):
                left, right = max(0, match.start() - 250), min(len(text), match.end() + 500)
                if spans and left <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], right)
                else:
                    spans.append([left, right])
            excerpts = [text[left:right] for left, right in spans]
            if excerpts:
                results.append({"path": parsed.path, "length": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest(), "excerpts": excerpts})
        finally:
            pending.discard(task)

    try:
        tab = browser.tabs[0]
        if "anigame.gg/vote" not in await vote.current_url(tab):
            await vote.navigate_page(tab, "https://www.anigame.gg/vote")
        await asyncio.sleep(10)
        print(json.dumps({"public_page_text": (await vote.body_text(tab))[:2500]}))
        resources = await vote.evaluate(tab, "Array.from(document.querySelectorAll('script[src],link[as=script]')).map(el => el.src || el.href)")
        for url in dict.fromkeys(resources):
            parsed = urlparse(url)
            if parsed.hostname not in {"anigame.gg", "www.anigame.gg"} or not parsed.path.startswith("/_next/static/chunks/"):
                continue
            resource = await vote.evaluate(tab, "fetch(" + json.dumps(url) + ").then(async r => ({status:r.status, source:await r.text()}))")
            text = resource.get("source", "")
            if resource.get("status") == 200 and ("/api/vote/" in text or 'rel:\"preload\"' in text or 'VotePage' in text):
                print(json.dumps({"public_asset": parsed.path, "status": resource["status"], "source": text}))
        tab.add_handler(uc.cdp.debugger.ScriptParsed, inspect)
        await tab.send(uc.cdp.debugger.enable())
        await asyncio.sleep(5)
        if pending:
            await asyncio.wait_for(asyncio.gather(*list(pending)), 20)
        for result in sorted(results, key=lambda item: item["path"]):
            print(json.dumps(result))
    finally:
        await vote.close_browser(browser)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
