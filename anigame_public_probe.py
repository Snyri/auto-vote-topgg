"""Inspect public AniGame assets only; no sign-in and no vote submission."""

import concurrent.futures
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request


ROOT = "https://www.anigame.gg"


def read(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1000).decode("utf-8", "replace")


def main():
    status, html = read(ROOT + "/vote")
    print(json.dumps({"document_status": status, "length": len(html)}))
    if status != 200:
        print(html[:500])
        return
    paths = sorted(set(re.findall(r'/_next/static/chunks/[^"\\\s<>]+\.js', html)))
    def inspect(path):
        status, text = read(ROOT + path)
        excerpts = []
        if status == 200:
            matches = list(re.finditer(r'/api/|cooldown|vote|claim|turnstile|nextVote|canVote|top\.gg', text, re.I))
            spans = []
            for match in matches:
                left, right = max(0, match.start() - 250), min(len(text), match.end() + 450)
                if spans and left <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], right)
                else:
                    spans.append([left, right])
            excerpts = [text[left:right] for left, right in spans]
        return {"path": path, "status": status, "length": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest(), "excerpts": excerpts}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(inspect, paths):
            if result["excerpts"] or result["status"] != 200:
                print(json.dumps(result))


if __name__ == "__main__":
    main()
