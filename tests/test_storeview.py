import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.client import QueryResult
from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore
from sushiwait.storeview import StoreViewError, store_view


BASE = datetime(2026, 10, 6, 3, tzinfo=timezone.utc)


def stamp(second):
    return (BASE + timedelta(seconds=second)).isoformat().replace("+00:00", "Z")


def snapshot(second, *, labels=None, origin="synthetic", profile="miniapp_gateway",
             store_id="900001", count=5, status="OPEN"):
    payload = {"id": int(store_id), "name": "synthetic store", "storeStatus": status,
               "groupQueuesCount": count, "groupQueues": {
                   "mixedQueue": labels if labels is not None else ["001", "002", "003"],
                   "reservationQueue": ["R001", "R002"]}}
    return normalize_snapshot(payload, store_id, request_started_at=stamp(second),
        received_at=stamp(second), elapsed_ms=0, api_profile=profile, data_origin=origin)


class StoreViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"

    def view(self, db, second=60, **kwargs):
        return store_view(db, "900001", data_origin="synthetic", api_profile="miniapp_gateway",
                          as_of=stamp(second), **kwargs)

    def raw(self, db, second, body, *, ok=1):
        db.db.execute("INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
            "VALUES(?,?,?,?,?,?,?)", (db.run_id, "900001", "synthetic", "miniapp_gateway", stamp(second), ok,
                                    json.dumps(body) if isinstance(body, dict) else body))
        db.db.commit()

    def test_latest_response_preserves_both_queues_order_duplicates_and_leading_zero(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            db.save(snapshot(30, labels=["A001", "001", "001"]))
            result = self.view(db)
        display = result["last_response"]["display"]
        self.assertEqual(display["groupQueues"]["groups"]["mixedQueue"]["value"], ["A001", "001", "001"])
        self.assertEqual(display["groupQueues"]["groups"]["reservationQueue"]["value"], ["R001", "R002"])
        self.assertEqual(result["last_response_age_seconds"], 30)
        self.assertEqual(result["availability"], "recent_response")
        self.assertFalse(result["display_is_last_known"])
        self.assertEqual(result["source_freshness"], "unknown")
        self.assertIsNone(result["source_updated_at"])
        self.assertFalse(result["eta_available"])
        self.assertFalse(result["personal_ticket_status_available"])
        self.assertFalse(result["complete_queue_available"])
        self.assertFalse(result["collector_state_available"])
        self.assertEqual(result["collector_liveness"], "unknown")

    def test_closed_status_with_nonempty_display_is_preserved(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(30, status="CLOSED", count=2))
            display = self.view(db)["last_response"]["display"]
        self.assertEqual(display["storeStatus"]["value"], "CLOSED")
        self.assertEqual(display["groupQueuesCount"], {"presence": "present", "value": 2, "unit": "unknown"})
        self.assertEqual(display["groupQueues"]["groups"]["mixedQueue"]["value"], ["001", "002", "003"])

    def test_missing_zero_empty_null_and_invalid_are_distinct(self):
        with SnapshotStore(self.path) as db:
            value = snapshot(30, labels=[], count=0)
            value["normalized"]["groupQueues"]["groups"]["reservationQueue"] = {"presence": "null", "value": None}
            value["normalized"]["groupQueues"]["groups"]["counterQueue"] = {"presence": "invalid", "value": None}
            db.save(value)
            display = self.view(db)["last_response"]["display"]
        groups = display["groupQueues"]["groups"]
        self.assertEqual(display["groupQueuesCount"]["value"], 0)
        self.assertEqual(groups["mixedQueue"], {"presence": "present", "value": []})
        self.assertEqual(groups["reservationQueue"]["presence"], "null")
        self.assertEqual(groups["counterQueue"]["presence"], "invalid")
        self.assertEqual(groups["boothQueue"]["presence"], "missing")

    def test_response_age_threshold_includes_boundary_and_marks_stale_after_it(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            self.assertEqual(self.view(db, 90)["availability"], "recent_response")
            result = self.view(db, 91)
        self.assertEqual(result["availability"], "stale")
        self.assertTrue(result["display_is_last_known"])
        self.assertEqual(result["last_response_age_seconds"], 91)

    def test_request_failure_preserves_last_known_without_presenting_it_as_latest(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            db.save_failure("900001", QueryResult(False, None, "http_error", 504,
                stamp(29), stamp(30), 1000), data_origin="synthetic", api_profile="miniapp_gateway")
            result = self.view(db)
        self.assertEqual(result["availability"], "last_known_only")
        self.assertEqual(result["refresh_state"], "query_failed")
        self.assertTrue(result["display_is_last_known"])
        self.assertEqual(result["latest_observation"]["http_status"], 504)
        self.assertEqual(result["last_response"]["received_at"], stamp(0))

    def test_preflight_stop_is_a_local_check_without_fake_http_response(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            db.save_preflight_stop("900001", "auth_expiring", checked_at=stamp(30),
                data_origin="synthetic", api_profile="miniapp_gateway")
            result = self.view(db)
        self.assertEqual(result["refresh_state"], "preflight_stopped")
        timing = result["latest_observation"]["timing"]
        self.assertEqual(timing["semantics"], "local_preflight_check")
        self.assertEqual(datetime.fromisoformat(timing["checked_at"].replace("Z", "+00:00")),
                         datetime.fromisoformat(stamp(30).replace("Z", "+00:00")))
        self.assertEqual(result["last_response_age_seconds"], 60)

    def test_transport_failure_without_http_response_does_not_create_response_timestamp(self):
        with SnapshotStore(self.path) as db:
            db.save_failure("900001", QueryResult(False, None, "network_error", None,
                stamp(29), stamp(30), 1000), data_origin="synthetic", api_profile="miniapp_gateway")
            result = self.view(db)
        self.assertEqual(result["availability"], "unavailable")
        self.assertIsNone(result["last_response"])
        self.assertIsNone(result["last_response_age_seconds"])
        self.assertIsNone(result["latest_observation"]["http_status"])
        self.assertEqual(result["latest_observation"]["timing"]["semantics"], "query_attempt_finished")

    def test_unknown_record_time_blocks_recency_without_echoing_raw_timestamp(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            self.raw(db, 30, snapshot(30))
            db.db.execute("UPDATE samples SET received_at=? WHERE id=2", ("PRIVATE_INVALID_TIME",))
            db.db.commit()
            result = self.view(db)
        self.assertEqual(result["availability"], "invalid_latest_observation")
        self.assertNotIn("PRIVATE_INVALID_TIME", json.dumps(result))

    def test_legacy_schema_is_explicitly_rejected_without_migration(self):
        with SnapshotStore(self.path) as db:
            db._schema_version = 1
            with self.assertRaises(StoreViewError) as error: self.view(db)
            self.assertEqual(error.exception.error_code, "store_view_requires_database_schema_2")
            self.assertEqual(db.db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_new_valid_response_recovers_display_after_failure(self):
        with SnapshotStore(self.path) as db:
            db.save_preflight_stop("900001", "auth_expiring", checked_at=stamp(0),
                data_origin="synthetic", api_profile="miniapp_gateway")
            db.save(snapshot(30, labels=["004"]))
            result = self.view(db)
        self.assertEqual(result["availability"], "recent_response")
        self.assertEqual(result["refresh_state"], "response_received")
        self.assertEqual(result["scan"]["valid_failures"], 1)

    def test_foreign_store_profile_and_origin_do_not_replace_display(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            db.save(snapshot(30, store_id="900002", labels=["999"]))
            db.save(snapshot(30, profile="legacy", labels=["998"]))
            db.save(snapshot(30, origin="fixture", labels=["997"]))
            result = self.view(db)
        self.assertEqual(result["scan"]["scanned_rows"], 1)
        self.assertEqual(result["last_response"]["received_at"], stamp(0))
        self.assertNotIn('"999"', json.dumps(result))

    def test_future_display_and_failure_are_excluded_before_interpretation(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(0))
            self.raw(db, 120, {"secret": "FUTURE_MUST_NOT_ESCAPE"})
            db.save_preflight_stop("900001", "auth_expiring", checked_at=stamp(121),
                data_origin="synthetic", api_profile="miniapp_gateway")
            result = self.view(db)
        self.assertEqual(result["availability"], "recent_response")
        self.assertEqual(result["scan"]["future_rows_excluded"], 2)
        self.assertNotIn("FUTURE_MUST_NOT_ESCAPE", json.dumps(result))

    def test_invalid_record_identity_timing_duplicate_json_and_oversize_block_latest_claim(self):
        for kind in ("identity", "timing", "duplicate", "large", "label"):
            with self.subTest(kind=kind), SnapshotStore(Path(self.tmp.name) / (kind + ".sqlite3")) as db:
                db.save(snapshot(0))
                bad = snapshot(30)
                if kind == "identity": bad["normalized"]["id"]["value"] = 900002
                if kind == "timing": bad["timing"]["received_at"] = stamp(29)
                if kind == "label": bad["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"] = ["SECRET_NOT_A_NUMBER"]
                if kind == "duplicate": bad = '{"secret":"MUST_NOT_ESCAPE","secret":"again"}'
                if kind == "large": bad = "MUST_NOT_ESCAPE" * 6000
                self.raw(db, 30, bad)
                result = self.view(db)
                self.assertEqual(result["availability"], "invalid_latest_observation")
                self.assertTrue(result["display_is_last_known"])
                self.assertIsNone(result["latest_observation"])
                self.assertNotIn("MUST_NOT_ESCAPE", json.dumps(result))
                self.assertNotIn("SECRET_NOT_A_NUMBER", json.dumps(result))

    def test_out_of_order_tail_cannot_make_stale_display_appear_recent(self):
        with SnapshotStore(self.path) as db:
            db.save(snapshot(30))
            db.save(snapshot(0, labels=["999"]))
            result = self.view(db)
        self.assertEqual(result["availability"], "invalid_latest_observation")
        self.assertEqual(result["scan"]["out_of_order_rows"], 1)
        self.assertEqual(result["last_response"]["received_at"], stamp(30))
        self.assertNotIn('"999"', json.dumps(result))

    def test_no_success_is_unavailable_and_bounded_tail_does_not_invent_history(self):
        with SnapshotStore(self.path) as db:
            self.assertEqual(self.view(db)["availability"], "unavailable")
            db.save(snapshot(0))
            db.save_preflight_stop("900001", "auth_expiring", checked_at=stamp(30),
                data_origin="synthetic", api_profile="miniapp_gateway")
            result = self.view(db, sample_limit=1)
        self.assertEqual(result["availability"], "unavailable")
        self.assertIsNone(result["last_response"])
        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual(result["scan"]["scanned_rows"], 1)

    def test_private_metadata_does_not_survive_projection(self):
        with SnapshotStore(self.path) as db:
            value = snapshot(30)
            value["headers"] = {"Authorization": "MUST_NOT_ESCAPE"}
            value["personal_ticket"] = "MUST_NOT_ESCAPE"
            value["normalized"]["address"] = {"presence": "present", "value": "MUST_NOT_ESCAPE"}
            value["timing"]["private"] = "MUST_NOT_ESCAPE"
            db.save(value)
            result = self.view(db)
        self.assertNotIn("MUST_NOT_ESCAPE", json.dumps(result))
        self.assertNotIn("run_id", json.dumps(result))
        self.assertTrue(result["output_requires_private_handling"])

    def test_readonly_view_does_not_change_database_or_open_network_or_read_auth(self):
        with SnapshotStore(self.path) as db: db.save(snapshot(30))
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target in ("socket.socket", "socket.create_connection", "sushiwait.cli.client_for",
                           "sushiwait.cli.read_credentials_file", "sushiwait.surgeguard._command",
                           "subprocess.Popen"):
                stack.enter_context(patch(target, side_effect=AssertionError("unexpected side effect")))
            stack.enter_context(contextlib.redirect_stdout(output))
            code = main(["store-view", "--db", str(self.path), "--store-id", "0900001",
                "--api-profile", "miniapp_gateway", "--data-origin", "synthetic", "--as-of", stamp(60)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["store_id"], "900001")
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_bad_bounds_and_time_are_rejected_before_database_access(self):
        common = ["store-view", "--db", str(self.path), "--store-id", "900001",
                  "--api-profile", "miniapp_gateway", "--data-origin", "synthetic", "--as-of", stamp(60)]
        for extra in (["--max-age-seconds", "0"], ["--sample-limit", "10001"], ["--as-of", "2026-10-06"]):
            with self.subTest(extra=extra), patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("database opened")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(common + extra), 1)
        self.assertFalse(self.path.exists())

    def test_busy_transaction_is_not_rolled_back_by_view(self):
        with SnapshotStore(self.path) as db:
            db.db.execute("BEGIN")
            with self.assertRaises(StoreViewError): self.view(db)
            self.assertTrue(db.db.in_transaction)
            db.db.rollback()
