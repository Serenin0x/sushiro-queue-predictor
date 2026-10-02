"""Local storage for normalized public observations, never raw HTTP responses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4


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
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version not in (0, 1) or (version == 0 and (read_only or tables)):
            self.db.close()
            raise ValueError("unsupported_database_schema")
        self.run_id = str(uuid4())
        if read_only:
            return
        self.db.execute("""CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            data_origin TEXT NOT NULL CHECK (data_origin IN ('live','fixture','synthetic')),
            received_at TEXT NOT NULL,
            ok INTEGER NOT NULL CHECK (ok IN (0,1)),
            payload_json TEXT NOT NULL
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS sample_lookup ON samples(store_id,data_origin,id)")
        self.db.execute("PRAGMA user_version = 1")
        self.db.commit()

    def __enter__(self) -> SnapshotStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.db.close()

    def save(self, snapshot: dict[str, Any]) -> int:
        return self._insert(snapshot["store_id"], snapshot["data_origin"],
                            snapshot["timing"]["received_at"], True, snapshot)

    def save_failure(self, store_id: str, result: Any, *, data_origin: str = "live") -> int:
        # Only locally defined error codes and timings may reach disk.
        failure = {"store_id": store_id, "data_origin": data_origin,
                   "error_code": result.error_code, "http_status": result.http_status,
                   "timing": {"request_started_at": result.started_at,
                              "received_at": result.received_at, "elapsed_ms": result.elapsed_ms}}
        return self._insert(store_id, data_origin, result.received_at, False, failure)

    def _insert(self, store_id: str, origin: str, received_at: str, ok: bool, payload: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO samples(run_id,store_id,data_origin,received_at,ok,payload_json) VALUES(?,?,?,?,?,?)",
            (self.run_id, str(store_id), origin, received_at, int(ok),
             json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        self.db.commit()
        return int(cur.lastrowid)

    def last(self, store_id: str, *, data_origin: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload_json FROM samples WHERE store_id=? AND data_origin=? AND ok=1 ORDER BY id DESC LIMIT 1",
            (str(store_id), data_origin)).fetchone()
        return json.loads(row[0]) if row else None

    def report(self) -> dict:
        rows = self.db.execute(
            "SELECT store_id,data_origin,COUNT(*),SUM(ok),MIN(received_at),MAX(received_at) "
            "FROM samples GROUP BY store_id,data_origin ORDER BY store_id,data_origin").fetchall()
        return {"schema_version": 1, "groups": [
            {"store_id": r[0], "data_origin": r[1], "samples": r[2],
             "successful_samples": r[3], "failed_samples": r[2]-r[3],
             "first_received_at": r[4], "last_received_at": r[5],
             "upstream_freshness": "unknown"} for r in rows],
            "limitations": ["采样成功不代表源数据刚更新", "本报告不计算等待时间或真实过号率"]}
