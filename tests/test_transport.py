"""HTTP hints remain typed, private-header-free and separate from source data."""

from email.message import Message
import io
import json
import tempfile
import unittest
from urllib.error import HTTPError

from sushiwait.client import SushiroClient
from sushiwait.observations import normalize_snapshot, compute_change
from sushiwait.storage import SnapshotStore
from sushiwait.transport import observe_headers, sanitize_transport


URL = "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=123"
TIME = "2026-10-04T09:15:22Z"
HINTS = {"Date": "Sun, 04 Oct 2026 09:15:22 GMT", "X-RateLimit-Limit": "60",
         "X-RateLimit-Remaining": "59", "X-Gateway-Cache": "MISS",
         "Cache-Control": "max-age=0, must-revalidate, no-cache, no-store, private"}


def snapshot(transport=None):
    return normalize_snapshot({"id": 123, "name": "合成店", "wait": 8}, "123",
        request_started_at=TIME, received_at=TIME, elapsed_ms=1,
        data_origin="synthetic", api_profile="miniapp_gateway", transport=transport)


class Response:
    def __init__(self, headers, url=URL):
        self.headers = headers
        self.url = url
        self.closed = False

    def getcode(self): return 200
    def geturl(self): return self.url
    def read(self, size): return b'{"id":123,"name":"synthetic"}'
    def close(self): self.closed = True


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def open(self, request, timeout):
        self.calls += 1
        if self.error: raise self.error
        return self.response


class TransportTests(unittest.TestCase):
    def test_allowlist_preserves_hints_without_source_or_quota_guarantees(self):
        result = observe_headers({**HINTS, "Set-Cookie": "PRIVATE_COOKIE",
                                  "X-Device-Risk": "PRIVATE_RISK", "Authorization": "PRIVATE_TOKEN"})
        self.assertEqual(result["http_date"], TIME)
        self.assertEqual((result["rate_limit"], result["rate_remaining"]), (60, 59))
        self.assertEqual(result["cache_max_age_seconds"], 0)
        self.assertEqual(result["gateway_cache"], "MISS")
        self.assertTrue(all(result["cache_flags"].values()))
        self.assertFalse(result["source_update_time_verified"])
        self.assertFalse(result["rate_window_verified"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_invalid_and_secret_values_are_unknown_without_echo(self):
        for value in (True, -1, "-1", "1.2", " 12", "１２", "9" * 512, "PRIVATE_VALUE"):
            with self.subTest(value=value):
                result = observe_headers({"X-RateLimit-Limit": value, "Cache-Control": "no-store"})
                self.assertIsNone(result["rate_limit"])
                self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertIsNone(observe_headers({"Date": "PRIVATE_DATE", "X-Gateway-Cache": "PRIVATE_HINT"}))
        self.assertIsNone(observe_headers({"Set-Cookie": "PRIVATE_COOKIE"}))

    def test_retry_after_only_numeric_and_ambiguous_max_age_unknown(self):
        result = observe_headers({"Retry-After": "15", "Age": "0", "Cache-Control": "max-age=0,max-age=60"})
        self.assertEqual(result["retry_after_seconds"], 15)
        self.assertEqual(result["age_seconds"], 0)
        self.assertIsNone(result["cache_max_age_seconds"])
        self.assertIsNone(observe_headers({"Retry-After": "Sun, 04 Oct 2026 10:00:00 GMT"}))

    def test_stored_metadata_revalidated_and_not_mutated(self):
        hints = observe_headers(HINTS)
        hints.update({"rate_remaining": True, "token": "PRIVATE", "source_update_time_verified": True})
        result = sanitize_transport(hints)
        self.assertIsNone(result["rate_remaining"])
        self.assertFalse(result["source_update_time_verified"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertTrue(hints["source_update_time_verified"])
        for value in (None, [], {"schema_version": True}, {"schema_version": 2}):
            self.assertIsNone(sanitize_transport(value))

    def test_cache_and_quota_changes_do_not_change_queue_content_hash(self):
        old = snapshot()
        new = snapshot(observe_headers(HINTS))
        self.assertEqual(old["content_hash"], new["content_hash"])
        self.assertEqual(compute_change(old, new)["content_status"], "unchanged")
        self.assertIsNone(new["source_updated_at"])
        self.assertEqual(new["upstream_freshness"], "unknown")

    def test_client_success_keeps_safe_metadata_and_closes_response(self):
        response = Response({**HINTS, "Set-Cookie": "PRIVATE"})
        opener = Opener(response)
        result = SushiroClient("synthetic", api_profile="miniapp_gateway", opener=opener).fetch_store("123")
        self.assertTrue(result.ok)
        self.assertEqual(result.transport["rate_remaining"], 59)
        self.assertTrue(response.closed)
        self.assertEqual(opener.calls, 1)
        self.assertNotIn("PRIVATE", repr(result))

    def test_429_records_retry_hint_without_reading_body_or_retrying(self):
        headers = Message()
        headers["Retry-After"] = "15"
        headers["Set-Cookie"] = "PRIVATE_COOKIE"
        body = io.BytesIO(b"PRIVATE_BODY")
        opener = Opener(error=HTTPError(URL, 429, "PRIVATE_REASON", headers, body))
        result = SushiroClient("synthetic", api_profile="miniapp_gateway", opener=opener).fetch_store("123")
        self.assertFalse(result.ok)
        self.assertEqual(result.http_status, 429)
        self.assertEqual(result.transport["retry_after_seconds"], 15)
        self.assertIsNone(result.payload)
        self.assertEqual(opener.calls, 1)
        self.assertTrue(body.closed)
        self.assertNotIn("PRIVATE", json.dumps(result.transport))

    def test_redirected_response_does_not_supply_transport_hints(self):
        opener = Opener(Response(HINTS, "https://other.invalid/PRIVATE"))
        result = SushiroClient("synthetic", api_profile="miniapp_gateway", opener=opener).fetch_store("123")
        self.assertEqual(result.error_code, "redirect_blocked")
        self.assertIsNone(result.transport)

    def test_quality_report_additive_metadata_supports_older_snapshots(self):
        with tempfile.TemporaryDirectory() as root, SnapshotStore(root + "/test.sqlite3") as db:
            db.save(snapshot())
            db.save(snapshot(observe_headers(HINTS)))
            report = db.report()
            self.assertEqual(report["database_schema_version"], 2)
            quality = report["groups"][0]["quality"]
            self.assertEqual(quality["transport"]["observed_records"], 1)
            self.assertEqual(quality["transport"]["gateway_cache_hints"], {"MISS": 1})
            self.assertEqual(quality["public_content"]["unchanged_content"], 1)
            self.assertFalse(quality["transport"]["rate_window_verified"])
