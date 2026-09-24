import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import audit_dependencies as audit


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))

    def __exit__(self, exc_type, exc, tb):
        return False


class DependencyAuditTests(unittest.TestCase):
    def test_parse_lock_requires_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.lock"
            path.write_text("requests==2.34.2\n", encoding="utf-8")
            self.assertEqual(audit.parse_lock(str(path)), [("requests", "2.34.2")])

    def test_parse_lock_rejects_silently_skipped_dependencies(self):
        for unsupported in ("unlocked-package>=1", "-r additional.lock", "package @ https://example.invalid/pkg.whl"):
            with self.subTest(unsupported=unsupported), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "requirements.lock"
                path.write_text(f"requests==2.34.2\n{unsupported}\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Unsupported lock entry"):
                    audit.parse_lock(str(path))

    def test_parse_lock_accepts_comments_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.lock"
            path.write_text("# locked dependencies\nrequests==2.34.2 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
            self.assertEqual(audit.parse_lock(str(path)), [("requests", "2.34.2")])

    def test_parse_lock_rejects_duplicate_normalized_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.lock"
            path.write_text("some_package==1.0\nSome-Package==2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                audit.parse_lock(str(path))

    @patch("audit_dependencies.time.sleep")
    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_retries_transient_network_error(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.URLError("temporary"),
            FakeResponse({"results": [{}]}),
        ]

        self.assertEqual(audit.query_osv([("requests", "2.34.2")]), [{}])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(audit.OSV_RETRY_DELAYS_SEC[0])

    @patch("audit_dependencies.time.sleep")
    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_does_not_retry_non_retryable_http_error(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.HTTPError(
            audit.OSV_QUERY_BATCH,
            400,
            "bad request",
            {},
            None,
        )

        with self.assertRaises(urllib.error.HTTPError):
            audit.query_osv([("requests", "2.34.2")])
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_rejects_incomplete_response(self, urlopen):
        urlopen.return_value = FakeResponse({"results": []})
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            audit.query_osv([("requests", "2.34.2")])

    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_follows_only_paginated_packages_and_preserves_order(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"results": [{}, {"next_page_token": "second-page"}]}),
            FakeResponse({"results": [{"vulns": [{"id": "TEST-ADVISORY"}]}]}),
        ]
        self.assertEqual(
            audit.query_osv([("first", "1"), ("second", "2")]),
            [{}, {"vulns": [{"id": "TEST-ADVISORY"}]}],
        )
        second_request = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(second_request, {"queries": [{
            "package": {"ecosystem": "PyPI", "name": "second"},
            "version": "2",
            "page_token": "second-page",
        }]})

    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_rejects_repeated_page_tokens(self, urlopen):
        urlopen.return_value = FakeResponse({"results": [{"next_page_token": "same-page"}]})
        with self.assertRaisesRegex(RuntimeError, "repeated page token"):
            audit.query_osv([("requests", "2.34.2")])
        self.assertEqual(urlopen.call_count, 2)

    @patch("audit_dependencies.OSV_MAX_PAGES", 2)
    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_rejects_unbounded_pagination(self, urlopen):
        urlopen.side_effect = [
            FakeResponse({"results": [{"next_page_token": "page-two"}]}),
            FakeResponse({"results": [{"next_page_token": "page-three"}]}),
        ]
        with self.assertRaisesRegex(RuntimeError, "pagination limit"):
            audit.query_osv([("requests", "2.34.2")])

    @patch("audit_dependencies.urllib.request.urlopen")
    def test_query_osv_rejects_invalid_response_instead_of_reporting_clean(self, urlopen):
        for payload in (
            [], {"results": {}}, {"results": [None]},
            {"results": [{"error": "upstream unavailable"}]},
            {"results": [{"vulns": {}}]},
            {"results": [{"vulns": [{}]}]},
            {"results": [{"vulns": [{"id": ""}]}]},
            {"results": [{"next_page_token": 42}]},
        ):
            with self.subTest(payload=payload):
                urlopen.return_value = FakeResponse(payload)
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    audit.query_osv([("requests", "2.34.2")])

    @patch("audit_dependencies.query_osv", return_value=[{"vulns": [{"id": "TEST-ADVISORY"}]}])
    @patch("audit_dependencies.parse_lock", return_value=[("requests", "2.34.2")])
    @patch("builtins.print")
    def test_main_fails_when_vulnerability_found(self, output, parse_lock, query_osv):
        self.assertEqual(audit.main(), 1)
        output.assert_any_call("requests==2.34.2: TEST-ADVISORY")


if __name__ == "__main__":
    unittest.main()
