"""Synthetic/offline safety and semantics of the independent anonymous source."""
from dataclasses import asdict
import copy
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs

from sushiwait.cli import main
from sushiwait.remote import (RemoteClient, RemoteStore, QUEUE_NAMES, ENDPOINTS,
                             collect_remote, validate_record)


def queues():
    return {name: [] for name in QUEUE_NAMES} | {"boothQueue": ["123", "123", "124-1"],
        "mixedQueue": ["124-1", "123"], "storeQueue": ["90", "123", "80"]}


class Response:
    def __init__(self, body, request, *, status=200, headers=None, url=None):
        self.body, self.status, self.headers = body, status, headers or {}
        self.url = request.full_url if url is None else url
        self.closed = False
        self.read_sizes = []
    def getcode(self): return self.status
    def geturl(self): return self.url
    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]
    def close(self): self.closed = True


class Opener:
    def __init__(self, values=None, *, bodies=None, statuses=None, headers=None, url=None, error=None):
        self.values, self.bodies = values or [queues(), 12], bodies
        self.statuses, self.headers, self.url, self.error = statuses, headers, url, error
        self.requests, self.responses = [], []
    def open(self, request, *, timeout):
        index = len(self.requests)
        self.requests.append((request, timeout))
        if self.error: raise self.error
        body = self.bodies[index] if self.bodies is not None else json.dumps(self.values[index]).encode()
        response = Response(body, request, status=self.statuses[index] if self.statuses else 200,
                            headers=self.headers, url=self.url)
        self.responses.append(response)
        return response


