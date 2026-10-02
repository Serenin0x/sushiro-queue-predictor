"""Local storage for normalized public observations, never raw HTTP responses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .observations import _validate_api_profile


_LEGACY_COLUMNS = {
    "id": "INTEGER", "run_id": "TEXT", "store_id": "TEXT", "data_origin": "TEXT",
    "received_at": "TEXT", "ok": "INTEGER", "payload_json": "TEXT",
}
_CURRENT_COLUMNS = {**_LEGACY_COLUMNS, "api_profile": "TEXT"}


def _legacy_payload(value: str) -> dict:
    """Label known v1 data; conflicting labels must never be overwritten."""
    try:
        payload = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid_legacy_sample") from None
    if not isinstance(payload, dict) or payload.get("api_profile", "legacy") != "legacy":
        raise ValueError("invalid_legacy_sample")
    return {**payload, "api_profile": "legacy"}


class SnapshotStore:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("database_symlink_not_allowed")
        if read_only:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            if os.name == "posix":
                self.path.chmod(0o600)
            self.db = sqlite3.connect(self.path)
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in (0, 1, 2) or (version == 0 and (read_only or tables)):
                raise ValueError("unsupported_database_schema")
            self.run_id = str(uuid4())
            self._schema_version = version
            if version == 0:
                self._create_schema()
            else:
                self._check_columns(_LEGACY_COLUMNS if version == 1 else _CURRENT_COLUMNS)
                if version == 1 and not read_only:
                    self._migrate_legacy()
                elif version == 2:
                    invalid = self.db.execute(
                        "SELECT 1 FROM samples WHERE api_profile NOT IN ('legacy','miniapp_gateway') "
                        "OR api_profile IS NULL LIMIT 1"
                    ).fetchone()
                    if invalid is not None:
                        raise ValueError("invalid_database_api_profile")
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _check_columns(self, expected: dict[str, str]) -> None:
        actual = {
            row[1]: row[2].upper()
            for row in self.db.execute("PRAGMA table_info(samples)").fetchall()
        }
        if actual != expected:
            raise ValueError("unsupported_database_schema")

    def _create_schema(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("""CREATE TABLE samples (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            data_origin TEXT NOT NULL CHECK (data_origin IN ('live','fixture','synthetic')),
            api_profile TEXT NOT NULL CHECK (api_profile IN ('legacy','miniapp_gateway')),
            received_at TEXT NOT NULL,
            ok INTEGER NOT NULL CHECK (ok IN (0,1)),
            payload_json TEXT NOT NULL
        )""")
        self._create_profile_index()
        self.db.execute("PRAGMA user_version = 2")
        self.db.commit()
        self._schema_version = 2

    def _create_profile_index(self) -> None:
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS sample_profile_lookup "
            "ON samples(store_id,data_origin,api_profile,id)"
        )

    def _migrate_legacy(self) -> None:
        """Atomically preserve v1 sample IDs, metadata and payloads as legacy."""
        self.db.execute("BEGIN IMMEDIATE")
        rows = self.db.execute("SELECT id,payload_json FROM samples ORDER BY id").fetchall()
        converted = [
            (json.dumps(_legacy_payload(payload), ensure_ascii=False, allow_nan=False), sample_id)
            for sample_id, payload in rows
        ]
        self.db.execute(
            "ALTER TABLE samples ADD COLUMN api_profile TEXT NOT NULL DEFAULT 'legacy' "
            "CHECK (api_profile IN ('legacy','miniapp_gateway'))"
        )
        self.db.executemany("UPDATE samples SET payload_json=? WHERE id=?", converted)
        self._create_profile_index()
        self.db.execute("PRAGMA user_version = 2")
        self.db.commit()
        self._schema_version = 2

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.db.close()

    def save(self, snapshot: dict[str, Any]) -> int:
        profile = _validate_api_profile(snapshot.get("api_profile", "legacy"))
        snapshot = {**snapshot, "api_profile": profile}
        return self._insert(snapshot["store_id"], snapshot["data_origin"],
                            profile, snapshot["timing"]["received_at"], True, snapshot)

    def save_failure(
        self, store_id: str, result: Any, *, data_origin: str = "live",
        api_profile: str = "legacy",
    ) -> int:
        # Only locally defined error codes and timings may reach disk.
        api_profile = _validate_api_profile(api_profile)
        failure = {"store_id": store_id, "data_origin": data_origin, "api_profile": api_profile,
                   "error_code": result.error_code, "http_status": result.http_status,
                   "timing": {"request_started_at": result.started_at,
                              "received_at": result.received_at, "elapsed_ms": result.elapsed_ms}}
        return self._insert(store_id, data_origin, api_profile, result.received_at, False, failure)

    def _insert(
        self, store_id: str, origin: str, api_profile: str, received_at: str,
        ok: bool, payload: dict,
    ) -> int:
        api_profile = _validate_api_profile(api_profile)
        cur = self.db.execute(
            "INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.run_id, str(store_id), origin, api_profile, received_at, int(ok),
             json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        self.db.commit()
        return int(cur.lastrowid)

    def last(
        self, store_id: str, *, data_origin: str, api_profile: str = "legacy",
    ) -> dict | None:
        api_profile = _validate_api_profile(api_profile)
        if self._schema_version == 1:
            # A read-only v1 connection presents a labelled legacy view without
            # changing the database. It contains no gateway history.
            if api_profile != "legacy":
                return None
            row = self.db.execute(
                "SELECT payload_json FROM samples WHERE store_id=? AND data_origin=? "
                "AND ok=1 ORDER BY id DESC LIMIT 1", (str(store_id), data_origin)
            ).fetchone()
            return _legacy_payload(row[0]) if row else None
        row = self.db.execute(
            "SELECT payload_json FROM samples WHERE store_id=? AND data_origin=? AND api_profile=? "
            "AND ok=1 ORDER BY id DESC LIMIT 1",
            (str(store_id), data_origin, api_profile)).fetchone()
        return json.loads(row[0]) if row else None

    def report(self) -> dict:
        profile = "'legacy'" if self._schema_version == 1 else "api_profile"
        rows = self.db.execute(
            f"SELECT store_id,data_origin,{profile},COUNT(*),SUM(ok),MIN(received_at),MAX(received_at) "
            f"FROM samples GROUP BY store_id,data_origin,{profile} "
            f"ORDER BY store_id,data_origin,{profile}").fetchall()
        return {"schema_version": 2, "database_schema_version": self._schema_version, "groups": [
            {"store_id": r[0], "data_origin": r[1], "api_profile": r[2], "samples": r[3],
             "successful_samples": r[4], "failed_samples": r[3]-r[4],
             "first_received_at": r[5], "last_received_at": r[6],
             "upstream_freshness": "unknown"} for r in rows],
            "limitations": ["采样成功不代表源数据刚更新", "本报告不计算等待时间或真实过号率"]}
