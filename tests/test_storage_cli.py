import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.client import QueryResult
from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE1 = ROOT / "examples/fixtures/store-detail-01.synthetic.json"
FIXTURE2 = ROOT / "examples/fixtures/store-detail-02.synthetic.json"


def synthetic():
    f = json.loads(FIXTURE1.read_text())
    return normalize_snapshot(f["payload"], f["store_id"],
        request_started_at=f["observed_at"], received_at=f["observed_at"],
        elapsed_ms=0, data_origin="synthetic")


class StorageCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "observations.sqlite3"

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(args)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_replay_is_offline_and_survives_reopening(self):
        with patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("must stay offline")):
            code, rows = self.run_cli(["replay", "--fixture", str(FIXTURE1), "--fixture", str(FIXTURE2), "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["snapshot"]["data_origin"], "synthetic")
        with SnapshotStore(self.db_path) as db:
            summary = db.report()["groups"][0]
            self.assertEqual(summary["successful_samples"], 2)
            self.assertIsNone(db.last("900001", data_origin="live"))
            self.assertEqual(db.last("900001", data_origin="synthetic")["normalized"]["raw_wait"]["value"], 0)

    def test_live_and_synthetic_history_are_isolated(self):
        a = synthetic()
        b = dict(a, data_origin="live")
        with SnapshotStore(self.db_path) as db:
            db.save(a)
            db.save(b)
            self.assertEqual(db.last("900001", data_origin="synthetic")["data_origin"], "synthetic")
            self.assertEqual(db.last("900001", data_origin="live")["data_origin"], "live")
            self.assertEqual(len(db.report()["groups"]), 2)

    def test_collect_stops_after_rate_limit_without_retry_or_sleep(self):
        result = QueryResult(False, None, "http_error", 429,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory, patch("sushiwait.cli.time.sleep") as sleep:
            factory.return_value.fetch_store.return_value = result
            code, rows = self.run_cli(["collect", "--store-id", "123", "--store-id", "456", "--samples", "3", "--db", str(self.db_path)])
            self.assertEqual(code, 1)
            factory.return_value.fetch_store.assert_called_once_with("123")
            sleep.assert_not_called()
        self.assertEqual(rows[0]["http_status"], 429)
        with SnapshotStore(self.db_path) as db:
            self.assertIsNone(db.last("123", data_origin="live"))
            self.assertEqual(db.report()["groups"][0]["failed_samples"], 1)

    def test_stores_match_uses_normalized_public_name(self):
        result = QueryResult(True, {"data":[{"id":123,"name":"测试中关村大融城店"},{"id":456,"name":"其他测试店"}]}, None, 200,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory:
            factory.return_value.fetch_stores.return_value = result
            code, rows = self.run_cli(["stores", "--match", "中关村"])
        self.assertEqual(code, 0)
        self.assertEqual([s["store_id"] for s in rows[0]["stores"]], ["123"])

    def test_oversampling_rejected_before_network(self):
        with patch("sushiwait.cli.SushiroClient") as factory:
            code, _ = self.run_cli(["collect", "--store-id", "123", "--interval", "1", "--db", str(self.db_path)])
            factory.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_unmarked_fixture_rejected_without_partial_insert(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"payload":{"id":1,"name":"private"}}')
        code, _ = self.run_cli(["replay", "--fixture", str(FIXTURE1), "--fixture", str(bad), "--db", str(self.db_path)])
        self.assertEqual(code, 2)
        with SnapshotStore(self.db_path) as db:
            self.assertEqual(db.report()["groups"], [])

    def test_unknown_database_schema_is_not_overwritten(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            SnapshotStore(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_symlink_database_rejected(self):
        target = Path(self.tmp.name) / "real.db"
        target.write_bytes(b"unchanged")
        self.db_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "database_symlink_not_allowed"):
            SnapshotStore(self.db_path)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_missing_report_database_is_not_created(self):
        code, _ = self.run_cli(["report", "--db", str(self.db_path)])
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_report_is_read_only(self):
        with SnapshotStore(self.db_path) as db:
            db.save(synthetic())
        before = self.db_path.read_bytes()
        with SnapshotStore(self.db_path, read_only=True) as db:
            self.assertEqual(db.report()["groups"][0]["samples"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.save(synthetic())
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_unversioned_unrelated_database_is_not_modified(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES ('preserved')")
        before = self.db_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            SnapshotStore(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_leading_zero_ids_share_history_and_are_deduplicated(self):
        a = json.loads(FIXTURE1.read_text())["payload"]
        a["id"] = 12
        result = QueryResult(True, a, None, 200,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory:
            factory.return_value.fetch_store.return_value = result
            code, _ = self.run_cli(["snapshot", "--store-id", "0012", "--db", str(self.db_path)])
            self.assertEqual(code, 0)
            factory.return_value.fetch_store.assert_called_once_with("12")
            factory.return_value.fetch_store.reset_mock()
            code, rows = self.run_cli(["collect", "--store-id", "0012", "--store-id", "12", "--samples", "1", "--db", str(self.db_path)])
            self.assertEqual(code, 0)
            factory.return_value.fetch_store.assert_called_once_with("12")
            self.assertEqual(rows[0]["change"]["comparison"], "comparable")
        with SnapshotStore(self.db_path) as db:
            self.assertEqual([x["store_id"] for x in db.report()["groups"]], ["12"])

    def test_invalid_identity_stops_before_local_or_network_changes(self):
        with patch("sushiwait.cli.SushiroClient") as factory:
            code, _ = self.run_cli(["snapshot", "--store-id", "not-a-real-id", "--db", str(self.db_path)])
            factory.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_report_orders_normalized_offset_times_in_utc(self):
        first = normalize_snapshot({"id":12,"name":"合成店"}, "12", request_started_at="2026-10-02T18:00:00+08:00", received_at="2026-10-02T18:00:00+08:00", elapsed_ms=0, data_origin="synthetic")
        second = normalize_snapshot({"id":12,"name":"合成店"}, "12", request_started_at="2026-10-02T11:00:00Z", received_at="2026-10-02T11:00:00Z", elapsed_ms=0, data_origin="synthetic")
        with SnapshotStore(self.db_path) as db:
            db.save(second)
            db.save(first)
            report = db.report()["groups"][0]
            self.assertEqual(report["first_received_at"], "2026-10-02T10:00:00.000Z")
            self.assertEqual(report["last_received_at"], "2026-10-02T11:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
