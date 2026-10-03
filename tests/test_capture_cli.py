"""Offline capture CLI contracts, using only generated synthetic input."""

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sushiwait.capture import CaptureError
from sushiwait.cli import main
from sushiwait.credentials import read_credentials_file


NOW = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
PRIVATE_MARKER = "synthetic-private-value"


def synthetic_token(*, expired=False):
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    expiry = NOW - timedelta(seconds=1) if expired else NOW + timedelta(hours=1)
    return ".".join((encoded({"alg": "HS256", "typ": "JWT"}),
                     encoded({"iat": int((NOW-timedelta(minutes=1)).timestamp()),
                              "exp": int(expiry.timestamp()), "sub": PRIVATE_MARKER}),
                     encoded({"synthetic": "signature-only"})))


class CaptureCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.har = self.parent / "synthetic-private-capture.har"
        self.output = self.parent / "synthetic-private-context.json"

    @contextlib.contextmanager
    def local_only(self):
        """Neither offline command may initialize networking, CA or storage."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "SUSHIWAIT_CA_FILE": str(self.parent / "missing-private-ca.pem"),
                "SUSHIWAIT_GATEWAY_AUTHORIZATION": PRIVATE_MARKER,
            }))
            stack.enter_context(patch("sushiwait.cli._utc_clock", return_value=NOW))
            stack.enter_context(patch("sushiwait.capture._clock", return_value=NOW))
            guards = []
            for target in ("sushiwait.cli.client_for", "sushiwait.cli.SushiroClient",
                           "sushiwait.cli.SnapshotStore", "sushiwait.client.SushiroClient.__init__",
                           "sushiwait.client.SushiroClient._default_opener", "ssl.create_default_context",
                           "urllib.request.build_opener", "urllib.request.urlopen", "socket.create_connection"):
                guards.append(stack.enter_context(patch(target, side_effect=AssertionError("must stay offline"))))
            yield
            for guard in guards:
                guard.assert_not_called()

    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        combined = stdout.getvalue() + stderr.getvalue()
        for private in (PRIVATE_MARKER, str(self.har), str(self.output), str(self.parent)):
            self.assertNotIn(private, combined)
        return code, rows, stderr.getvalue()

    def import_arguments(self, *, entry=0, revision=1):
        return ["capture-import", "--har", str(self.har), "--entry-index", str(entry),
                "--output", str(self.output), "--revision", str(revision)]

    def write_synthetic_har(self, *, expired=False):
        token = synthetic_token(expired=expired)
        headers = {"Authorization": "Bearer " + token, "X-App-Client": "synthetic-client",
                   "X-App-Code": "synthetic-code", "User-Agent": "Synthetic Agent/1.0",
                   "Referer": "https://synthetic.invalid/private-reference",
                   "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"log": {"version": "1.2", "creator": {"name": "synthetic", "version": "1"},
            "entries": [{"startedDateTime": (NOW-timedelta(seconds=5)).isoformat(), "time": 100,
                "request": {"method": "GET", "url": "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=900001",
                    "headers": [{"name": key, "value": value} for key, value in headers.items()],
                    "queryString": [{"name": "storeId", "value": "900001"}], "cookies": [],
                    "httpVersion": "HTTP/2.0", "headersSize": -1, "bodySize": 0},
                "response": {"status": 200, "statusText": "OK", "headers": [], "cookies": [],
                    "content": {"size": 1, "mimeType": "application/json", "text": json.dumps({
                        "id": 900001, "name": "合成门店", "groupQueues": {"mixedQueue": ["S001"]}})},
                    "httpVersion": "HTTP/2.0", "redirectURL": "", "headersSize": -1, "bodySize": -1},
                "cache": {}, "timings": {"send": 0, "wait": 100, "receive": 0}}]}}
        self.har.write_text(json.dumps(payload), encoding="utf-8")
        self.har.chmod(0o600)
        return token, headers

    def test_capture_check_dispatches_only_inspection_and_safe_report(self):
        report = {"schema_version": 1, "api_profile": "miniapp_gateway", "network_verified": False,
                  "candidates": [{"entry_index": 3, "store_id": "900001"}]}
        inspection = SimpleNamespace(public_report=Mock(return_value=report), private=PRIVATE_MARKER)
        with self.local_only(), patch("sushiwait.cli.inspect_capture", return_value=inspection) as inspect, \
                patch("sushiwait.cli.write_credentials_from_capture", side_effect=AssertionError("check never writes")) as write:
            code, rows, stderr = self.run_cli(["capture-check", "--har", str(self.har)])
            inspect.assert_called_once_with(str(self.har), now=NOW)
            inspection.public_report.assert_called_once_with()
            write.assert_not_called()
        self.assertEqual((code, rows, stderr), (0, [report], ""))
        self.assertFalse(self.har.exists())
        self.assertFalse(self.output.exists())

    def test_capture_import_leaves_writer_clock_live_with_explicit_selection(self):
        inspection = SimpleNamespace(public_report=Mock(side_effect=AssertionError("import emits writer metadata")))
        metadata = {"written": True, "committed": True, "api_profile": "miniapp_gateway", "revision": 7,
                    "entry_index": 3, "network_verified": False}
        with self.local_only(), patch("sushiwait.cli.inspect_capture", return_value=inspection) as inspect, \
                patch("sushiwait.cli.write_credentials_from_capture", return_value=metadata) as write:
            code, rows, stderr = self.run_cli(self.import_arguments(entry=3, revision=7))
            inspect.assert_called_once_with(str(self.har), now=NOW)
            write.assert_called_once_with(inspection, entry_index=3, destination=str(self.output), revision=7)
            inspection.public_report.assert_not_called()
        self.assertEqual((code, rows, stderr), (0, [metadata], ""))

    def test_capture_errors_are_safe_json_exit_one_without_traceback_or_private_context(self):
        for command in ("capture-check", "capture-import"):
            with self.subTest(command=command):
                error = CaptureError("capture_invalid_json")
                error.__cause__ = ValueError(PRIVATE_MARKER + str(self.har))
                arguments = [command, "--har", str(self.har)] if command == "capture-check" else self.import_arguments()
                with self.local_only(), patch("sushiwait.cli.inspect_capture", side_effect=error), \
                        patch("sushiwait.cli.write_credentials_from_capture") as write:
                    code, rows, stderr = self.run_cli(arguments)
                    write.assert_not_called()
                self.assertEqual(code, 1)
                self.assertEqual(rows, [{"ok": False, "api_profile": "miniapp_gateway",
                                         "error_code": error.error_code, "network_performed": False}])
                self.assertEqual(stderr, "")
                self.assertFalse(self.output.exists())

    def test_import_writer_error_is_safe_and_does_not_fall_through_to_collection(self):
        inspection = object()
        error = CaptureError("capture_invalid_revision")
        with self.local_only(), patch("sushiwait.cli.inspect_capture", return_value=inspection), \
                patch("sushiwait.cli.write_credentials_from_capture", side_effect=error):
            code, rows, stderr = self.run_cli(self.import_arguments(revision=0))
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["error_code"], error.error_code)
        self.assertFalse(rows[0]["network_performed"])
        self.assertEqual(stderr, "")
        self.assertFalse(self.output.exists())

    def test_required_arguments_are_rejected_before_capture_or_other_io(self):
        arguments = (["capture-check"], ["capture-import"],
                     ["capture-import", "--har", str(self.har)],
                     ["capture-import", "--har", str(self.har), "--entry-index", "0", "--revision", "1"],
                     ["capture-import", "--har", str(self.har), "--entry-index", "0", "--output", str(self.output)],
                     ["capture-import", "--har", str(self.har), "--output", str(self.output), "--revision", "1"])
        for argv in arguments:
            with self.subTest(argv=argv), self.local_only(), \
                    patch("sushiwait.cli.inspect_capture", side_effect=AssertionError("parse first")) as inspect, \
                    patch("sushiwait.cli.write_credentials_from_capture", side_effect=AssertionError("parse first")) as write:
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(str(self.har), stderr.getvalue())
                self.assertNotIn(str(self.output), stderr.getvalue())
                inspect.assert_not_called()
                write.assert_not_called()
        self.assertFalse(self.har.exists())
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private capture import")
    def test_generated_capture_import_is_readable_private_context_without_network(self):
        token, headers = self.write_synthetic_har()
        with self.local_only():
            code, rows, stderr = self.run_cli(self.import_arguments(revision=5))
            context = read_credentials_file(self.output, api_profile="miniapp_gateway")
        self.assertEqual(code, 0)
        self.assertTrue(rows[0]["written"])
        self.assertTrue(rows[0]["committed"])
        self.assertFalse(rows[0]["network_verified"])
        self.assertEqual(stderr, "")
        self.assertEqual(context.revision, 5)
        self.assertEqual(context.authorization, "Bearer " + token)
        self.assertEqual(context.app_client, headers["X-App-Client"])
        self.assertEqual(context.app_code, headers["X-App-Code"])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        rendered = json.dumps(rows)
        for value in (token, *headers.values(), "S001"):
            self.assertNotIn(value, rendered)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private capture import")
    def test_expired_generated_capture_import_never_creates_destination(self):
        token, _ = self.write_synthetic_har(expired=True)
        with self.local_only():
            code, rows, stderr = self.run_cli(self.import_arguments())
        self.assertEqual(code, 1)
        self.assertFalse(rows[0]["ok"])
        self.assertFalse(rows[0]["network_performed"])
        self.assertNotIn(token, json.dumps(rows))
        self.assertEqual(stderr, "")
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private capture import")
    def test_invalid_entry_or_revision_rejects_without_destination_or_network(self):
        self.write_synthetic_har()
        for entry, revision in ((-1, 1), (100, 1), (0, 0), (0, 2**63)):
            with self.subTest(entry=entry, revision=revision), self.local_only():
                code, rows, stderr = self.run_cli(self.import_arguments(entry=entry, revision=revision))
            self.assertEqual(code, 1)
            self.assertFalse(rows[0]["ok"])
            self.assertFalse(rows[0]["network_performed"])
            self.assertEqual(stderr, "")
            self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
