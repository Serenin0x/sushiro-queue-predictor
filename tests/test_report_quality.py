"""Offline collection-quality reports over synthetic bounded histories."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sushiwait.client import QueryResult
from sushiwait.observations import QUEUE_NAMES, normalize_snapshot
from sushiwait.storage import SnapshotStore


def snapshot(second=0, *, fields=None, profile="legacy", origin="synthetic", store_id="12", elapsed=100):
    time = f"2026-10-03T10:00:{second:02d}.000Z"
    return normalize_snapshot(
        {"id": int(store_id), "name": "合成门店", **(fields or {})}, store_id,
        request_started_at=time, received_at=time, elapsed_ms=elapsed,
        data_origin=origin, api_profile=profile,
    )


def failure(second=0, *, status=401, error="http_error", elapsed=100):
    time = f"2026-10-03T10:00:{second:02d}.000Z"
    return QueryResult(False, None, error, status, time, time, elapsed)


class ReportQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quality.sqlite3"

    def raw(self, db, payload, *, ok=0, second=0, run=None, profile="legacy"):
        encoded = json.dumps(payload) if isinstance(payload, dict) else payload
        db.db.execute(
            "INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (run or db.run_id, "12", "synthetic", profile,
             f"2026-10-03T10:00:{second:02d}.000Z", ok, encoded),
        )
        db.db.commit()

    def quality(self, db, **kwargs):
        return db.report(**kwargs)["groups"][0]["quality"]

    def test_preflight_is_local_event_without_fabricated_http_or_request_time(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot())
            db.save_failure("12", failure(10), data_origin="synthetic")
            db.save_failure("12", failure(20, status=200, error="normalization_failed"),
                            data_origin="synthetic", failure_phase="normalization")
            db.save_preflight_stop("12", "auth_declared_expired",
                                   checked_at="2026-10-03T18:00:30+08:00", data_origin="synthetic")
            report = db.report()
            group, quality = report["groups"][0], self.quality(db)
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["database_schema_version"], 2)
            self.assertEqual(group["samples"], 4)
            self.assertEqual(group["failed_samples"], 3)
            self.assertEqual(group["record_time_semantics"], "response_received_or_preflight_checked")
            self.assertEqual(group["last_recorded_at"], "2026-10-03T10:00:30.000Z")
            self.assertEqual(group["last_received_at"], group["last_recorded_at"])
            self.assertEqual(quality["failures"]["by_phase"],
                             {"request": 1, "normalization": 1, "preflight": 1, "unknown": 0})
            self.assertEqual(quality["failures"]["actual_request_failures"], 2)
            self.assertEqual(quality["failures"]["local_stops"], 1)
            self.assertEqual(quality["failures"]["http_status_by_phase"],
                             {"request": {"401": 1}, "normalization": {"200": 1}})
            self.assertEqual(quality["requests"]["records"], 3)
            self.assertEqual(quality["requests"]["elapsed_ms"]["count"], 3)
            self.assertEqual(quality["requests"]["window_last_request_started_at"],
                             "2026-10-03T10:00:20.000Z")
            stop = json.loads(db.db.execute("SELECT payload_json FROM samples ORDER BY id DESC LIMIT 1").fetchone()[0])
            self.assertIsNone(stop["http_status"])
            self.assertEqual(stop["timing"], {"checked_at": "2026-10-03T10:00:30.000Z"})

    def test_auth_metadata_whitelist_and_invalid_stop_inputs_never_insert(self):
        secret = "SYNTHETIC_PRIVATE_VALUE"
        with SnapshotStore(self.path) as db:
            db.save_preflight_stop("12", "auth_expiring", checked_at="2026-10-03T10:00:00Z",
                                  data_origin="synthetic", auth_status={
                                      "configured": True, "token_kind": "jwt_like", "expired": False,
                                      "declared_expires_at": "2026-10-03T18:00:10+08:00",
                                      "declared_issued_at": None, "declared_lifetime_seconds": 100,
                                      "remaining_seconds": 10, "expiry_source": "unverified_claim",
                                      "signature_verified": False, "sub": secret, "authorization": secret,
                                      "revision": secret, "claims": {"private": secret},
                                  })
            payload = json.loads(db.db.execute("SELECT payload_json FROM samples").fetchone()[0])
            self.assertEqual(len(payload["auth_status"]), 9)
            self.assertEqual(payload["auth_status"]["declared_expires_at"], "2026-10-03T10:00:10.000Z")
            self.assertNotIn(secret, json.dumps(payload))
            self.assertNotIn("auth_status", json.dumps(db.report()))
            for kwargs in ({"error_code": secret, "checked_at": "2026-10-03T10:00:00Z"},
                           {"error_code": "auth_expiring", "checked_at": secret}):
                with self.assertRaises(ValueError) as raised:
                    db.save_preflight_stop("12", data_origin="synthetic", **kwargs)
                self.assertNotIn(secret, str(raised.exception))
            with self.assertRaisesRegex(ValueError, "invalid_failure_phase"):
                db.save_failure("12", failure(), failure_phase="preflight")
            self.assertEqual(db.report()["groups"][0]["samples"], 1)

    def test_bad_auth_metadata_does_not_preserve_values_or_claim_verification(self):
        with SnapshotStore(self.path) as db:
            db.save_preflight_stop("12", "auth_claims_invalid", checked_at="2026-10-03T10:00:00Z",
                                  data_origin="synthetic", auth_status={
                                      "configured": "private", "token_kind": "private",
                                      "declared_issued_at": "private", "declared_expires_at": "private",
                                      "declared_lifetime_seconds": float("inf"), "remaining_seconds": True,
                                      "expired": "private", "expiry_source": "private", "signature_verified": True,
                                  })
            payload = json.loads(db.db.execute("SELECT payload_json FROM samples").fetchone()[0])
            self.assertNotIn("auth_status", payload)

    def test_intervals_use_real_request_starts_same_run_and_include_request_failure(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0, elapsed=100))
            second = snapshot(10, elapsed=200)
            second["timing"]["request_started_at"] = "2026-10-03T18:00:10+08:00"
            second["timing"]["received_at"] = "2026-10-03T18:00:10+08:00"
            db.save(second)
            db.save_failure("12", failure(20, elapsed=300), data_origin="synthetic")
            db.run_id = "different-synthetic-run"
            db.save(snapshot(30, elapsed=400))
            db.save(snapshot(40, elapsed=500))
            quality = self.quality(db)
            self.assertEqual(quality["requests"]["start_interval_ms"],
                             {"count": 3, "min": 10000, "max": 10000, "mean": 10000.0})
            self.assertEqual(quality["requests"]["elapsed_ms"],
                             {"count": 5, "min": 100, "max": 500, "mean": 300.0})
            self.assertEqual(quality["public_content"]["comparable_pairs"], 2)

    def test_preflight_invalid_timing_and_backward_starts_do_not_invent_intervals(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(10))
            db.save(snapshot(0))
            bad = snapshot(20)
            bad["timing"]["elapsed_ms"] = True
            db.save(bad)
            db.save(snapshot(30))
            db.save_preflight_stop("12", "auth_expiring", checked_at="2026-10-03T10:00:35Z",
                                   data_origin="synthetic")
            db.save(snapshot(40))
            quality = self.quality(db)
            self.assertEqual(quality["requests"]["reversed_start_pairs"], 1)
            self.assertEqual(quality["requests"]["invalid_timing_records"], 1)
            self.assertEqual(quality["requests"]["start_interval_ms"]["count"], 0)
            self.assertEqual(quality["requests"]["elapsed_ms"]["count"], 4)

    def test_single_sample_and_empty_database_have_no_invented_rate_or_time(self):
        with SnapshotStore(self.path) as db:
            self.assertEqual(db.report()["groups"], [])
            db.save(snapshot())
            quality = self.quality(db)
            self.assertEqual(quality["requests"]["start_interval_ms"],
                             {"count": 0, "min": None, "max": None, "mean": None})
            self.assertEqual(quality["public_content"]["comparable_pairs"], 0)
            self.assertEqual(quality["upstream_freshness"], "unknown")
            self.assertNotIn("eta", quality)
            self.assertNotIn("no_show_rate", quality)

    def test_profile_origin_and_store_quality_are_isolated(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            db.save(snapshot(10))
            db.save(snapshot(20, profile="miniapp_gateway"))
            db.save(snapshot(30, origin="fixture"))
            db.save(snapshot(40, store_id="13"))
            groups = db.report()["groups"]
            self.assertEqual(len(groups), 4)
            for group in groups:
                pairs = group["quality"]["public_content"]["comparable_pairs"]
                expected = int(group["store_id"] == "12" and group["data_origin"] == "synthetic"
                               and group["api_profile"] == "legacy")
                self.assertEqual(pairs, expected)

    def test_presence_retains_missing_null_invalid_and_zero_without_guessing_unit(self):
        fields = ({}, {"wait": None, "groupQueues": None},
                  {"wait": True, "groupQueues": False},
                  {"wait": 0, "groupQueues": {key: [] for key in QUEUE_NAMES}})
        with SnapshotStore(self.path) as db:
            for index, value in enumerate(fields):
                db.save(snapshot(index, fields=value))
            quality = self.quality(db)
            all_states = {"missing": 1, "null": 1, "invalid": 1, "present": 1, "unknown": 0}
            self.assertEqual(quality["field_presence"]["raw_wait"], all_states)
            self.assertEqual(quality["field_presence"]["groupQueues"], all_states)
            for queue in quality["queues"].values():
                self.assertEqual(queue["presence"], all_states)
                self.assertEqual(queue["comparable_pairs"], 0)
            self.assertEqual(quality["public_content"]["field_changes"]["raw_wait"], 3)

    def test_corrupt_presence_metadata_is_unknown_without_echoing_private_state(self):
        private = "SYNTHETIC_PRIVATE_VALUE"
        with SnapshotStore(self.path) as db:
            observed = snapshot()
            observed["normalized"]["raw_wait"] = {"presence": private, "value": private}
            observed["normalized"]["groupQueues"] = {"presence": private, "groups": private}
            self.raw(db, observed, ok=1)
            quality = self.quality(db)
            self.assertEqual(quality["field_presence"]["raw_wait"]["unknown"], 1)
            self.assertEqual(quality["field_presence"]["groupQueues"]["unknown"], 1)
            for queue in quality["queues"].values():
                self.assertEqual(queue["presence"]["unknown"], 1)
            self.assertNotIn(private, json.dumps(quality))

    def test_public_array_changes_include_order_duplicates_prefixes_and_reset_labels(self):
        arrays = (["A001", "A002"], ["A002", "A001", "A001"], ["B001"], ["B001"])
        with SnapshotStore(self.path) as db:
            for index, labels in enumerate(arrays):
                db.save(snapshot(index, fields={"waitTimeCounter": -1,
                                                "groupQueues": {"mixedQueue": labels}}))
            quality = self.quality(db)
            mixed = quality["queues"]["mixedQueue"]
            self.assertEqual(mixed["comparable_pairs"], 3)
            self.assertEqual(mixed["display_values_changes"], 2)
            self.assertEqual(mixed["display_set_changes"], 1)
            self.assertEqual(quality["public_content"]["content_changes"], 2)
            self.assertEqual(quality["public_content"]["unchanged_content"], 1)
            encoded = json.dumps(quality)
            self.assertNotIn("A001", encoded)
            self.assertNotIn("B001", encoded)

    def test_failure_bad_json_and_invalid_success_break_public_comparison_chain(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0, fields={"wait": 1}))
            db.save_failure("12", failure(1), data_origin="synthetic")
            db.save(snapshot(2, fields={"wait": 2}))
            self.raw(db, "{bad-json", second=3)
            db.save(snapshot(4, fields={"wait": 3}))
            wrong = snapshot(5)
            wrong["api_profile"] = "miniapp_gateway"
            self.raw(db, wrong, ok=1, second=5)
            db.save(snapshot(6, fields={"wait": 4}))
            quality = self.quality(db)
            self.assertEqual(quality["public_content"]["comparable_pairs"], 0)
            self.assertEqual(quality["public_content"]["invalid_successful_payloads"], 1)
            self.assertEqual(quality["invalid_json_or_oversized_payloads"], 1)
            self.assertEqual(quality["failures"]["by_phase"]["unknown"], 1)

    def test_unknown_error_phase_and_http_values_are_not_echoed_and_old_phase_is_request(self):
        secret = "SYNTHETIC_PRIVATE_VALUE"
        with SnapshotStore(self.path) as db:
            self.raw(db, {"error_code": secret, "http_status": secret, "failure_phase": secret,
                          "timing": {"received_at": secret}, secret: secret})
            self.raw(db, {"error_code": "http_error", "http_status": 401,
                          "timing": {"request_started_at": "2026-10-03T10:00:10Z",
                                     "received_at": "2026-10-03T10:00:10Z", "elapsed_ms": 0}}, second=10)
            self.raw(db, {"error_code": secret, "http_status": True, "failure_phase": "request"}, second=20)
            quality = self.quality(db)
            self.assertEqual(quality["failures"]["by_phase"]["unknown"], 1)
            self.assertEqual(quality["failures"]["by_phase"]["request"], 2)
            self.assertEqual(quality["failures"]["http_status_by_phase"]["request"], {"401": 1, "unknown": 1})
            self.assertEqual(quality["failures"]["error_codes_by_phase"]["request"], {"http_error": 1, "unknown": 1})
            self.assertNotIn(secret, json.dumps(db.report()))

    def test_latest_window_limit_is_explicit_and_excludes_older_stats(self):
        with SnapshotStore(self.path) as db:
            db.save_failure("12", failure(0), data_origin="synthetic")
            for index in range(1, 5):
                db.save(snapshot(index, fields={"wait": index}, elapsed=index*100))
            group = db.report(max_quality_samples=2)["groups"][0]
            quality = group["quality"]
            self.assertEqual(group["samples"], 5)
            self.assertEqual(group["failed_samples"], 1)
            self.assertEqual(quality["window"]["included_samples"], 2)
            self.assertEqual(quality["window"]["first_sample_id"], 4)
            self.assertEqual(quality["window"]["last_sample_id"], 5)
            self.assertTrue(quality["window"]["truncated"])
            self.assertEqual(quality["failures"]["actual_request_failures"], 0)
            self.assertEqual(quality["requests"]["elapsed_ms"]["mean"], 350.0)
            self.assertEqual(quality["public_content"]["comparable_pairs"], 1)
            for limit in (0, -1, True, "10", 10001):
                with self.assertRaisesRegex(ValueError, "invalid_report_sample_limit"):
                    db.report(max_quality_samples=limit)

    def test_malformed_duplicate_nonfinite_oversized_and_binary_json_are_bounded(self):
        with SnapshotStore(self.path) as db:
            for value in ("[]", '{"error_code":"http_error","error_code":"timeout"}',
                          '{"number":NaN}', '{"private":"' + "x"*(2*1024*1024) + '"}', b"\xff"):
                self.raw(db, value)
            quality = self.quality(db)
            self.assertEqual(quality["invalid_json_or_oversized_payloads"], 5)
            self.assertEqual(quality["failures"]["by_phase"]["unknown"], 5)
            self.assertEqual(quality["requests"]["records"], 0)
            self.assertLess(len(json.dumps(quality)), 10000)

    def test_report_streams_cursors_without_fetchall_payload_materialization(self):
        class StreamingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __iter__(self):
                return iter(self.cursor)

            def fetchall(self):
                raise AssertionError("report must stream")

        class StreamingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, *args):
                return StreamingCursor(self.connection.execute(*args))

            def close(self):
                self.connection.close()

        with SnapshotStore(self.path) as db:
            for index in range(10):
                db.save(snapshot(index))
            db.db = StreamingConnection(db.db)
            quality = self.quality(db, max_quality_samples=3)
            self.assertEqual(quality["window"]["included_samples"], 3)

    def test_read_only_v1_quality_preserves_file_and_virtual_legacy_profile(self):
        old = snapshot()
        del old["api_profile"]
        with sqlite3.connect(self.path) as connection:
            connection.execute("""CREATE TABLE samples (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, store_id TEXT NOT NULL,
                data_origin TEXT NOT NULL, received_at TEXT NOT NULL,
                ok INTEGER NOT NULL, payload_json TEXT NOT NULL
            )""")
            connection.execute("INSERT INTO samples VALUES(1,'synthetic-run','12','synthetic',?,1,?)",
                               (old["timing"]["received_at"], json.dumps(old)))
            connection.execute("PRAGMA user_version=1")
        before = self.path.read_bytes()
        with SnapshotStore(self.path, read_only=True) as db:
            report = db.report()
            self.assertEqual(report["database_schema_version"], 1)
            self.assertEqual(report["groups"][0]["api_profile"], "legacy")
            self.assertEqual(report["groups"][0]["quality"]["public_content"]["valid_successful_payloads"], 1)
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
