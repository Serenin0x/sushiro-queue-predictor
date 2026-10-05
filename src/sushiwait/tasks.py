"""Private bounded collection checkpoints; no credentials, UI or network calls.

A write-ahead attempt identifies its exact SQLite run and sample boundary.
After a crash, a committed result advances once; no result is an unknown gap,
never a fabricated observation or a replay of the possibly completed request.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from .capture import CaptureError, _check_parent, _open_parent
from .credentials import (
    CredentialError, QueryCredentials, _identity, _private_file, _read_private_file,
)
from .storage import _report_payload, _report_snapshot, _safe_error


_MAX_BYTES = 16 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"ready", "running", "stopped", "completed"}
_KEYS = {"schema_version", "config", "database_identity", "cursor", "successful",
         "failed", "uncertain", "credential", "pending", "last_sample",
         "last_attempt_at", "updated_at", "state", "stop_reason", "last_gap"}


class TaskError(ValueError):
    def __init__(self, code: str):
        self.error_code = code
        super().__init__(code)


def _text_time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise TaskError("collection_task_invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise TaskError("collection_task_invalid") from None


def _time(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise TaskError("collection_task_clock_invalid")
    return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _integer(value, lower=0, upper=2**63-1) -> bool:
    return type(value) is int and lower <= value <= upper


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaskError("collection_task_invalid")
        result[key] = value
    return result


def _decode(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or set(value) != _KEYS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        c = value["config"]
        if (not isinstance(c, dict) or set(c) != {"db", "api_profile", "store_ids", "interval", "samples", "wait_for_credentials"}
                or not isinstance(c["db"], str) or len(c["db"]) > 4096 or not Path(c["db"]).is_absolute()
                or c["api_profile"] not in ("legacy", "miniapp_gateway")
                or not isinstance(c["store_ids"], list) or not 1 <= len(c["store_ids"]) <= 3
                or len(set(c["store_ids"])) != len(c["store_ids"])
                or not all(isinstance(s, str) and s.isascii() and s.isdecimal() and str(int(s)) == s
                           and 0 < int(s) <= 2**63-1 for s in c["store_ids"])
                or not _integer(c["interval"], 30, 3600) or not _integer(c["samples"], 1, 120)
                or not _integer(c["wait_for_credentials"], 0, 600)):
            raise ValueError
        total = len(c["store_ids"]) * c["samples"]
        if (not all(_integer(value[k], 0, total) for k in ("cursor", "successful", "failed", "uncertain"))
                or value["cursor"] != sum(value[k] for k in ("successful", "failed", "uncertain"))
                or value["state"] not in _STATES
                or (value["state"] == "completed") != (value["cursor"] == total)
                or not isinstance(value["stop_reason"], (str, type(None)))
                or value["stop_reason"] is not None and value["stop_reason"] != "unknown" and _safe_error(value["stop_reason"]) == "unknown"):
            raise ValueError
        for k in ("updated_at", "last_attempt_at"):
            if value[k] is not None:
                _text_time(value[k])
        if value["updated_at"] is None:
            raise ValueError
        identity = value["database_identity"]
        if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or not all(_integer(i) for i in identity)):
            raise ValueError
        credential = value["credential"]
        if credential is not None:
            if (not isinstance(credential, dict) or set(credential) != {"revision", "context_digest", "authorization_digest", "blocked_authorization_digest"}
                    or not _integer(credential["revision"], 1)
                    or not all(isinstance(credential[k], str) and _HASH.fullmatch(credential[k]) for k in ("context_digest", "authorization_digest"))
                    or credential["blocked_authorization_digest"] is not None and
                       (not isinstance(credential["blocked_authorization_digest"], str) or not _HASH.fullmatch(credential["blocked_authorization_digest"]))):
                raise ValueError
        pending = value["pending"]
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"cursor", "run_id", "after_sample_id", "started_at"}
                    or pending["cursor"] != value["cursor"] or not _integer(pending["cursor"], 0, total-1)
                    or not _integer(pending["after_sample_id"])):
                raise ValueError
            if not isinstance(pending["run_id"], str) or len(pending["run_id"]) != 36:
                raise ValueError
            UUID(pending["run_id"])
            _text_time(pending["started_at"])
            if value["state"] != "running":
                raise ValueError
        last = value["last_sample"]
        if last is not None and (not isinstance(last, dict) or set(last) != {"id", "digest"}
                                 or not _integer(last["id"], 1) or not isinstance(last["digest"], str) or not _HASH.fullmatch(last["digest"])):
            raise ValueError
        gap = value["last_gap"]
        if gap is not None:
            if (not isinstance(gap, dict) or set(gap) != {"from", "to", "reason"}
                    or gap["reason"] not in ("restart", "uncertain_attempt")):
                raise ValueError
            if _text_time(gap["to"]) < _text_time(gap["from"]):
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise TaskError("collection_task_invalid") from None


def public_task(value: dict) -> dict:
    """No paths, hashes, identities, run IDs, raw errors or context values."""
    c = value["config"]
    return {"task_schema_version": 1, "state": value["state"],
            "api_profile": c["api_profile"], "store_ids": c["store_ids"],
            "target_rounds": c["samples"], "target_slots": c["samples"] * len(c["store_ids"]),
            "completed_slots": value["cursor"], "successful_slots": value["successful"],
            "failed_slots": value["failed"], "uncertain_slots": value["uncertain"],
            "interval_seconds": c["interval"], "stop_reason": value["stop_reason"],
            "pending_attempt": value["pending"] is not None,
            "credential_revision": value["credential"]["revision"] if value["credential"] else None,
            "refresh_required": bool(value["credential"] and value["credential"]["blocked_authorization_digest"]),
            "all_slots_successful": value["cursor"] == c["samples"] * len(c["store_ids"]) and value["failed"] == value["uncertain"] == 0,
            "updated_at": value["updated_at"], "last_gap": value["last_gap"],
            "upstream_freshness": "unknown", "eta_available": False}


def task_status(path: str | Path) -> dict:
    try:
        return public_task(_decode(_read_private_file(path)))
    except CredentialError:
        raise TaskError("collection_task_unavailable_or_unsafe") from None


class CollectionTask:
    """Exclusive cooperating worker; atomic 0600 checkpoint and bounded input."""

    def __init__(self, path: str | Path, *, config: dict, resume: bool, now: datetime):
        self.path = Path(os.path.abspath(path))
        self.parent_fd = self.lock_fd = self.database_parent_fd = None
        self.identity = None
        self.loaded = False
        try:
            import fcntl
            self.parent_fd, self.name = _open_parent(self.path, private=True)
            if Path(config["db"]) in (self.path, Path(str(self.path) + ".lock")):
                raise TaskError("collection_task_path_conflict")
            self.lock_name = self.name + ".lock"
            self.lock_fd = os.open(self.lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                                   | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=self.parent_fd)
            info = os.fstat(self.lock_fd)
            if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                raise TaskError("collection_task_unavailable_or_unsafe")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_identity = (info.st_dev, info.st_ino)
            try:
                self.identity = self._named_identity()
            except FileNotFoundError:
                pass
            if self.identity is not None:
                if not resume:
                    raise TaskError("collection_task_exists_use_resume")
                self.value = _decode(_read_private_file(self.path))
                if self.value["database_identity"] is None:
                    raise TaskError("collection_task_invalid")
                if self.value["config"] != config:
                    raise TaskError("collection_task_config_conflict")
                self.loaded = True
            else:
                if resume:
                    raise TaskError("collection_task_missing")
                self.value = {"schema_version": 1, "config": deepcopy(config), "database_identity": None,
                    "cursor": 0, "successful": 0, "failed": 0, "uncertain": 0, "credential": None,
                    "pending": None, "last_sample": None, "last_attempt_at": None,
                    "updated_at": _time(now), "state": "ready", "stop_reason": None, "last_gap": None}
                # Validation only; initial checkpoint is written after binding its database.
                _decode(json.dumps(self.value).encode())
            self._guard()
            self.database_parent_fd, self.database_name = _open_parent(config["db"], private=True)
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise
        except Exception as error:
            self.close()
            if isinstance(error, TaskError):
                raise
            raise TaskError("collection_task_busy" if isinstance(error, BlockingIOError)
                            else "collection_task_unavailable_or_unsafe") from None

    def _named_identity(self):
        info = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskError("collection_task_unavailable_or_unsafe")
        return _identity(info)

    def _guard(self):
        _check_parent(self.path, self.parent_fd, private=True)
        info = os.stat(self.lock_name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != self.lock_identity):
            raise TaskError("collection_task_lock_changed")
        try:
            current = self._named_identity()
        except FileNotFoundError:
            current = None
        if current != self.identity:
            raise TaskError("collection_task_changed")

    def _commit(self, value: dict):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > _MAX_BYTES:
            raise TaskError("collection_task_invalid")
        _decode(body)
        self._guard()
        name = self.name + "." + uuid4().hex + ".tmp"
        fd = None
        renamed = False
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=self.parent_fd)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
            os.fsync(fd)
            self._guard()
            os.replace(name, self.name, src_dir_fd=self.parent_fd, dst_dir_fd=self.parent_fd)
            renamed = True
            self.identity = self._named_identity()
            self.value = value
            os.fsync(self.parent_fd)
        except OSError:
            raise TaskError("collection_task_durability_unconfirmed" if renamed
                            else "collection_task_write_failed") from None
        finally:
            if fd is not None:
                os.close(fd)
            if not renamed:
                try:
                    os.unlink(name, dir_fd=self.parent_fd)
                except FileNotFoundError:
                    pass

    def prepare_database(self):
        """Reject unsafe/missing resumed DBs before SnapshotStore could create/chmod."""
        _check_parent(self.value["config"]["db"], self.database_parent_fd, private=True)
        try:
            info = os.stat(self.database_name, dir_fd=self.database_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if self.loaded:
                raise TaskError("collection_task_database_changed") from None
            return
        if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskError("collection_task_database_unsafe")

    def bind(self, db, *, now: datetime):
        """Pin the DB and last committed row before any new query."""
        info = os.stat(self.database_name, dir_fd=self.database_parent_fd, follow_symlinks=False)
        identity = [info.st_dev, info.st_ino]
        if db._schema_version != 2 or not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskError("collection_task_database_unsafe")
        if self.value["database_identity"] not in (None, identity):
            raise TaskError("collection_task_database_changed")
        last = self.value["last_sample"]
        if last is not None:
            row = db.db.execute("SELECT id,run_id,store_id,data_origin,api_profile,ok,"
                "CASE WHEN length(CAST(payload_json AS BLOB)) <= 2097152 THEN payload_json ELSE NULL END "
                "FROM samples WHERE id=?", (last["id"],)).fetchone()
            if row is None or _digest(row) != last["digest"]:
                raise TaskError("collection_task_database_changed")
        if not self.loaded:
            value = deepcopy(self.value)
            value["database_identity"] = identity
            self._commit(value)

        self.db = db
        self._guard_database()
        if self.loaded and _text_time(_time(now)) < _text_time(self.value["updated_at"]):
            raise TaskError("collection_task_clock_rollback")
        uncertain_before = self.value["uncertain"]
        if self.value["pending"] is not None:
            self.reconcile(now=now, interrupted=True)
        if (self.loaded and self.value["last_attempt_at"] is not None and self.value["state"] != "completed"
                and self.value["uncertain"] == uncertain_before):
            if _text_time(_time(now)) < _text_time(self.value["last_attempt_at"]):
                raise TaskError("collection_task_clock_rollback")
            value = deepcopy(self.value)
            value["last_gap"] = {"from": value["last_attempt_at"], "to": _time(now), "reason": "restart"}
            value["updated_at"] = _time(now)
            self._commit(value)

    def _guard_database(self):
        _check_parent(self.db.path, self.database_parent_fd, private=True)
        info = os.stat(self.database_name, dir_fd=self.database_parent_fd, follow_symlinks=False)
        if (not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600
                or [info.st_dev, info.st_ino] != self.value["database_identity"]):
            raise TaskError("collection_task_database_changed")

    def check_context(self, context: QueryCredentials, stop_code: str | None):
        self._guard()
        self._guard_database()
        old = self.value["credential"]
        current = {"revision": context.revision, "context_digest": _digest(asdict(context)),
                   "authorization_digest": _digest(context.authorization), "blocked_authorization_digest": None}
        if not _integer(context.revision, 1) or context.authorization is None:
            raise CredentialError("credentials_file_invalid")
        if old:
            if current["revision"] < old["revision"]:
                raise CredentialError("credentials_revision_rollback")
            if current["revision"] == old["revision"] and current["context_digest"] != old["context_digest"]:
                raise CredentialError("credentials_revision_conflict")
            if old["blocked_authorization_digest"] == current["authorization_digest"] and stop_code is None:
                raise CredentialError("credentials_refresh_required")
        if stop_code in ("auth_expiring", "auth_declared_expired"):
            current["blocked_authorization_digest"] = current["authorization_digest"]
        if current != old:
            value = deepcopy(self.value)
            value["credential"] = current
            self._commit(value)

    def begin(self, *, now: datetime):
        if self.value["pending"] is not None or self.value["state"] == "completed":
            raise TaskError("collection_task_invalid")
        self._guard_database()
        if self.value["last_attempt_at"] is not None and _text_time(_time(now)) < _text_time(self.value["last_attempt_at"]):
            raise TaskError("collection_task_clock_rollback")
        value = deepcopy(self.value)
        value["pending"] = {"cursor": value["cursor"], "run_id": self.db.run_id,
                            "after_sample_id": self.db.db.execute("SELECT COALESCE(MAX(id),0) FROM samples").fetchone()[0],
                            "started_at": _time(now)}
        value.update(state="running", stop_reason=None, updated_at=_time(now), last_attempt_at=_time(now))
        self._commit(value)

    def reconcile(self, *, now: datetime, interrupted: bool = False):
        p = self.value["pending"]
        if p is None:
            raise TaskError("collection_task_invalid")
        self._guard_database()
        rows = self.db.db.execute("SELECT id,run_id,store_id,data_origin,api_profile,ok,"
            "CASE WHEN length(CAST(payload_json AS BLOB)) <= 2097152 THEN payload_json ELSE NULL END "
            "FROM samples WHERE run_id=? AND id>? ORDER BY id LIMIT 2", (p["run_id"], p["after_sample_id"])).fetchall()
        value = deepcopy(self.value)
        expected_store = value["config"]["store_ids"][value["cursor"] % len(value["config"]["store_ids"])]
        if len(rows) > 1 or not rows and not interrupted:
            raise TaskError("collection_task_result_conflict")
        if rows:
            row = rows[0]
            payload = _report_payload(row[6])
            if (row[2:5] != (expected_store, "live", value["config"]["api_profile"])
                    or payload is None or row[5] not in (0, 1)):
                raise TaskError("collection_task_result_conflict")
            if row[5]:
                if _report_snapshot(payload, expected_store, "live", value["config"]["api_profile"]) is None:
                    raise TaskError("collection_task_result_conflict")
                value["successful"] += 1
            else:
                if (payload.get("store_id") != expected_store or payload.get("api_profile") != value["config"]["api_profile"]
                        or payload.get("failure_phase") not in ("request", "normalization")):
                    raise TaskError("collection_task_result_conflict")
                value["failed"] += 1
                value["stop_reason"] = _safe_error(payload.get("error_code"))
                if payload.get("http_status") == 401 and value["credential"]:
                    value["credential"]["blocked_authorization_digest"] = value["credential"]["authorization_digest"]
            value["last_sample"] = {"id": row[0], "digest": _digest(row)}
        else:
            if _text_time(_time(now)) < _text_time(p["started_at"]):
                raise TaskError("collection_task_clock_rollback")
            value["uncertain"] += 1
            value["last_gap"] = {"from": p["started_at"], "to": _time(now), "reason": "uncertain_attempt"}
        value["cursor"] += 1
        value["pending"] = None
        total = len(value["config"]["store_ids"]) * value["config"]["samples"]
        value["state"] = "completed" if value["cursor"] == total else "stopped" if value["stop_reason"] else "ready"
        value["updated_at"] = _time(now)
        self._commit(value)

    def stop(self, code: str, *, now: datetime):
        value = deepcopy(self.value)
        value["state"] = "stopped"
        value["stop_reason"] = _safe_error(code)
        value["updated_at"] = _time(now)
        self._commit(value)

    def close(self):
        for attr in ("lock_fd", "parent_fd", "database_parent_fd"):
            fd = getattr(self, attr, None)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
