"""Private, append-only, self-reported outcome records. No booking or prediction.

Input observations are not independently verified. A checked/imported record
is never automatically a verified training label; synthetic data stays separate.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
from uuid import UUID

from .capture import CaptureError, _check_parent, _open_parent
from .credentials import CredentialError, _identity, _private_file, _read_private_file

MAX_INPUT_BYTES = 16 * 1024
_FIELDS = frozenset({"schema_version", "episode_id", "revision", "supersedes_revision",
    "store_id", "api_profile", "data_origin", "queue_type", "party_size", "table_type",
    "recorded_at", "events"})
_EVENT_FIELDS = frozenset({"event_id", "event_type", "event_time_lower", "event_time_upper",
    "observed_at", "evidence_kind", "verification_status"})
_EVENT_TYPES = frozenset({"issued", "checked_in", "called", "no_show", "cancelled",
    "seated", "observation_ended"})
_TERMINALS = frozenset({"no_show", "cancelled", "seated", "observation_ended"})
_COLUMNS = {"id": "INTEGER", "episode_id": "TEXT", "revision": "INTEGER",
    "store_id": "TEXT", "data_origin": "TEXT", "api_profile": "TEXT", "payload_json": "TEXT"}
_ERRORS = frozenset({"outcome_invalid_json", "outcome_invalid_record", "outcome_invalid_time",
    "outcome_time_order", "outcome_future_record", "outcome_input_unavailable",
    "outcome_input_unsafe", "outcome_input_too_large", "outcome_input_changed",
    "outcome_unsupported", "outcome_fixture_origin", "outcome_database_unsafe",
    "outcome_database_changed", "outcome_database_schema", "outcome_database_error",
    "outcome_database_busy", "outcome_revision_conflict", "outcome_episode_conflict",
    "outcome_invalid_limit"})


class OutcomeError(ValueError):
    def __init__(self, code: str):
        self.error_code = code if code in _ERRORS else "outcome_invalid_record"
        super().__init__(self.error_code)


def _database_error(error: Exception) -> str:
    if isinstance(error, OutcomeError):
        return error.error_code
    if isinstance(error, CaptureError):
        if error.error_code == "capture_unsupported":
            return "outcome_unsupported"
        if error.error_code == "capture_destination_changed":
            return "outcome_database_changed"
        return "outcome_database_unsafe"
    if isinstance(error, BlockingIOError) or (isinstance(error, sqlite3.Error)
            and getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
        return "outcome_database_busy"
    if isinstance(error, OSError) and error.errno in (errno.ELOOP, errno.ENOTDIR):
        return "outcome_database_unsafe"
    return "outcome_database_error"


def _json(body: bytes | str) -> dict:
    def unique(pairs: list) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def reject(value: str) -> None:
        raise ValueError("constant")

    failed = False
    try:
        value = json.loads(body, object_pairs_hook=unique, parse_constant=reject)
        if not isinstance(value, dict):
            failed = True
    except (ValueError, UnicodeError, RecursionError, OverflowError, TypeError):
        failed = True
    # JSONDecodeError.doc may contain private data; don't attach that exception.
    if failed:
        raise OutcomeError("outcome_invalid_json")
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value):
        raise OutcomeError("outcome_invalid_time")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    raise OutcomeError("outcome_invalid_time")


def _utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid(value: Any) -> str:
    try:
        parsed = UUID(value) if isinstance(value, str) and len(value) == 36 else None
        if parsed is not None and parsed.version == 4 and str(parsed) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise OutcomeError("outcome_invalid_record")


def validate_episode(value: dict, *, now: datetime | None = None) -> dict:
    """Return canonical private data, not a public report or verified label."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise OutcomeError("outcome_invalid_record")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise OutcomeError("outcome_invalid_record")
    revision = value["revision"]
    if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
        raise OutcomeError("outcome_invalid_record")
    supersedes = value["supersedes_revision"]
    if (revision == 1 and supersedes is not None) or (revision > 1 and
            (type(supersedes) is not int or supersedes != revision - 1)):
        raise OutcomeError("outcome_revision_conflict")
    store = value["store_id"]
    if not isinstance(store, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", store) or int(store) > 2**31 - 1:
        raise OutcomeError("outcome_invalid_record")
    if (value["api_profile"] not in ("legacy", "miniapp_gateway")
            or value["data_origin"] not in ("self_reported", "synthetic")
            or value["queue_type"] not in ("ordinary", "reservation", "unknown")
            or value["table_type"] not in ("booth", "counter", "either", "unknown")
            or value["party_size"] is not None and
                (type(value["party_size"]) is not int or not 1 <= value["party_size"] <= 2**31 - 1)):
        raise OutcomeError("outcome_invalid_record")
    recorded = _time(value["recorded_at"])
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise OutcomeError("outcome_invalid_time")
    if recorded > clock:
        raise OutcomeError("outcome_future_record")
    events = value["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 7:
        raise OutcomeError("outcome_invalid_record")
    normalized, kinds, identities = [], set(), set()
    earliest_possible = None
    for event in events:
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
            raise OutcomeError("outcome_invalid_record")
        kind = event["event_type"]
        if not isinstance(kind, str) or kind not in _EVENT_TYPES or kind in kinds:
            raise OutcomeError("outcome_invalid_record")
        identifier = _uuid(event["event_id"])
        if identifier in identities:
            raise OutcomeError("outcome_invalid_record")
        if event["verification_status"] != "unverified" or event["evidence_kind"] != (
                "synthetic" if value["data_origin"] == "synthetic" else "self_observation"):
            raise OutcomeError("outcome_invalid_record")
        lower, upper, observed = (_time(event[key]) for key in
            ("event_time_lower", "event_time_upper", "observed_at"))
        if lower > upper or upper > observed or observed > recorded:
            raise OutcomeError("outcome_time_order")
        earliest_possible = lower if earliest_possible is None else max(lower, earliest_possible)
        if earliest_possible > upper:
            raise OutcomeError("outcome_time_order")
        normalized.append({**event, "event_time_lower": _utc(lower),
            "event_time_upper": _utc(upper), "observed_at": _utc(observed)})
        kinds.add(kind)
        identities.add(identifier)
    if normalized[0]["event_type"] != "issued" or len(kinds & _TERMINALS) > 1:
        raise OutcomeError("outcome_invalid_record")
    if kinds & _TERMINALS and normalized[-1]["event_type"] not in _TERMINALS:
        raise OutcomeError("outcome_invalid_record")
    return {**value, "episode_id": _uuid(value["episode_id"]),
        "recorded_at": _utc(recorded), "events": normalized}


def candidate_targets(episode: dict) -> dict:
    """Interval arithmetic for later review; never fills missing calls with zero."""
    events = {event["event_type"]: event for event in episode["events"]}

    def interval(start: str, end: str) -> dict | None:
        if start not in events or end not in events:
            return None
        a, b = events[start], events[end]
        return {"lower_seconds": max(0.0, (_time(b["event_time_lower"]) - _time(a["event_time_upper"])).total_seconds()),
            "upper_seconds": (_time(b["event_time_upper"]) - _time(a["event_time_lower"])).total_seconds()}

    return {"called_wait": interval("issued", "called"),
        "called_to_seated": interval("called", "seated"),
        "right_censored_without_call": "called" not in events and "observation_ended" in events,
        "training_eligible": False, "authenticity_verified": False}


def public_summary(episode: dict) -> dict:
    targets = candidate_targets(episode)
    return {"schema_version": 1, "data_origin": episode["data_origin"], "revision": episode["revision"],
        "event_count": len(episode["events"]),
        "has_called_observation": targets["called_wait"] is not None,
        "has_call_to_seat_observations": targets["called_to_seated"] is not None,
        "right_censored_without_call": targets["right_censored_without_call"],
        "training_eligible": False, "authenticity_verified": False,
        "revision_order_verified": False, "network_performed": False}


def read_episode(path: str | Path, *, synthetic: bool = False, now: datetime | None = None) -> dict:
    if not synthetic:
        try:
            body = _read_private_file(path)
        except CredentialError as error:
            mapping = {"credentials_file_unsupported": "outcome_unsupported",
                "credentials_file_too_large": "outcome_input_too_large",
                "credentials_file_unsafe": "outcome_input_unsafe",
                "credentials_file_changed": "outcome_input_changed"}
            raise OutcomeError(mapping.get(error.error_code, "outcome_input_unavailable")) from None
    else:
        fd = None
        code = None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise OutcomeError("outcome_input_unsafe")
            if before.st_size > MAX_INPUT_BYTES:
                raise OutcomeError("outcome_input_too_large")
            pieces, remaining = [], MAX_INPUT_BYTES + 1
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                pieces.append(chunk)
                remaining -= len(chunk)
            body = b"".join(pieces)
            if len(body) > MAX_INPUT_BYTES:
                raise OutcomeError("outcome_input_too_large")
            if _identity(before) != _identity(os.fstat(fd)) or len(body) != before.st_size:
                raise OutcomeError("outcome_input_changed")
        except OutcomeError as error:
            code = error.error_code
        except (OSError, ValueError, TypeError):
            code = "outcome_input_unavailable"
        finally:
            if fd is not None:
                os.close(fd)
        if code:
            raise OutcomeError(code)
    episode = validate_episode(_json(body), now=now)
    if synthetic and episode["data_origin"] != "synthetic":
        raise OutcomeError("outcome_fixture_origin")
    return episode


class OutcomeStore:
    """Separate private SQLite, serialized cooperating writers, no row overwrite.

    Existing parent must be owned 0700; files owned 0600, single-link, no symlinks.
    Same-user uncooperative filesystem changes remain outside this guarantee.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(os.path.abspath(path))
        self.read_only = read_only
        self.parent_fd = None
        self.db = None
        failure = None
        try:
            import fcntl
            self.parent_fd, self.name = _open_parent(self.path, private=True)
            fcntl.flock(self.parent_fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
            fd = os.open(self.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.parent_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise OutcomeError("outcome_database_unsafe")
                self.identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                uri=True, timeout=0)
            self.db.execute("PRAGMA trusted_schema=OFF")
            self._guard()
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            objects = self.db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and not objects and not read_only:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("CREATE TABLE episodes(id INTEGER PRIMARY KEY,episode_id TEXT NOT NULL,"
                    "revision INTEGER NOT NULL,store_id TEXT NOT NULL,data_origin TEXT NOT NULL,"
                    "api_profile TEXT NOT NULL,payload_json TEXT NOT NULL)")
                self.db.execute("CREATE UNIQUE INDEX outcome_episode_revision ON episodes(episode_id,revision)")
                self.db.execute("CREATE INDEX outcome_latest ON episodes(episode_id,id)")
                self.db.execute("PRAGMA user_version=1")
                self.db.commit()
            elif version != 1 or set(objects) != {("table", "episodes"),
                    ("index", "outcome_episode_revision"), ("index", "outcome_latest")}:
                raise OutcomeError("outcome_database_schema")
            columns = {row[1]: row[2].upper() for row in self.db.execute("PRAGMA table_info(episodes)")}
            if columns != _COLUMNS or self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise OutcomeError("outcome_database_schema")
            indexes = {row[1]: (row[2], row[4]) for row in self.db.execute("PRAGMA index_list(episodes)")}
            if indexes != {"outcome_episode_revision": (1, 0), "outcome_latest": (0, 0)}:
                raise OutcomeError("outcome_database_schema")
            for index, expected in (("outcome_episode_revision", ["episode_id", "revision"]),
                                    ("outcome_latest", ["episode_id", "id"])):
                if [row[2] for row in self.db.execute("PRAGMA index_info(" + index + ")")] != expected:
                    raise OutcomeError("outcome_database_schema")
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise
        except Exception as error:
            failure = _database_error(error)
            self.close()
        if failure:
            raise OutcomeError(failure)

    def _guard(self) -> None:
        _check_parent(self.path, self.parent_fd, private=True)
        info = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
            raise OutcomeError("outcome_database_unsafe")
        if (info.st_dev, info.st_ino) != self.identity:
            raise OutcomeError("outcome_database_changed")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None

    def __enter__(self) -> OutcomeStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def append(self, value: dict, *, now: datetime | None = None) -> dict:
        if self.read_only:
            raise OutcomeError("outcome_database_error")
        episode = validate_episode(value, now=now)
        encoded = json.dumps(episode, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_INPUT_BYTES:
            raise OutcomeError("outcome_input_too_large")
        try:
            self._guard()
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute("SELECT revision,CASE WHEN length(CAST(payload_json AS BLOB))<=? "
                "THEN payload_json ELSE NULL END FROM episodes WHERE episode_id=? "
                "ORDER BY revision DESC LIMIT 1", (MAX_INPUT_BYTES, episode["episode_id"])).fetchone()
            if prior and prior[0] == episode["revision"] and prior[1] == encoded:
                self.db.rollback()
                return {"committed": False, "idempotent": True, "revision_order_verified": True}
            if episode["revision"] != (prior[0] + 1 if prior else 1):
                raise OutcomeError("outcome_revision_conflict")
            if prior:
                old = validate_episode(_json(prior[1]), now=now)
                if old["revision"] != prior[0] or any(episode[k] != old[k] for k in
                        ("episode_id", "store_id", "data_origin", "api_profile", "queue_type")):
                    raise OutcomeError("outcome_episode_conflict")
                if _time(episode["recorded_at"]) < _time(old["recorded_at"]):
                    raise OutcomeError("outcome_time_order")
            self.db.execute("INSERT INTO episodes(episode_id,revision,store_id,data_origin,api_profile,payload_json) "
                "VALUES(?,?,?,?,?,?)", (episode["episode_id"], episode["revision"], episode["store_id"],
                episode["data_origin"], episode["api_profile"], encoded))
            self._guard()
            self.db.commit()
            return {"committed": True, "idempotent": False, "revision_order_verified": True}
        except (KeyboardInterrupt, SystemExit):
            self.db.rollback()
            raise
        except Exception as error:
            failure = _database_error(error)
            self.db.rollback()
        raise OutcomeError(failure)

    def report(self, *, limit: int = 1000) -> dict:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise OutcomeError("outcome_invalid_limit")
        self._guard()
        revisions, total = self.db.execute("SELECT COUNT(*),COUNT(DISTINCT episode_id) FROM episodes").fetchone()
        rows = self.db.execute("SELECT episode_id,revision,store_id,data_origin,api_profile,"
            "CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json ELSE NULL END "
            "FROM episodes e WHERE revision=(SELECT MAX(revision) FROM episodes p WHERE p.episode_id=e.episode_id) "
            "ORDER BY id DESC LIMIT ?", (MAX_INPUT_BYTES, limit))
        origins, terminals = Counter(), Counter()
        included = invalid = calls = seated = censored = 0
        for identifier, revision, store, origin, profile, raw in rows:
            included += 1
            try:
                episode = validate_episode(_json(raw))
                if any(episode[k] != v for k, v in (("episode_id", identifier), ("revision", revision),
                        ("store_id", store), ("data_origin", origin), ("api_profile", profile))):
                    raise OutcomeError("outcome_invalid_record")
                target = candidate_targets(episode)
                origins[origin] += 1
                terminal = episode["events"][-1]["event_type"]
                terminals[terminal if terminal in _TERMINALS else "ongoing"] += 1
                calls += int(target["called_wait"] is not None)
                seated += int(target["called_to_seated"] is not None)
                censored += int(target["right_censored_without_call"])
            except OutcomeError:
                invalid += 1
        return {"schema_version": 1, "window": {"total_revisions": revisions, "total_episodes": total,
            "latest_episodes_included": included, "limit": limit, "truncated": total > included},
            "invalid_latest_records": invalid, "origins": dict(sorted(origins.items())),
            "terminal_states": dict(sorted(terminals.items())), "candidate_called_wait_episodes": calls,
            "candidate_call_to_seat_episodes": seated, "right_censored_without_call_episodes": censored,
            "verified_training_labels": 0, "authenticity_verified": False,
            "eta_available": False, "network_performed": False}
