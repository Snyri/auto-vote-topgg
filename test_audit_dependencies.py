import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

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


if __name__ == "__main__":
    unittest.main()
