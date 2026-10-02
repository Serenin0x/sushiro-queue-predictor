"""Synthetic source isolation and transactional v1 migration tests."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.client import QueryResult
from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore


def sample(profile="legacy", *, origin="synthetic", label="A001"):
    return normalize_snapshot(
        {"id": 12, "name": "合成门店", "wait": 4,
         "groupQueues": {"mixedQueue": [label]}},
        "12", request_started_at="2026-10-02T10:00:00Z",
        received_at="2026-10-02T10:00:00Z", elapsed_ms=0,
        data_origin=origin, api_profile=profile,
    )


def failure():
    return QueryResult(
        False, None, "http_error", 401,
        "2026-10-02T10:01:00.000Z", "2026-10-02T10:01:01.000Z", 1000,
    )


def create_legacy_database(path, payloads):
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE samples (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, store_id TEXT NOT NULL,
            data_origin TEXT NOT NULL CHECK (data_origin IN ('live','fixture','synthetic')),
            received_at TEXT NOT NULL, ok INTEGER NOT NULL CHECK (ok IN (0,1)),
            payload_json TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX sample_lookup ON samples(store_id,data_origin,id)")
        for index, (ok, payload) in enumerate(payloads):
            encoded = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?)",
                       (5 + index * 4, "legacy-run", "12", "synthetic",
                        f"2026-10-02T10:0{index}:00.000Z", ok, encoded))
        db.execute("PRAGMA user_version = 1")


class StorageProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "observations.sqlite3"

    def test_latest_success_is_isolated_by_profile_and_origin(self):
        with SnapshotStore(self.path) as db:
            db.save(sample(label="L001"))
            db.save(sample("miniapp_gateway", label="G001"))
            db.save(sample("miniapp_gateway", origin="fixture", label="F001"))
            db.save_failure("12", failure(), data_origin="synthetic", api_profile="miniapp_gateway")
            legacy = db.last("12", data_origin="synthetic")
            gateway = db.last("12", data_origin="synthetic", api_profile="miniapp_gateway")
            fixture = db.last("12", data_origin="fixture", api_profile="miniapp_gateway")
            self.assertEqual(legacy["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"], ["L001"])
            self.assertEqual(gateway["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"], ["G001"])
            self.assertEqual(fixture["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"], ["F001"])
            self.assertIsNone(db.last("12", data_origin="fixture", api_profile="legacy"))
        with SnapshotStore(self.path, read_only=True) as db:
            self.assertEqual(db.last("12", data_origin="synthetic", api_profile="miniapp_gateway")["api_profile"], "miniapp_gateway")

    def test_report_and_failure_payloads_have_fixed_profile_labels(self):
        with SnapshotStore(self.path) as db:
            db.save(sample())
            db.save(sample("miniapp_gateway"))
            db.save_failure("12", failure(), data_origin="synthetic")
            db.save_failure("12", failure(), data_origin="synthetic", api_profile="miniapp_gateway")
            report = db.report()
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["database_schema_version"], 2)
            groups = {item["api_profile"]: item for item in report["groups"]}
            self.assertEqual(set(groups), {"legacy", "miniapp_gateway"})
            for group in groups.values():
                self.assertEqual(group["successful_samples"], 1)
                self.assertEqual(group["failed_samples"], 1)
                self.assertEqual(group["upstream_freshness"], "unknown")
            rows = db.db.execute("SELECT api_profile,payload_json FROM samples WHERE ok=0 ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in rows], ["legacy", "miniapp_gateway"])
            for profile, encoded in rows:
                self.assertEqual(json.loads(encoded)["api_profile"], profile)

    def test_unknown_profiles_are_rejected_without_inserting_or_echoing_them(self):
        private_profile = "PRIVATE_PROFILE_VALUE"
        with SnapshotStore(self.path) as db:
            bad = sample()
            bad["api_profile"] = private_profile
            actions = (
                lambda: db.save(bad),
                lambda: db.save_failure("12", failure(), api_profile=private_profile),
                lambda: db.last("12", data_origin="synthetic", api_profile=private_profile),
            )
            for action in actions:
                with self.assertRaisesRegex(ValueError, "invalid_api_profile") as raised:
                    action()
                self.assertNotIn(private_profile, str(raised.exception))
            self.assertEqual(db.report()["groups"], [])
            with self.assertRaises(sqlite3.IntegrityError):
                db.db.execute(
                    "INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
                    "VALUES('test','12','synthetic','unrecognized','now',1,'{}')"
                )

    def test_untagged_legacy_save_adds_label_without_mutating_caller(self):
        old = sample()
        del old["api_profile"]
        with SnapshotStore(self.path) as db:
            db.save(old)
            self.assertNotIn("api_profile", old)
            self.assertEqual(db.last("12", data_origin="synthetic")["api_profile"], "legacy")
            self.assertIsNone(db.last("12", data_origin="synthetic", api_profile="miniapp_gateway"))

    def test_v1_migration_preserves_success_failures_ids_runs_and_public_payloads(self):
        old = sample(label="L123")
        del old["api_profile"]
        failed = {"store_id": "12", "data_origin": "synthetic", "error_code": "http_error",
                  "http_status": 401, "timing": {"received_at": "2026-10-02T10:01:00.000Z"}}
        create_legacy_database(self.path, [(1, old), (0, failed)])
        with SnapshotStore(self.path) as db:
            self.assertEqual(db.db.execute("PRAGMA user_version").fetchone()[0], 2)
            migrated = db.last("12", data_origin="synthetic")
            self.assertEqual(migrated, {**old, "api_profile": "legacy"})
            self.assertIsNone(db.last("12", data_origin="synthetic", api_profile="miniapp_gateway"))
            rows = db.db.execute("SELECT id,run_id,api_profile,ok,payload_json FROM samples ORDER BY id").fetchall()
            self.assertEqual([(r[0], r[1], r[2], r[3]) for r in rows],
                             [(5, "legacy-run", "legacy", 1), (9, "legacy-run", "legacy", 0)])
            self.assertEqual(json.loads(rows[1][4]), {**failed, "api_profile": "legacy"})
            summary = db.report()["groups"][0]
            self.assertEqual(summary["samples"], 2)
            self.assertEqual(summary["failed_samples"], 1)
            db.save(sample("miniapp_gateway", label="G123"))
            self.assertEqual(db.last("12", data_origin="synthetic")["normalized"], old["normalized"])
        with SnapshotStore(self.path, read_only=True) as db:
            self.assertEqual(len(db.report()["groups"]), 2)

    def test_read_only_v1_view_labels_legacy_without_migration_or_file_changes(self):
        old = sample()
        del old["api_profile"]
        create_legacy_database(self.path, [(1, old)])
        before = self.path.read_bytes()
        with SnapshotStore(self.path, read_only=True) as db:
            self.assertEqual(db.report()["database_schema_version"], 1)
            self.assertEqual(db.report()["groups"][0]["api_profile"], "legacy")
            self.assertEqual(db.last("12", data_origin="synthetic")["api_profile"], "legacy")
            self.assertIsNone(db.last("12", data_origin="synthetic", api_profile="miniapp_gateway"))
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("api_profile", [row[1] for row in db.execute("PRAGMA table_info(samples)")])

    def test_v1_conflicting_profile_or_invalid_json_aborts_atomically(self):
        for old in ({"api_profile": "miniapp_gateway"}, "not valid JSON"):
            with self.subTest(old=old):
                path = self.path.with_name("conflict" + str(len(str(old))) + ".sqlite3")
                create_legacy_database(path, [(1, old)])
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "invalid_legacy_sample"):
                    SnapshotStore(path)
                self.assertEqual(path.read_bytes(), before)
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertNotIn("api_profile", [r[1] for r in db.execute("PRAGMA table_info(samples)")])
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)

    def test_v1_database_error_after_alter_rolls_back_ddl_and_payload_updates(self):
        old = sample()
        del old["api_profile"]
        create_legacy_database(self.path, [(1, old)])
        before = self.path.read_bytes()
        with patch.object(SnapshotStore, "_create_profile_index", side_effect=sqlite3.OperationalError("synthetic failure")):
            with self.assertRaises(sqlite3.OperationalError):
                SnapshotStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("api_profile", [r[1] for r in db.execute("PRAGMA table_info(samples)")])
            self.assertEqual(json.loads(db.execute("SELECT payload_json FROM samples").fetchone()[0]), old)

    def test_versioned_but_unrelated_schema_is_rejected_without_overwriting(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE samples(private TEXT)")
            db.execute("INSERT INTO samples VALUES('PRESERVED_VALUE')")
            db.execute("PRAGMA user_version = 1")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            SnapshotStore(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
