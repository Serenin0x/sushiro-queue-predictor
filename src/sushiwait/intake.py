"""Private first-receipt ledger for outcome claims; never certifies a label.

The input retains its caller-declared event/record times. A separate locally
assigned first receipt limits retrospective availability. No legacy backfill,
external time attestation, evidence review, network or ticket action is implied.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .capture import _open_parent
from .credentials import _private_file
from .outcomes import (MAX_INPUT_BYTES, OutcomeStore, _database_error, _json,
                       _time, _utc, _ERRORS as _OUTCOME_ERRORS, candidate_targets, validate_episode)

MAX_REVISIONS = 10_000
_COLUMNS = {"id": "INTEGER", "episode_id": "TEXT", "revision": "INTEGER",
    "store_id": "TEXT", "data_origin": "TEXT", "api_profile": "TEXT",
    "received_at": "TEXT", "record_sha256": "TEXT", "payload_json": "TEXT"}
_SELECT = ("id,episode_id,revision,store_id,data_origin,api_profile,received_at,"
           "record_sha256,CASE WHEN length(CAST(payload_json AS BLOB))<=16384 "
           "THEN payload_json ELSE NULL END")
_ERRORS = _OUTCOME_ERRORS | frozenset({
    "outcome_intake_record_invalid", "outcome_intake_database_unsafe",
    "outcome_intake_database_schema", "outcome_intake_requires_idle_writer",
    "outcome_intake_input_too_large", "outcome_intake_episode_conflict",
    "outcome_intake_claim_time_order", "outcome_intake_revision_conflict",
    "outcome_intake_revision_limit_exceeded", "outcome_intake_invalid_scope_or_bounds",
    "outcome_intake_requires_idle_reader", "outcome_intake_receipt_time_order",
    "outcome_intake_operation_failed"})


class IntakeError(ValueError):
    def __init__(self, code, *, commit_status="not_started"):
        self.error_code = code if type(code) is str and code in _ERRORS else "outcome_intake_operation_failed"
        self.commit_status = commit_status if commit_status in ("not_started", "committed", "unknown") else "unknown"
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _receipt_digest(episode, received_at):
    return hashlib.sha256(_canonical({"episode": episode, "received_at": received_at}).encode()).hexdigest()


def _record(row, *, now):
    """Validate stored columns, canonical payload and its separate receipt."""
    try:
        episode = validate_episode(_json(row[8]), now=now)
        text = _canonical(episode)
        received = _time(row[6])
        if (type(row[0]) is not int or row[0] < 1 or text != row[8]
                or not isinstance(row[7], str)
                or _receipt_digest(episode, row[6]) != row[7]
                or any(episode[k] != v for k, v in zip(
                    ("episode_id", "revision", "store_id", "data_origin", "api_profile"), row[1:6]))):
            raise ValueError
        if _utc(received) != row[6] or received < _time(episode["recorded_at"]) or received > now:
            raise ValueError
        return episode, received
    except (ValueError, TypeError, IndexError, OverflowError):
        raise IntakeError("outcome_intake_record_invalid") from None


class OutcomeIntakeStore(OutcomeStore):
    """Independent schema1 database; reuse only private ownership/close guards."""
    def __init__(self, path, *, read_only=False):
        self.path = Path(os.path.abspath(path))
        self.read_only = read_only
        self.parent_fd = self.db = None
        try:
            import fcntl
            self.parent_fd, self.name = _open_parent(self.path, private=True)
            fcntl.flock(self.parent_fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            fd = os.open(self.name, (os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT)
                         | os.O_NOFOLLOW, 0o600, dir_fd=self.parent_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise IntakeError("outcome_intake_database_unsafe")
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
                self.db.execute("CREATE TABLE intake_revisions(id INTEGER PRIMARY KEY,"
                    "episode_id TEXT NOT NULL,revision INTEGER NOT NULL,store_id TEXT NOT NULL,"
                    "data_origin TEXT NOT NULL,api_profile TEXT NOT NULL,received_at TEXT NOT NULL,"
                    "record_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL)")
                self.db.execute("CREATE UNIQUE INDEX intake_episode_revision ON intake_revisions(episode_id,revision)")
                self.db.execute("PRAGMA user_version=1")
                self.db.commit()
                objects = [("table", "intake_revisions"), ("index", "intake_episode_revision")]
                version = 1
            if (version != 1 or set(objects) != {("table", "intake_revisions"), ("index", "intake_episode_revision")}
                    or {r[1]: r[2].upper() for r in self.db.execute("PRAGMA table_info(intake_revisions)")} != _COLUMNS
                    or self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
                    or {r[1]: (r[2], r[4]) for r in self.db.execute("PRAGMA index_list(intake_revisions)")}
                       != {"intake_episode_revision": (1, 0)}
                    or [r[2] for r in self.db.execute("PRAGMA index_info(intake_episode_revision)")]
                       != ["episode_id", "revision"]):
                raise IntakeError("outcome_intake_database_schema")
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise
        except Exception as error:
            self.close()
            raise IntakeError(error.error_code if isinstance(error, IntakeError)
                              else _database_error(error)) from None

    def append(self, value):
        """Assign first receipt inside a transaction; input has no receipt field."""
        if self.read_only or self.db is None or self.db.in_transaction:
            raise IntakeError("outcome_intake_requires_idle_writer")
        attempted = committed = False
        try:
            self._guard()
            episode = validate_episode(value, now=_clock())
            text = _canonical(episode)
            if len(text.encode()) > MAX_INPUT_BYTES:
                raise IntakeError("outcome_intake_input_too_large")
            self.db.execute("BEGIN IMMEDIATE")
            now = _clock()
            validate_episode(episode, now=now)
            prior = self.db.execute(f"SELECT {_SELECT} FROM intake_revisions WHERE episode_id=? "
                                    "ORDER BY revision DESC LIMIT 1", (episode["episode_id"],)).fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM (SELECT id FROM intake_revisions LIMIT 10001)").fetchone()[0]
            latest = self.db.execute(f"SELECT {_SELECT} FROM intake_revisions ORDER BY id DESC LIMIT 1").fetchone()
            if latest is not None:
                _record(latest, now=now)  # Future local receipt / clock rollback stops writes.
            existing = self.db.execute(f"SELECT {_SELECT} FROM intake_revisions WHERE episode_id=? AND revision=?",
                                      (episode["episode_id"], episode["revision"])).fetchone()
            if existing is not None:
                stored, _ = _record(existing, now=now)
                if stored != episode:
                    raise IntakeError("outcome_intake_revision_conflict")
                self.db.rollback()
                return {"committed": False, "commit_status": "not_started", "idempotent": True,
                        "first_receipt_preserved": True, "training_eligible": False}
            if prior is not None:
                previous, receipt = _record(prior, now=now)
                if any(previous[k] != episode[k] for k in
                       ("episode_id", "store_id", "data_origin", "api_profile", "queue_type")):
                    raise IntakeError("outcome_intake_episode_conflict")
                if _time(episode["recorded_at"]) < _time(previous["recorded_at"]):
                    raise IntakeError("outcome_intake_claim_time_order")
            if episode["revision"] != (prior[2] + 1 if prior else 1):
                raise IntakeError("outcome_intake_revision_conflict")
            if count >= MAX_REVISIONS:
                raise IntakeError("outcome_intake_revision_limit_exceeded")
            self.db.execute("INSERT INTO intake_revisions(episode_id,revision,store_id,data_origin,"
                "api_profile,received_at,record_sha256,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (episode["episode_id"], episode["revision"], episode["store_id"], episode["data_origin"],
                 episode["api_profile"], _utc(now), _receipt_digest(episode, _utc(now)), text))
            self._guard()
            attempted = True
            self.db.commit()
            committed = True
            self._guard()
            os.fsync(self.parent_fd)
            return {"committed": True, "commit_status": "committed", "idempotent": False,
                    "first_receipt_preserved": True, "durability_confirmed": True,
                    "training_eligible": False}
        except (KeyboardInterrupt, SystemExit):
            if not committed:
                self.db.rollback()
            raise
        except Exception as error:
            if not committed:
                self.db.rollback()
            code = error.error_code if isinstance(error, IntakeError) else _database_error(error)
            raise IntakeError(code, commit_status="committed" if committed else
                              "unknown" if attempted else "not_started") from None

    def cohort(self, *, as_of, data_origin, api_profile, max_revisions=MAX_REVISIONS):
        """Audit complete bounded chains; select by locally assigned first receipt."""
        begun = False
        try:
            cutoff, now = _time(as_of), _clock()
            if (type(max_revisions) is not int or not 1 <= max_revisions <= MAX_REVISIONS
                    or data_origin not in ("self_reported", "synthetic")
                    or api_profile not in ("legacy", "miniapp_gateway") or cutoff > now):
                raise IntakeError("outcome_intake_invalid_scope_or_bounds")
            if not self.read_only or self.db.in_transaction:
                raise IntakeError("outcome_intake_requires_idle_reader")
            self._guard()
            self.db.execute("BEGIN")
            begun = True
            rows = self.db.execute(f"SELECT {_SELECT} FROM intake_revisions ORDER BY id LIMIT ?",
                                   (max_revisions+1,)).fetchall()
            if len(rows) > max_revisions:
                raise IntakeError("outcome_intake_revision_limit_exceeded")
            chains, selected, global_time, future, scoped = {}, {}, None, 0, 0
            for row in rows:
                episode, received = _record(row, now=now)
                if global_time is not None and received < global_time:
                    raise IntakeError("outcome_intake_receipt_time_order")
                global_time = received
                prior = chains.get(episode["episode_id"])
                if episode["revision"] != (prior["revision"]+1 if prior else 1):
                    raise IntakeError("outcome_intake_revision_conflict")
                if prior and (any(prior[k] != episode[k] for k in
                    ("store_id", "data_origin", "api_profile", "queue_type"))
                    or _time(episode["recorded_at"]) < _time(prior["recorded_at"])):
                    raise IntakeError("outcome_intake_episode_conflict")
                chains[episode["episode_id"]] = episode
                if episode["data_origin"] == data_origin and episode["api_profile"] == api_profile:
                    scoped += 1
                    if received <= cutoff:
                        selected[episode["episode_id"]] = episode
                    else:
                        future += 1
            states = Counter()
            calls = seated = censored = 0
            for episode in selected.values():
                targets = candidate_targets(episode)
                terminal = episode["events"][-1]["event_type"]
                states[terminal if terminal in {"no_show", "cancelled", "seated", "observation_ended"}
                       else "ongoing"] += 1
                calls += int(targets["called_wait"] is not None)
                seated += int(targets["called_to_seated"] is not None)
                censored += int(targets["right_censored_without_call"])
            self._guard()
            return {"intake_schema_version": 1, "revisions_audited": len(rows),
                "scoped_revisions": scoped, "selected_episodes": len(selected),
                "future_receipt_revisions_excluded": future, "terminal_states": dict(sorted(states.items())),
                "unverified_called_wait_candidates": calls, "unverified_call_to_seat_candidates": seated,
                "right_censored_without_call_episodes": censored,
                "availability_basis": "local_first_receipt_time", "receipt_revision_chains_checked": True,
                "independent_time_attestation": False, "historical_availability_verified": False,
                "durable_availability_verified": False,
                "authenticity_verified": False, "verified_training_labels": 0, "training_eligible": False,
                "prediction_features_exported": False, "eta_available": False,
                "output_requires_private_handling": True, "network_performed": False}
        except IntakeError:
            raise
        except Exception as error:
            raise IntakeError(_database_error(error)) from None
        finally:
            if begun and self.db.in_transaction:
                self.db.rollback()
