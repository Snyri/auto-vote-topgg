#!/usr/bin/env python3
"""Fail CI when locked PyPI packages have known OSV advisories."""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_ATTEMPTS = 3
OSV_RETRY_DELAYS_SEC = (1, 2)
OSV_MAX_PAGES = 20
LOCK_ENTRY = re.compile(r"([A-Za-z0-9_.-]+)==([^\s\\;]+)")
LOCK_HASH = re.compile(r"--hash=sha256:[0-9a-fA-F]{64}")


def parse_lock(path: str) -> list[tuple[str, str]]:
    content = Path(path).read_text(encoding="utf-8")
    packages = []
    seen = set()
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip().removesuffix("\\").strip()
        if not line or LOCK_HASH.fullmatch(line):
            continue
        match = LOCK_ENTRY.fullmatch(line)
        if not match:
            raise ValueError(f"Unsupported lock entry on line {line_number}; expected an exact package pin")
        name, version = match.groups()
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in seen:
            raise ValueError(f"Duplicate locked package: {name}")
        seen.add(normalized_name)
        packages.append((name, version))
    if not packages:
        raise ValueError("Lock file contains no pinned packages")
    return packages


def _query_osv_batch(queries: list[dict]) -> list[dict]:
    request = urllib.request.Request(
        OSV_QUERY_BATCH,
        data=json.dumps({"queries": queries}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(1, OSV_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise RuntimeError("OSV returned an invalid response")
            results = payload["results"]
            if len(results) != len(queries):
                raise RuntimeError("OSV returned an incomplete response")
            for result in results:
                if not isinstance(result, dict) or set(result) - {"vulns", "next_page_token"}:
                    raise RuntimeError("OSV returned an invalid query result")
                vulnerabilities = result.get("vulns", [])
                if not isinstance(vulnerabilities, list) or any(
                    not isinstance(vulnerability, dict)
                    or not isinstance(vulnerability.get("id"), str)
                    or not vulnerability["id"].strip()
                    for vulnerability in vulnerabilities
                ):
                    raise RuntimeError("OSV returned an invalid vulnerability list")
                if not isinstance(result.get("next_page_token", ""), str):
                    raise RuntimeError("OSV returned an invalid page token")
            return results
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt == OSV_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == OSV_ATTEMPTS:
                raise

        time.sleep(OSV_RETRY_DELAYS_SEC[attempt - 1])

    raise RuntimeError("OSV query retry loop exited unexpectedly")


def query_osv(packages: list[tuple[str, str]]) -> list[dict]:
    queries = [
        {"package": {"ecosystem": "PyPI", "name": name}, "version": version}
        for name, version in packages
    ]
    combined: list[dict] = [{} for _ in packages]
    pending = list(enumerate(queries))
    seen_tokens: list[set[str]] = [set() for _ in packages]
    for _ in range(OSV_MAX_PAGES):
        if not pending:
            return combined
        results = _query_osv_batch([query for _, query in pending])
        next_pending = []
        for (index, query), result in zip(pending, results):
            if result.get("vulns"):
                combined[index].setdefault("vulns", []).extend(result["vulns"])
            token = result.get("next_page_token")
            if token:
                if token in seen_tokens[index]:
                    raise RuntimeError("OSV returned a repeated page token")
                seen_tokens[index].add(token)
                next_pending.append((index, {**query, "page_token": token}))
        pending = next_pending
    if pending:
        raise RuntimeError("OSV pagination limit exceeded; audit is incomplete")
    return combined


def main(path: str = "requirements.lock") -> int:
    packages = parse_lock(path)
    findings = []
    for (name, version), result in zip(packages, query_osv(packages)):
        for vulnerability in result.get("vulns", []):
            findings.append(f"{name}=={version}: {vulnerability['id']}")
    if findings:
        print("Known dependency vulnerabilities found:")
        print("\n".join(findings))
        return 1
    print(f"OSV: no known vulnerabilities in {len(packages)} locked packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements.lock"))
