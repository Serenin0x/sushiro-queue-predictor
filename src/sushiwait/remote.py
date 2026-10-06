"""Anonymous CRM queue observations, isolated from authenticated Store data.

Two independent GETs do not form an atomic snapshot. Neither endpoint returns
store identity, source-update time, a complete cursor, or verified ETA labels.
Only the five observed public arrays and a bounded integer count are retained.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from . import __version__
from .capture import _open_parent, _check_parent
from .client import _RejectRedirects, _unique_json_object, _reject_json_constant, _has_business_error
from .credentials import _private_file
from .transport import observe_headers, sanitize_transport

SOURCE = "crm_remote_v1_1"
QUEUE_NAMES = ("reservationQueue", "counterQueue", "boothQueue", "mixedQueue", "storeQueue")
ORIGIN = "https://crm-cn-prd.sushiro.com.cn"
BASE_PATH = "/api/1.1/remote/"
ENDPOINTS = ("groupqueues", "storequeuecount")
MAX_BODY = 2 * 1024 * 1024
MAX_RECORD = 32 * 1024
_LABEL = re.compile(r"[A-Za-z]?[0-9]{1,7}(?:-[A-Za-z0-9]{1,7})?\Z", re.ASCII)
_COLUMNS = {"id": "INTEGER", "run_id": "TEXT", "store_id": "TEXT", "ok": "INTEGER", "payload_json": "TEXT"}
_ERRORS = frozenset({"invalid_endpoint", "invalid_store_id", "invalid_response",
    "redirect_blocked", "http_error", "response_too_large", "timeout", "network_error",
    "tls_error", "tls_verification_failed", "invalid_json", "unsupported_queue_schema",
    "unsupported_count_schema", "preceding_query_failed", "business_error"})


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value, re.ASCII)
            or not 0 < int(value) <= 2**63 - 1 or str(int(value)) != value):
        raise ValueError("invalid_store_id")
    return value


def _payload(endpoint, value):
    if isinstance(value, dict) and _has_business_error(value):
        raise ValueError("business_error")
    if endpoint == "storequeuecount":
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError("unsupported_count_schema")
        return {"raw_count": value, "unit": "unknown"}
    if (not isinstance(value, dict) or any(name not in value for name in QUEUE_NAMES)
            or any(not isinstance(value[name], list) or len(value[name]) > 100
                   or any(not isinstance(label, str) or not _LABEL.fullmatch(label)
                          for label in value[name]) for name in QUEUE_NAMES)):
        raise ValueError("unsupported_queue_schema")
    return {"queues": {name: list(value[name]) for name in QUEUE_NAMES}}


@dataclass(frozen=True)
class RemoteResult:
    endpoint: str
    attempted: bool
    ok: bool
    payload: dict | None = field(repr=False)
    error_code: str | None
    http_status: int | None
    started_at: str | None
    received_at: str | None
    elapsed_ms: int | None
    transport: dict | None = field(default=None, repr=False)


class RemoteClient:
    """One attempt, verified TLS, fixed destination; no credential input at all."""
    def __init__(self, *, timeout_seconds=15, max_response_bytes=MAX_BODY, opener=None):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 15 or type(max_response_bytes) is not int
                or not 1 <= max_response_bytes <= MAX_BODY):
            raise ValueError("invalid_transport_bounds")
        self.timeout = timeout_seconds
        self.max_bytes = max_response_bytes
        self.opener = opener

    def fetch(self, endpoint, store_id):
        started, tick = _utc(), time.monotonic_ns()
        status, transport, attempted, response = None, None, False, None

        def result(error=None, payload=None):
            return RemoteResult(endpoint, attempted, error is None, payload if error is None else None,
                error, status, started, _utc(), max(0, (time.monotonic_ns() - tick) // 1_000_000), transport)

        if endpoint not in ENDPOINTS:
            return result("invalid_endpoint")
        try:
            _id(store_id)
        except ValueError:
            return result("invalid_store_id")
        url = ORIGIN + BASE_PATH + endpoint + "?" + urlencode({"storeid": store_id})
        request = Request(url, method="GET", headers={"Accept": "application/json",
            "Accept-Encoding": "identity", "User-Agent": "SUSHIWAIT/" + __version__ + " (anonymous read-only)"})
        try:
            if self.opener is None:
                self.opener = build_opener(ProxyHandler({}), _RejectRedirects(),
                                           HTTPSHandler(context=ssl.create_default_context()))
                self.opener.addheaders = []
            attempted = True
            response = self.opener.open(request, timeout=self.timeout)
            status = response.getcode()
            if type(status) is not int or not 100 <= status <= 599:
                status = None
                return result("invalid_response")
            if 300 <= status < 400 or response.geturl() != url:
                return result("redirect_blocked")
            transport = observe_headers(response.headers)
            if not 200 <= status < 300:
                return result("http_error")
            length = response.headers.get("Content-Length")
            if isinstance(length, str) and length.isascii() and length.isdecimal() and int(length) > self.max_bytes:
                return result("response_too_large")
            raw = response.read(self.max_bytes + 1)
            if not isinstance(raw, bytes):
                return result("invalid_response")
            if len(raw) > self.max_bytes:
                return result("response_too_large")
        except HTTPError as error:
            status = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
            if status is not None and not 300 <= status < 400 and error.geturl() == url:
                transport = observe_headers(error.headers)
            try:
                error.close()
            except Exception:
                pass
            return result("redirect_blocked" if status is not None and 300 <= status < 400 else "http_error")
        except ssl.SSLCertVerificationError:
            return result("tls_verification_failed")
        except ssl.SSLError:
            return result("tls_error")
        except (TimeoutError, socket.timeout):
            return result("timeout")
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                return result("tls_verification_failed")
            if isinstance(error.reason, ssl.SSLError):
                return result("tls_error")
            return result("timeout" if isinstance(error.reason, TimeoutError) else "network_error")
        except (OSError, http.client.HTTPException):
            return result("network_error")
        except Exception:
            return result("network_error")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        try:
            value = json.loads(raw.decode("utf8"), object_pairs_hook=_unique_json_object,
                               parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, RecursionError):
            return result("invalid_json")
        try:
            return result(payload=_payload(endpoint, value))
        except ValueError as error:
            return result(str(error))

    def snapshot(self, store_id):
        _id(store_id)
        queues = self.fetch("groupqueues", store_id)
        count = (self.fetch("storequeuecount", store_id) if queues.ok else
                 RemoteResult("storequeuecount", False, False, None, "preceding_query_failed", None, None, None, None))
        return {"schema_version": 1, "source": SOURCE, "data_origin": "live",
            "requested_store_id": store_id, "response_store_identity_verified": False,
            "ok": queues.ok and count.ok, "queries": {"groupqueues": asdict(queues), "storequeuecount": asdict(count)},
            "atomic_snapshot": False, "source_update_time_verified": False,
            "source_freshness": "unknown", "eta_available": False,
            "complete_queue_cursor_available": False, "verified_training_labels": 0}


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("remote_record_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("remote_record_invalid")
    return parsed.astimezone(timezone.utc)


def validate_record(value):
    """Revalidate records before writing or returning a stored public value."""
    fixed = {"schema_version": 1, "source": SOURCE, "data_origin": "live",
        "response_store_identity_verified": False, "atomic_snapshot": False,
        "source_update_time_verified": False, "source_freshness": "unknown",
        "eta_available": False, "complete_queue_cursor_available": False, "verified_training_labels": 0}
    if not isinstance(value, dict) or any(type(value.get(k)) is not type(v) or value[k] != v for k,v in fixed.items()):
        raise ValueError("remote_record_invalid")
    _id(value.get("requested_store_id"))
    queries = value.get("queries")
    if not isinstance(queries, dict) or set(queries) != set(ENDPOINTS) or type(value.get("ok")) is not bool:
        raise ValueError("remote_record_invalid")
    safe = {}
    for endpoint in ENDPOINTS:
        item = queries[endpoint]
        if (not isinstance(item, dict) or item.get("endpoint") != endpoint
                or type(item.get("ok")) is not bool or type(item.get("attempted")) is not bool):
            raise ValueError("remote_record_invalid")
        error = item.get("error_code")
        if error is not None and error not in _ERRORS or item["ok"] != (error is None):
            raise ValueError("remote_record_invalid")
        status = item.get("http_status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("remote_record_invalid")
        if error == "preceding_query_failed":
            if endpoint != "storequeuecount" or item["attempted"] or any(item.get(k) is not None for k in ("started_at","received_at","elapsed_ms","payload","http_status","transport")):
                raise ValueError("remote_record_invalid")
        else:
            if _time(item.get("received_at")) < _time(item.get("started_at")) or type(item.get("elapsed_ms")) is not int or item["elapsed_ms"] < 0:
                raise ValueError("remote_record_invalid")
        payload = None
        if item["ok"]:
            if not item["attempted"] or status is None or not 200 <= status < 300 or not isinstance(item.get("payload"), dict):
                raise ValueError("remote_record_invalid")
            payload = (_payload(endpoint, item["payload"].get("raw_count")) if endpoint == "storequeuecount"
                       else _payload(endpoint, item["payload"].get("queues")))
        elif item.get("payload") is not None:
            raise ValueError("remote_record_invalid")
        safe[endpoint] = {k: item.get(k) for k in ("endpoint","attempted","ok","error_code","http_status","started_at","received_at","elapsed_ms")}
        safe[endpoint].update(payload=payload, transport=sanitize_transport(item.get("transport")))
    if value["ok"] != all(safe[e]["ok"] for e in ENDPOINTS):
        raise ValueError("remote_record_invalid")
    if safe["groupqueues"]["ok"]:
        if safe["storequeuecount"]["error_code"] == "preceding_query_failed" or _time(safe["storequeuecount"]["started_at"]) < _time(safe["groupqueues"]["received_at"]):
            raise ValueError("remote_record_invalid")
    elif safe["storequeuecount"]["error_code"] != "preceding_query_failed":
        raise ValueError("remote_record_invalid")
    return {**fixed, "requested_store_id": value["requested_store_id"], "ok": value["ok"], "queries": safe}


class RemoteStore:
    """Independent private schema; authenticated snapshot databases are rejected."""
    def __init__(self, path, *, read_only=False):
        self.path = Path(os.path.abspath(path))
        self.db = self.parent_fd = None
        self.read_only = read_only
        self.run_id = str(uuid4())
        try:
            import fcntl
            self.parent_fd, self.name = _open_parent(self.path, private=True)
            fcntl.flock(self.parent_fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            fd = os.open(self.name, (os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW,
                         0o600, dir_fd=self.parent_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("remote_database_unsafe")
                self.identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=0)
            self.db.execute("PRAGMA trusted_schema=OFF")
            self._guard()
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            objects = self.db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and not objects and not read_only:
                self.db.execute("CREATE TABLE remote_samples(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,"
                    "store_id TEXT NOT NULL,ok INTEGER NOT NULL CHECK(ok IN (0,1)),payload_json TEXT NOT NULL)")
                self.db.execute("PRAGMA user_version=1")
                self.db.commit()
                version, objects = 1, [("table", "remote_samples")]
            if (version != 1 or objects != [("table", "remote_samples")]
                    or {r[1]: r[2].upper() for r in self.db.execute("PRAGMA table_info(remote_samples)")} != _COLUMNS
                    or self.db.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
                raise ValueError("remote_database_schema")
        except BaseException:
            self.close()
            raise

    def _guard(self):
        _check_parent(self.path, self.parent_fd, private=True)
        info = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600
                or self.identity != (info.st_dev, info.st_ino)):
            raise ValueError("remote_database_unsafe")

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def append(self, record):
        if self.read_only or self.db.in_transaction:
            raise ValueError("remote_database_read_only")
        self._guard()
        record = validate_record(record)
        from . import remoteintake
        try:
            self.db.execute('BEGIN IMMEDIATE')
            now = remoteintake._clock()
            receipt = remoteintake.make_receipt(record, self.run_id, now)
            previous = self.db.execute('SELECT CASE WHEN length(run_id)<=36 THEN run_id END,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
                'THEN payload_json END FROM remote_samples ORDER BY id DESC LIMIT 1', (MAX_RECORD,)).fetchone()
            if previous is not None:
                stored = json.loads(previous[1], object_pairs_hook=_unique_json_object,
                                    parse_constant=_reject_json_constant)
                prior = remoteintake.read_receipt(stored, validate_record(stored), previous[0])
                if prior is None:
                    # A cooperating older writer can append a legacy row after
                    # upgrade. Find the most recent envelope, without assigning
                    # a receipt to any intervening legacy observation.
                    known = self.db.execute('SELECT CASE WHEN length(run_id)<=36 THEN run_id END,'
                        'CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json END '
                        'FROM remote_samples WHERE instr(payload_json,?)>0 ORDER BY id DESC LIMIT 1',
                        (MAX_RECORD, '"local_intake"')).fetchone()
                    if known is not None:
                        stored = json.loads(known[1], object_pairs_hook=_unique_json_object,
                                            parse_constant=_reject_json_constant)
                        prior = remoteintake.read_receipt(stored, validate_record(stored), known[0])
                        if prior is None:
                            raise ValueError('remote_local_receipt_invalid')
                if prior is not None and _time(receipt['received_at']) < prior:
                    raise ValueError('remote_local_receipt_clock_order')
            # validate_record strips caller metadata; this receipt is assigned
            # afresh for this appended row, never backfilled from a claim.
            text = remoteintake._canonical({**record, 'local_intake': receipt})
            if len(text.encode()) > MAX_RECORD:
                raise ValueError('remote_record_too_large')
            row = self.db.execute("INSERT INTO remote_samples(run_id,store_id,ok,payload_json) VALUES(?,?,?,?)",
                (self.run_id, record["requested_store_id"], int(record["ok"]), text))
            self._guard()
            self.db.commit()
            return row.lastrowid
        except BaseException:
            self.db.rollback()
            raise

    def report(self, store_id, *, limit=1000):
        _id(store_id)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("invalid_report_bounds")
        self._guard()
        total = self.db.execute("SELECT count(*) FROM remote_samples WHERE store_id=?", (store_id,)).fetchone()[0]
        rows = self.db.execute("SELECT id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json END "
            "FROM remote_samples WHERE store_id=? ORDER BY id DESC LIMIT ?", (MAX_RECORD, store_id, limit)).fetchall()
        good, changes, intervals, last_record, previous = 0, {name: 0 for name in QUEUE_NAMES}, [], None, None
        for identifier, ok, text in reversed(rows):
            record = validate_record(json.loads(text, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant))
            if record["requested_store_id"] != store_id or type(identifier) is not int or ok != int(record["ok"]):
                raise ValueError("remote_record_invalid")
            good += int(record["ok"])
            current = record["queries"]["groupqueues"]
            if previous is not None:
                delta = (_time(current["started_at"]) - _time(previous["started_at"])).total_seconds()
                if delta < 0:
                    raise ValueError("remote_record_time_order")
                intervals.append(delta)
                if previous["ok"] and current["ok"]:
                    for name in QUEUE_NAMES:
                        changes[name] += int(previous["payload"]["queues"][name] != current["payload"]["queues"][name])
            previous, last_record = current, record
        return {"schema_version": 1, "source": SOURCE, "requested_store_id": store_id,
            "total_rows": total, "rows_examined": len(rows), "window_truncated": total > len(rows),
            "successful_pairs": good, "failed_pairs": len(rows) - good, "queue_changes": changes,
            "request_start_interval_seconds": {"minimum": min(intervals) if intervals else None,
                "maximum": max(intervals) if intervals else None, "pairs": len(intervals)},
            "latest_record": last_record, "source_freshness": "unknown", "eta_available": False,
            "verified_training_labels": 0, "network_performed": False}


def collect_remote(client, database, store_ids, *, interval=60, samples=1, emit=lambda value: None):
    """Bounded rounds, no retry/backlog burst; first failed pair stops the run."""
    if (not isinstance(store_ids, list) or not 1 <= len(store_ids) <= 3
            or len(set(store_ids)) != len(store_ids) or type(interval) is not int or not 30 <= interval <= 3600
            or type(samples) is not int or not 1 <= samples <= 120):
        raise ValueError("invalid_sampling_bounds")
    for store_id in store_ids:
        _id(store_id)
    counts = {store_id: 0 for store_id in store_ids}
    target = time.monotonic()
    for round_index in range(samples):
        if round_index:
            time.sleep(max(0, target - time.monotonic()))
        round_start = time.monotonic()
        for store_id in store_ids:
            record = client.snapshot(store_id)
            identifier = database.append(record)
            counts[store_id] += int(record["ok"])
            emit({"id": identifier, "round": round_index + 1, "record": record})
            if not record["ok"]:
                return {"ok": False, "stopped_on_failure": True, "successful_pairs": counts,
                    "completed_rounds": round_index, "target_rounds": samples,
                    "maximum_request_budget": len(store_ids) * samples * 2, "retries": 0}
        # Anchor to the actual round start. If a slow round misses its next
        # target, wait a full interval instead of immediately issuing more GETs.
        target = round_start + interval
        if time.monotonic() > target:
            target = time.monotonic() + interval
    return {"ok": True, "stopped_on_failure": False, "successful_pairs": counts,
        "completed_rounds": samples, "target_rounds": samples,
        "maximum_request_budget": len(store_ids) * samples * 2, "retries": 0}


def monitor_remote(schedule, client, database, *, wall_clock, monotonic_clock,
                   sleep, emit=lambda value: None):
    """Apply the existing shared schedule to anonymous pairs, without auth.

    The deadline bounds new pair starts; an in-flight pair may finish later
    (two separately bounded GETs). A schedule slot is a pair, not one HTTP.
    Private plan details never enter output or the observation database.
    """
    requests, succeeded = 0, 0

    def finish(ok):
        result = {"ok": ok, "source": SOURCE, "pairs_started": schedule.count,
            "successful_pairs": succeeded, "requests_attempted": requests,
            "maximum_pair_budget": schedule.maximum,
            "maximum_request_budget": 2 * schedule.maximum,
            "stopped_on_failure": not ok, "retries": 0,
            "scheduler_applied": True, "in_process_only": True,
            "personal_plan_details_in_output": False,
            "output_requires_private_handling": True,
            "source_freshness": "unknown", "eta_available": False,
            "notification_sent": False, "business_operation_performed": False,
            "missed_call_prevention_guaranteed": False,
            "upstream_frequency_verified": False, "verified_training_labels": 0}
        emit({"remote_monitor_summary": result})
        return result

    while True:
        decision = schedule.decision(wall=wall_clock().isoformat(), monotonic=monotonic_clock())
        if decision['done']:
            return finish(True)
        if not decision['due_stores']:
            sleep(max(0, decision['wake_monotonic'] - monotonic_clock()))
            continue
        store_id = decision['due_stores'][0]
        # Recheck at the actual first-GET boundary, including duration/budget.
        now, mono = wall_clock().isoformat(), monotonic_clock()
        current = schedule.decision(wall=now, monotonic=mono)
        if current['done'] or store_id not in current['due_stores']:
            continue
        schedule.mark_actual_start(store_id, wall=now, monotonic=mono)
        record = client.snapshot(store_id)
        identifier = database.append(record)
        requests += sum(query['attempted'] for query in record['queries'].values())
        emit({"id": identifier, "pair": schedule.count, "record": record})
        if not record['ok']:
            return finish(False)
        succeeded += 1