class RemoteTransportTests(unittest.TestCase):
    def test_honest_fixed_two_gets_without_credentials(self):
        opener = Opener()
        result = RemoteClient(opener=opener).snapshot("3014")
        self.assertTrue(result["ok"])
        self.assertFalse(result["atomic_snapshot"])
        self.assertFalse(result["response_store_identity_verified"])
        self.assertEqual(result["queries"]["groupqueues"]["payload"]["queues"], queues())
        self.assertEqual(result["queries"]["storequeuecount"]["payload"], {"raw_count": 12, "unit": "unknown"})
        self.assertEqual(validate_record(result), result)
        for endpoint, (request, timeout) in zip(ENDPOINTS, opener.requests):
            parts = urlsplit(request.full_url)
            self.assertEqual(parts.scheme, "https")
            self.assertEqual(parts.hostname, "crm-cn-prd.sushiro.com.cn")
            self.assertEqual(parts.path, "/api/1.1/remote/" + endpoint)
            self.assertEqual(parse_qs(parts.query), {"storeid": ["3014"]})
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(timeout, 15)
            self.assertIsNone(request.data)
            self.assertEqual({key.lower() for key in request.headers}, {"accept", "accept-encoding", "user-agent"})
            self.assertTrue(request.get_header("User-agent").startswith("SUSHIWAIT/"))
        self.assertTrue(all(r.closed for r in opener.responses))

    def test_invalid_route_or_id_has_no_network(self):
        opener = Opener()
        client = RemoteClient(opener=opener)
        for endpoint, identifier in (("cancel", "3014"), ("groupqueues", "0"), ("groupqueues", "03014"), ("groupqueues", "１"), ("groupqueues", "1&foo=2")):
            self.assertFalse(client.fetch(endpoint, identifier).attempted)
        self.assertEqual(opener.requests, [])

    def test_bounds(self):
        for values in ({"timeout_seconds": True}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 16},
                       {"max_response_bytes": True}, {"max_response_bytes": 0}, {"max_response_bytes": 2**22}):
            with self.subTest(values=values), self.assertRaises(ValueError): RemoteClient(**values)

    def test_first_failure_skips_second_get_and_preserves_failure(self):
        opener = Opener(statuses=[401])
        result = RemoteClient(opener=opener).snapshot("3014")
        self.assertFalse(result["ok"])
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(result["queries"]["storequeuecount"]["error_code"], "preceding_query_failed")
        self.assertEqual(validate_record(result), result)

    def test_count_failure_retains_public_first_response(self):
        result = RemoteClient(opener=Opener(values=[queues(), True])).snapshot("3014")
        self.assertFalse(result["ok"])
        self.assertEqual(result["queries"]["groupqueues"]["payload"]["queues"], queues())
        self.assertEqual(validate_record(result), result)

    def test_unknown_payload_contents_are_not_retained(self):
        value = queues() | {"authorization": "private-value", "personal": {"phone": "hidden"}}
        result = RemoteClient(opener=Opener(values=[value])).fetch("groupqueues", "3014")
        self.assertNotIn("private-value", json.dumps(asdict(result)))
        self.assertEqual(result.payload, {"queues": queues()})

    def test_business_error_is_not_empty_queue_success(self):
        for extra in ({"success": False}, {"code": 401}, {"error": "private-value"}):
            result = RemoteClient(opener=Opener(values=[queues() | extra])).fetch("groupqueues", "3014")
            self.assertEqual(result.error_code, "business_error")
            self.assertIsNone(result.payload)
            self.assertNotIn("private-value", json.dumps(asdict(result)))

    def test_missing_null_invalid_and_oversized_arrays_fail(self):
        for replacement in (None, "123", ["unsafe-secret"], ["1"] * 101, [1], ["123\n"]):
            value = queues() | {"storeQueue": replacement}
            with self.subTest(value=replacement):
                result = RemoteClient(opener=Opener(values=[value])).fetch("groupqueues", "3014")
                self.assertEqual(result.error_code, "unsupported_queue_schema")
                self.assertIsNone(result.payload)
        value = queues(); del value["counterQueue"]
        self.assertEqual(RemoteClient(opener=Opener(values=[value])).fetch("groupqueues", "3014").error_code, "unsupported_queue_schema")

    def test_count_zero_valid_and_non_integer_invalid(self):
        self.assertEqual(RemoteClient(opener=Opener(values=[0])).fetch("storequeuecount", "3014").payload["raw_count"], 0)
        for value in (True, 1.5, -1, 1000001, None, [], {}, "12"):
            with self.subTest(value=value):
                self.assertEqual(RemoteClient(opener=Opener(values=[value])).fetch("storequeuecount", "3014").error_code, "unsupported_count_schema")

    def test_duplicate_nonfinite_bad_utf8_json_fail(self):
        for body in (b'{"a":1,"a":2}', b'NaN', b'\xff', b'{'):
            self.assertEqual(RemoteClient(opener=Opener(bodies=[body])).fetch("groupqueues", "3014").error_code, "invalid_json")

    def test_response_limit_declared_and_actual(self):
        for opener in (Opener(headers={"Content-Length": "99"}), Opener(bodies=[b"x" * 99])):
            self.assertEqual(RemoteClient(opener=opener, max_response_bytes=10).fetch("groupqueues", "3014").error_code, "response_too_large")
            self.assertTrue(opener.responses[0].closed)
            self.assertTrue(all(size <= 11 for size in opener.responses[0].read_sizes))

    def test_redirects_and_url_drift_never_accepted(self):
        for opener in (Opener(statuses=[302]), Opener(url="https://other.example/")):
            self.assertEqual(RemoteClient(opener=opener).fetch("groupqueues", "3014").error_code, "redirect_blocked")
            self.assertEqual(len(opener.requests), 1)

    def test_network_errors_are_fixed_categories_without_details(self):
        for error, expected in ((TimeoutError("private-value"), "timeout"), (ssl.SSLCertVerificationError("private-value"), "tls_verification_failed"),
                (URLError(ssl.SSLCertVerificationError("private-value")), "tls_verification_failed"),
                (OSError("private-value"), "network_error")):
            result = RemoteClient(opener=Opener(error=error)).fetch("groupqueues", "3014")
            self.assertEqual(result.error_code, expected)
            self.assertNotIn("private-value", json.dumps(asdict(result)))

    def test_default_transport_keeps_tls_and_ignores_environment_proxy(self):
        opener = Opener()
        with patch("sushiwait.remote.build_opener", return_value=opener) as build, patch("sushiwait.remote.ssl.create_default_context", wraps=ssl.create_default_context) as context:
            RemoteClient().fetch("groupqueues", "3014")
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertEqual(context.call_count, 1)
        self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(handlers[2]._context.check_hostname)
        self.assertEqual(opener.addheaders, [])


class RemoteStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name).resolve() / "remote.sqlite3"
        self.record = RemoteClient(opener=Opener()).snapshot("3014")

    def test_private_roundtrip_read_only_report_does_not_write_or_query(self):
        with RemoteStore(self.path) as db:
            db.append(self.record)
            changed = copy.deepcopy(self.record)
            changed["queries"]["groupqueues"]["payload"]["queues"]["storeQueue"] = ["124", "90"]
            db.append(changed)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("socket.socket", side_effect=AssertionError("network")), RemoteStore(self.path, read_only=True) as db:
            report = db.report("3014")
            self.assertEqual(report["successful_pairs"], 2)
            self.assertEqual(report["queue_changes"]["storeQueue"], 1)
            self.assertEqual(report["queue_changes"]["boothQueue"], 0)
            self.assertFalse(report["eta_available"])
            self.assertEqual(db.report("3004")["rows_examined"], 0)
            with self.assertRaises(ValueError): db.append(self.record)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_existing_other_schema_rejected_without_modification(self):
        connection = sqlite3.connect(self.path); connection.execute("CREATE TABLE samples(id INTEGER)"); connection.commit(); connection.close()
        self.path.chmod(0o600)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): RemoteStore(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_symlink_hardlink_and_public_mode_rejected(self):
        self.path.write_bytes(b""); self.path.chmod(0o600)
        target = self.path.with_name("link.sqlite3"); target.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)): RemoteStore(target)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): RemoteStore(self.path)
        self.path.chmod(0o600)
        import os
        target.unlink(); os.link(self.path, target)
        with self.assertRaises(ValueError): RemoteStore(self.path)

    def test_bad_scope_or_invented_semantics_rejected(self):
        for field, value in (("source", "miniapp_gateway"), ("atomic_snapshot", True), ("response_store_identity_verified", True), ("source_freshness", "fresh"), ("verified_training_labels", 1), ("ok", False)):
            changed = copy.deepcopy(self.record); changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): validate_record(changed)

    def test_failed_count_is_not_zero_and_latest_failure_is_visible(self):
        failed = RemoteClient(opener=Opener(values=[queues(), None])).snapshot("3014")
        with RemoteStore(self.path) as db:
            db.append(self.record); db.append(failed)
            report = db.report("3014", limit=1)
        self.assertTrue(report["window_truncated"])
        self.assertEqual(report["failed_pairs"], 1)
        self.assertIsNone(report["latest_record"]["queries"]["storequeuecount"]["payload"])

    def test_database_replacement_and_concurrent_writer_stop(self):
        with RemoteStore(self.path) as db:
            with self.assertRaises((ValueError, OSError)): RemoteStore(self.path)
            self.path.rename(self.path.with_suffix(".old"))
            self.path.write_bytes(b""); self.path.chmod(0o600)
            with self.assertRaises(ValueError): db.append(self.record)

    def test_tampered_record_and_bounds_rejected(self):
        with RemoteStore(self.path) as db:
            db.append(self.record)
            for limit in (0, True, 10001):
                with self.assertRaises(ValueError): db.report("3014", limit=limit)
            db.db.execute("UPDATE remote_samples SET payload_json='{}'"); db.db.commit()
            with self.assertRaises(ValueError): db.report("3014")


class RemoteCollectionTests(unittest.TestCase):
    def test_stop_after_failure_no_retry_or_other_store(self):
        from unittest.mock import Mock
        record = RemoteClient(opener=Opener(statuses=[429])).snapshot("3014")
        client, db = Mock(), Mock()
        client.snapshot.return_value = record
        summary = collect_remote(client, db, ["3014", "3004"], samples=10)
        self.assertFalse(summary["ok"])
        self.assertEqual(client.snapshot.call_args_list, [unittest.mock.call("3014")])
        self.assertEqual(db.append.call_count, 1)

    def test_slow_round_waits_full_interval_without_catchup(self):
        from unittest.mock import Mock
        client, db = Mock(), Mock()
        client.snapshot.return_value = RemoteClient(opener=Opener()).snapshot("3014")
        with patch("sushiwait.remote.time.monotonic", side_effect=[0, 0, 31, 31, 31, 61, 62]), patch("sushiwait.remote.time.sleep") as sleep:
            result = collect_remote(client, db, ["3014"], interval=30, samples=2)
        self.assertTrue(result["ok"])
        sleep.assert_called_once_with(30)

    def test_invalid_bounds_never_query(self):
        from unittest.mock import Mock
        for ids, interval, samples in (([],60,1),(["3014"]*2,60,1),(["1","2","3","4"],60,1),(["3014"],29,1),(["3014"],60,121)):
            client, db = Mock(), Mock()
            with self.assertRaises(ValueError): collect_remote(client, db, ids, interval=interval, samples=samples)
            self.assertFalse(client.snapshot.called)

    def test_cli_bad_bounds_network_and_credentials_zero(self):
        with patch("socket.socket", side_effect=AssertionError("network")) as net, patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("credentials")) as auth, patch("sushiwait.remote.RemoteStore", side_effect=AssertionError("storage")) as db, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["remote-collect","--db","unused","--store-id","3014","--interval","29"]), 2)
            self.assertEqual(main(["remote-report","--db","unused","--store-id","3014","--limit","0"]), 2)
        self.assertEqual((net.call_count, auth.call_count, db.call_count), (0,0,0))


if __name__ == "__main__":
    unittest.main()
