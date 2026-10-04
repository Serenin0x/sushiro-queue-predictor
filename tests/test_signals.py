import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.observations import normalize_snapshot
from sushiwait.signals import SignalError, signal_report
from sushiwait.storage import SnapshotStore


BASE = datetime(2026, 10, 4, 10, tzinfo=timezone.utc)


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def sample(second, labels=None, *, count=10, profile="legacy", origin="synthetic", store_id="900001"):
    payload = {"id": int(store_id), "groupQueuesCount": count,
               "groupQueues": {"mixedQueue": labels if labels is not None else ["A001", "A002"]}}
    return normalize_snapshot(payload, store_id, request_started_at=stamp(second),
                              received_at=stamp(second), elapsed_ms=0,
                              api_profile=profile, data_origin=origin)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite3"

    def report(self, db, second=120, **kwargs):
        return signal_report(db, "900001", data_origin="synthetic", api_profile="legacy",
                             as_of=stamp(second), **kwargs)

    def raw(self, db, second, encoded, *, ok=1, run=None):
        db.db.execute("INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
                      "VALUES(?,?,?,?,?,?,?)", (run or db.run_id, "900001", "synthetic", "legacy",
                      stamp(second), ok, json.dumps(encoded) if isinstance(encoded, dict) else encoded))
        db.db.commit()

    def test_two_halves_measure_observed_turnover_without_queue_event_attribution(self):
        labels = [["A001", "A002"], ["A001", "A002"], ["A002", "A003"],
                  ["A002", "A003"], ["A002", "A003"], ["A003", "A004"],
                  ["A004", "A005"], ["A005", "A006"], ["A006", "A007"]]
        with SnapshotStore(self.path) as db:
            for index, values in enumerate(labels):
                db.save(sample(index * 15, values, count=10 - index))
            result = self.report(db)
        mixed = result["queues"]["mixedQueue"]
        self.assertEqual(mixed["whole"]["comparable_pairs"], 8)
        self.assertEqual(mixed["whole"]["observed_seconds"], 120)
        self.assertEqual(mixed["previous_half"]["removed_labels_per_observed_minute"], 1)
        self.assertEqual(mixed["recent_half"]["removed_labels_per_observed_minute"], 4)
        self.assertEqual(mixed["rate_comparison"]["removed_label_rate_ratio"], 4)
        self.assertEqual(mixed["rate_comparison"]["direction"], "higher")
        self.assertEqual(result["reported_count"]["sum_of_pair_deltas"], -8)
        self.assertEqual(result["reported_count"]["unit"], "unknown")
        self.assertEqual(result["calendar_at_as_of"]["date_type"], "holiday")
        self.assertIsNone(result["true_no_show_rate"])
        self.assertFalse(result["eta_available"])
        self.assertNotIn("A001", json.dumps(result))

    def test_opaque_prefix_reset_duplicate_and_order_changes_do_not_become_number_speed(self):
        with SnapshotStore(self.path) as db:
            for second, labels in ((0, ["A001", "A002"]), (30, ["A002", "A001", "A001"]),
                                   (60, ["B001"])):
                db.save(sample(second, labels))
            mixed = self.report(db, 60)["queues"]["mixedQueue"]["whole"]
        self.assertEqual(mixed["display_values_changed_pairs"], 2)
        self.assertEqual(mixed["display_set_changed_pairs"], 1)
        self.assertEqual((mixed["added_labels"], mixed["removed_labels"]), (1, 2))
        self.assertNotIn("cursor", json.dumps(mixed))

    def test_missing_arrays_are_unknown_but_observed_empty_arrays_have_zero_turnover(self):
        with SnapshotStore(self.path) as db:
            for second in (0, 30):
                db.save(sample(second, []))
            result = self.report(db, 30)
        self.assertEqual(result["queues"]["mixedQueue"]["whole"]["removed_labels_per_observed_minute"], 0)
        self.assertIsNone(result["queues"]["boothQueue"]["whole"]["removed_labels_per_observed_minute"])
        self.assertEqual(result["queues"]["boothQueue"]["presence"], {"missing": 2})

    def test_failure_and_missing_field_break_only_comparable_chains(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            self.raw(db, 30, {"private": "DO_NOT_ECHO"}, ok=0)
            db.save(sample(60))
            missing = sample(75)
            del missing["normalized"]["groupQueues"]["groups"]["mixedQueue"]
            db.save(missing)
            db.save(sample(90))
            db.save(sample(120))
            result = self.report(db)
        self.assertEqual(result["queues"]["mixedQueue"]["whole"]["comparable_pairs"], 1)
        self.assertEqual(result["chain_breaks"]["failed_record"], 1)
        self.assertNotIn("DO_NOT_ECHO", json.dumps(result))

    def test_gaps_runs_and_non_increasing_times_are_not_bridged(self):
        with SnapshotStore(self.path) as db:
            for second in (0, 30, 150, 180):
                db.save(sample(second))
            result = self.report(db, 180, window_seconds=180)
            self.assertEqual(result["queues"]["mixedQueue"]["whole"]["comparable_pairs"], 2)
            self.assertEqual(result["chain_breaks"], {"sampling_gap": 1})
            db.run_id = "another-synthetic-run"
            db.save(sample(195))
            result = self.report(db, 195, window_seconds=300)
            self.assertEqual(result["chain_breaks"]["run_changed"], 1)
        with SnapshotStore(Path(self.tmp.name) / "reversed.sqlite3") as db:
            for second in (30, 0, 60, 90):
                db.save(sample(second))
            result = self.report(db, 90)
        self.assertEqual(result["queues"]["mixedQueue"]["whole"]["comparable_pairs"], 1)
        self.assertEqual(result["chain_breaks"]["non_increasing_time"], 1)

    def test_future_observations_do_not_change_as_of_features(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            db.save(sample(30))
            before = self.report(db, 30)
            db.save(sample(60, ["FUTURE_SECRET_LABEL"]))
            after = self.report(db, 30)
        self.assertEqual(before["queues"], after["queues"])
        self.assertEqual(before["reported_count"], after["reported_count"])
        self.assertEqual(after["scan"]["future_rows_excluded"], 1)
        self.assertNotIn("FUTURE_SECRET_LABEL", json.dumps(after))

    def test_reversed_segments_cannot_double_count_overlapping_elapsed_time(self):
        with SnapshotStore(self.path) as db:
            for second in (0, 60, 30, 40, 90, 120):
                db.save(sample(second))
            result = self.report(db)
        whole = result["queues"]["mixedQueue"]["whole"]
        self.assertEqual(whole["observed_seconds"], 90)
        self.assertEqual(whole["observed_fraction_of_window"], 0.75)
        self.assertEqual(result["scan"]["out_of_order_successful_rows"], 2)
        self.assertEqual(result["chain_breaks"]["non_increasing_time"], 2)

    def test_time_window_and_midpoint_boundary_intervals_are_explicit(self):
        with SnapshotStore(self.path) as db:
            for second in (-1, 0, 50, 70, 120):
                db.save(sample(second))
            mixed = self.report(db)["queues"]["mixedQueue"]
        self.assertEqual(mixed["cross_midpoint_pairs"], 1)
        self.assertEqual(mixed["whole"]["observed_seconds"], 120)
        self.assertEqual(mixed["previous_half"]["observed_seconds"], 50)
        self.assertEqual(mixed["recent_half"]["observed_seconds"], 50)
        self.assertEqual(mixed["rate_comparison"]["status"], "insufficient_pairs")

    def test_stale_or_interrupted_tail_is_not_presented_as_current_acceleration(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            db.save(sample(30))
            stale = self.report(db, 121, window_seconds=300)
            self.assertEqual(stale["availability"], "stale")
            self.assertEqual(stale["last_success_age_seconds"], 91)
            self.assertEqual(stale["queues"]["mixedQueue"]["rate_comparison"]["status"], "window_not_current")
            self.raw(db, 40, {}, ok=0)
            self.assertEqual(self.report(db, 40)["availability"], "interrupted")

    def test_zero_baseline_keeps_null_ratio_and_missing_history_keeps_null_rates(self):
        with SnapshotStore(self.path) as db:
            empty = self.report(db)
            self.assertEqual(empty["availability"], "no_observations")
            self.assertIsNone(empty["reported_count"]["sum_of_pair_deltas"])
            for second in range(0, 121, 15):
                db.save(sample(second, ["A"] if second <= 60 else [str(second)]))
            result = self.report(db)["queues"]["mixedQueue"]["rate_comparison"]
        self.assertEqual(result["status"], "previous_zero")
        self.assertEqual(result["direction"], "higher")
        self.assertIsNone(result["removed_label_rate_ratio"])

    def test_group_isolation_and_bounded_scan_do_not_merge_other_stores_or_origins(self):
        with SnapshotStore(self.path) as db:
            for second in range(0, 121, 30):
                db.save(sample(second))
                db.save(sample(second, origin="fixture"))
                db.save(sample(second, profile="miniapp_gateway"))
                db.save(sample(second, store_id="900002"))
            result = self.report(db, sample_limit=2)
        self.assertEqual(result["scan"]["total_group_rows"], 5)
        self.assertEqual(result["scan"]["scanned_rows"], 2)
        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual(result["queues"]["mixedQueue"]["whole"]["comparable_pairs"], 1)

    def test_corrupt_json_identity_timing_and_unknown_record_times_are_not_echoed(self):
        secret = "SYNTHETIC_SECRET_DO_NOT_ECHO"
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            for index, encoded in enumerate(("{bad-json", '{"a":1,"a":2}', '{"a":NaN}', b"\xff",
                '{"secret":"' + "x" * (2 * 1024 * 1024) + '"}')):
                self.raw(db, 10 + index, encoded)
            wrong = sample(30)
            wrong["normalized"]["id"]["value"] = "900002"
            self.raw(db, 30, wrong)
            wrong = sample(40)
            wrong["timing"]["received_at"] = stamp(41)
            self.raw(db, 40, wrong)
            wrong = sample(50)
            wrong["timing"]["elapsed_ms"] = True
            self.raw(db, 50, wrong)
            self.raw(db, 60, {secret: secret})
            db.db.execute("UPDATE samples SET received_at=? WHERE id=(SELECT MAX(id) FROM samples)", (secret,))
            db.db.commit()
            result = self.report(db, 60)
        self.assertEqual(result["chain_breaks"]["invalid_record"], 8)
        self.assertEqual(result["scan"]["unknown_record_time_rows"], 1)
        self.assertEqual(result["availability"], "unknown")
        self.assertEqual(result["queues"]["mixedQueue"]["whole"]["comparable_pairs"], 0)
        self.assertNotIn(secret, json.dumps(result))

    def test_oversized_reported_counts_are_not_converted_to_unbounded_float_rates(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0, count=2**100))
            db.save(sample(30, count=0))
            result = self.report(db, 30)
        self.assertEqual(result["reported_count"]["comparable_pairs"], 0)
        self.assertIsNone(result["reported_count"]["sum_of_pair_deltas"])

    def test_invalid_settings_return_fixed_errors_and_leave_existing_transaction_alone(self):
        with SnapshotStore(self.path) as db:
            defaults = dict(data_origin="synthetic", api_profile="legacy", as_of=stamp(0))
            for key, value in (("as_of", "2026-10-04"), ("window_seconds", True),
                    ("window_seconds", 29), ("max_gap_seconds", 0), ("sample_limit", 10001),
                    ("data_origin", "DO_NOT_ECHO"), ("api_profile", "DO_NOT_ECHO")):
                with self.assertRaises(SignalError) as error:
                    signal_report(db, "900001", **{**defaults, key: value})
                self.assertNotIn("DO_NOT_ECHO", str(error.exception))
            db.db.execute("BEGIN")
            with self.assertRaisesRegex(SignalError, "signal_requires_idle_connection"):
                self.report(db)
            self.assertTrue(db.db.in_transaction)
            db.db.rollback()

    def test_read_transaction_ends_even_when_query_fails(self):
        with SnapshotStore(self.path) as db:
            db.db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                                  else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):
                self.report(db)
            self.assertFalse(db.db.in_transaction)

    def test_counts_and_streamed_rows_share_one_snapshot_during_concurrent_append(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            db.save(sample(30))
            db.db.execute("PRAGMA journal_mode=WAL")
            actual = db.db
            path = self.path

            class Cursor:
                def __init__(self, cursor, inject=False):
                    self.cursor, self.inject = cursor, inject

                def __iter__(self):
                    return iter(self.cursor)

                def fetchall(self):
                    raise AssertionError("must stream")

                def fetchone(self):
                    value = self.cursor.fetchone()
                    if self.inject:
                        with sqlite3.connect(path) as writer:
                            writer.execute("INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
                                "VALUES('synthetic-concurrent-run','900001','synthetic','legacy',?,1,?)",
                                (stamp(60), json.dumps(sample(60))))
                    return value

            class Connection:
                @property
                def in_transaction(self):
                    return actual.in_transaction

                def execute(self, sql, *args):
                    return Cursor(actual.execute(sql, *args), sql.startswith("SELECT COUNT(*)"))

                def rollback(self):
                    actual.rollback()

                def close(self):
                    actual.close()

            db.db = Connection()
            result = self.report(db, 60)
            self.assertEqual(result["scan"]["total_group_rows"], 2)
            self.assertEqual(result["scan"]["scanned_rows"], 2)
            self.assertFalse(actual.in_transaction)
            self.assertEqual(actual.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 3)

    def test_cli_rejects_invalid_time_and_store_without_echoing_private_values(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
        args = ["signal-report", "--db", str(self.path), "--store-id", "900001", "--api-profile",
                "legacy", "--data-origin", "synthetic", "--as-of", "DO_NOT_ECHO"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(args), 1)
        self.assertEqual(json.loads(out.getvalue())["error_code"], "invalid_signal_time")
        self.assertNotIn("DO_NOT_ECHO", out.getvalue())
        args[args.index("900001")] = "PRIVATE_IDENTIFIER"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(args), 2)
        self.assertEqual(json.loads(out.getvalue())["error_code"], "invalid_store_id")
        self.assertNotIn("PRIVATE_IDENTIFIER", out.getvalue())

    def test_read_only_legacy_view_does_not_migrate_or_invent_gateway_rows(self):
        old = sample(0)
        del old["api_profile"]
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,"
                "store_id TEXT NOT NULL,data_origin TEXT NOT NULL,received_at TEXT NOT NULL,"
                "ok INTEGER NOT NULL,payload_json TEXT NOT NULL)")
            connection.execute("INSERT INTO samples VALUES(1,'synthetic-run','900001','synthetic',?,1,?)",
                               (stamp(0), json.dumps(old)))
            connection.execute("PRAGMA user_version=1")
        before = self.path.read_bytes()
        with SnapshotStore(self.path, read_only=True) as db:
            self.assertEqual(self.report(db, 0)["scan"]["total_group_rows"], 1)
            gateway = signal_report(db, "900001", data_origin="synthetic", api_profile="miniapp_gateway", as_of=stamp(0))
            self.assertEqual(gateway["scan"]["total_group_rows"], 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_is_read_only_with_zero_network_client_and_credential_calls(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(0))
            db.save(sample(30))
        before = self.path.read_bytes()
        out = io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("sushiwait.cli.client_for", side_effect=AssertionError("client")), \
             patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("credentials")), \
             contextlib.redirect_stdout(out):
            code = main(["signal-report", "--db", str(self.path), "--store-id", "900001",
                         "--api-profile", "legacy", "--data-origin", "synthetic", "--as-of", stamp(30)])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out.getvalue())["network_performed"])
        self.assertEqual(self.path.read_bytes(), before)
        missing = Path(self.tmp.name) / "missing.sqlite3"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["signal-report", "--db", str(missing), "--store-id", "900001",
                "--api-profile", "legacy", "--data-origin", "synthetic", "--as-of", stamp(30)]), 1)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
