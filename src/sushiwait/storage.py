"""Local storage for normalized public observations, never raw HTTP responses."""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .observations import (
    QUEUE_NAMES, _SCALAR_FIELDS, _SIGNED_INT_FIELDS, _TEXT_FIELDS,
    _nonnegative_int, _public_id, _string_array, _validate_api_profile, compute_change,
)
from .transport import sanitize_transport


_LEGACY_COLUMNS = {
    "id": "INTEGER", "run_id": "TEXT", "store_id": "TEXT", "data_origin": "TEXT",
    "received_at": "TEXT", "ok": "INTEGER", "payload_json": "TEXT",
}
_CURRENT_COLUMNS = {**_LEGACY_COLUMNS, "api_profile": "TEXT"}
_PRESENCES = ("missing", "null", "invalid", "present")
_REPORT_PRESENCES = (*_PRESENCES, "unknown")
_PREFLIGHT_CODES = frozenset({
    "credentials_file_unavailable", "credentials_file_unsafe", "credentials_file_unsupported",
    "credentials_file_too_large", "credentials_file_changed", "credentials_file_invalid_json",
    "credentials_file_invalid", "credentials_profile_mismatch", "credentials_revision_rollback",
    "credentials_revision_conflict", "auth_expiring", "auth_declared_expired", "auth_claims_invalid",
    "client_configuration_error", "credentials_invalid",
    "credentials_refresh_required",
})
_REQUEST_CODES = frozenset({
    "invalid_endpoint", "unsupported_endpoint", "invalid_store_id", "invalid_response",
    "redirect_blocked", "http_error", "response_too_large", "timeout", "tls_verification_failed",
    "tls_error", "network_error", "invalid_json", "business_error", "store_id_mismatch",
    "normalization_failed",
})
_ERROR_CODES = _PREFLIGHT_CODES | _REQUEST_CODES
_MAX_REPORT_PAYLOAD_BYTES = 2 * 1024 * 1024


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _time_text(value: Any) -> str | None:
    result = _timestamp(value)
    return result.isoformat(timespec="milliseconds").replace("+00:00", "Z") if result else None


def _safe_error(value: Any) -> str:
    return value if isinstance(value, str) and value in _ERROR_CODES else "unknown"


def _safe_auth_status(value: Any) -> dict | None:
    """Copy only the existing authorization-time contract, never claims/headers."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("configured", "expired"):
        if key in value and (type(value[key]) is bool or (key == "expired" and value[key] is None)):
            result[key] = value[key]
    for key in ("declared_issued_at", "declared_expires_at"):
        if key in value and value[key] is None:
            result[key] = None
        elif (parsed := _time_text(value.get(key))) is not None:
            result[key] = parsed
    for key in ("declared_lifetime_seconds", "remaining_seconds"):
        number = value.get(key)
        if key in value and number is None:
            result[key] = None
        elif type(number) in (int, float) and (key != "remaining_seconds" or type(number) is int):
            try:
                if number >= 0 and math.isfinite(number):
                    result[key] = number
            except OverflowError:
                pass
    if value.get("token_kind") in ("missing", "unknown", "opaque", "jwt_like"):
        result["token_kind"] = value["token_kind"]
    if value.get("expiry_source") in ("unknown", "invalid_claim", "unverified_claim"):
        result["expiry_source"] = value["expiry_source"]
    if value.get("signature_verified") is False:
        result["signature_verified"] = False
    return result or None


def _summary(values: dict) -> dict:
    count = values["count"]
    return {"count": count, "min": values["min"], "max": values["max"],
            "mean": round(values["sum"] / count, 3) if count else None}


def _empty_summary() -> dict:
    return {"count": 0, "min": None, "max": None, "sum": 0}


def _add_summary(summary: dict, value: int) -> None:
    summary["count"] += 1
    summary["sum"] += value
    summary["min"] = value if summary["min"] is None else min(summary["min"], value)
    summary["max"] = value if summary["max"] is None else max(summary["max"], value)


def _report_field(field: Any, key: str, *, queue: bool = False) -> dict:
    """Validate stored presence/value pairs without returning arbitrary metadata."""
    if field is None:
        result = {"presence": "missing", "value": None}
    elif not isinstance(field, dict) or field.get("presence") not in _PRESENCES:
        result = {"presence": "unknown", "value": None}
    elif field["presence"] != "present":
        result = {"presence": field["presence"], "value": None}
    else:
        value = field.get("value")
        try:
            if queue:
                valid = _string_array(value)
            elif key in ("id", "storeId"):
                valid = _public_id(value)
            elif key in _TEXT_FIELDS:
                valid = isinstance(value, str)
            elif key in _SIGNED_INT_FIELDS:
                valid = type(value) is int
            else:
                valid = _nonnegative_int(value)
        except (ValueError, TypeError, OverflowError):
            valid = False
        result = {"presence": "present" if valid else "invalid", "value": value if valid else None}
    if key in ("groupQueuesCount", "raw_wait", *_SIGNED_INT_FIELDS):
        result["unit"] = "unknown"
    return result


def _report_snapshot(payload: dict, store_id: str, origin: str, profile: str) -> dict | None:
    fields = payload.get("normalized")
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or not isinstance(fields, dict)
            or payload.get("store_id") != store_id or payload.get("data_origin") != origin
            or payload.get("api_profile", "legacy") != profile):
        return None
    normalized = {key: _report_field(fields.get(key), key) for key in _SCALAR_FIELDS}
    parent = fields.get("groupQueues")
    parent_presence = (parent.get("presence") if isinstance(parent, dict) else
                       "missing" if parent is None else "unknown")
    if parent_presence not in _PRESENCES:
        parent_presence = "unknown"
    groups = parent.get("groups", {}) if isinstance(parent, dict) else {}
    invalid_groups = not isinstance(groups, dict)
    if invalid_groups:
        groups = {}
    normalized["groupQueues"] = {"presence": parent_presence, "groups": {
        key: (_report_field(groups.get(key), key, queue=True) if parent_presence == "present" and not invalid_groups
              else {"presence": "unknown", "value": None} if invalid_groups
              else {"presence": parent_presence, "value": None}) for key in QUEUE_NAMES
    }}
    return {"schema_version": 1, "store_id": store_id, "data_origin": origin,
            "api_profile": profile, "normalized": normalized,
            "timing": payload.get("timing") if isinstance(payload.get("timing"), dict) else {}}


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_report_payload")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid_report_payload")


def _report_payload(encoded: str | None) -> dict | None:
    try:
        payload = json.loads(encoded, object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant)
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
        return None


def _http_bucket(value: Any) -> str:
    if value is None:
        return "none"
    return str(value) if type(value) is int and 100 <= value <= 599 else "unknown"


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
        api_profile: str = "legacy", failure_phase: str = "request",
    ) -> int:
        # Only locally defined error codes and timings may reach disk.
        api_profile = _validate_api_profile(api_profile)
        if failure_phase not in ("request", "normalization"):
            raise ValueError("invalid_failure_phase")
        failure = {"store_id": store_id, "data_origin": data_origin, "api_profile": api_profile,
                   "failure_phase": failure_phase,
                   "error_code": _safe_error(result.error_code), "http_status": result.http_status,
                   "timing": {"request_started_at": result.started_at,
                              "received_at": result.received_at, "elapsed_ms": result.elapsed_ms}}
        transport = sanitize_transport(getattr(result, "transport", None))
        if transport is not None:
            failure["transport"] = transport
        return self._insert(store_id, data_origin, api_profile, result.received_at, False, failure)

    def save_preflight_stop(
        self, store_id: str, error_code: str, *, checked_at: str,
        api_profile: str = "legacy", data_origin: str = "live", auth_status: dict | None = None,
    ) -> int:
        """Record a local stop, without inventing an HTTP request or response."""
        api_profile = _validate_api_profile(api_profile)
        if not isinstance(error_code, str) or error_code not in _PREFLIGHT_CODES:
            raise ValueError("invalid_preflight_error_code")
        checked_at = _time_text(checked_at)
        if checked_at is None:
            raise ValueError("invalid_checked_at")
        failure = {"store_id": store_id, "data_origin": data_origin, "api_profile": api_profile,
                   "failure_phase": "preflight", "error_code": error_code, "http_status": None,
                   "timing": {"checked_at": checked_at}}
        status = _safe_auth_status(auth_status)
        if status is not None:
            failure["auth_status"] = status
        # The existing schema2 time column is a recorded-event time for this row.
        return self._insert(store_id, data_origin, api_profile, checked_at, False, failure)

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

    def report(self, *, max_quality_samples: int = 10_000) -> dict:
        """All-time counts and a bounded, streamed window of observable quality."""
        if type(max_quality_samples) is not int or not 1 <= max_quality_samples <= 10_000:
            raise ValueError("invalid_report_sample_limit")
        profile = "'legacy'" if self._schema_version == 1 else "api_profile"
        rows = self.db.execute(
            f"SELECT store_id,data_origin,{profile},COUNT(*),SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END),"
            f"MIN(received_at),MAX(received_at) "
            f"FROM samples GROUP BY store_id,data_origin,{profile} "
            f"ORDER BY store_id,data_origin,{profile}")
        groups = []
        for row in rows:
            store_id, origin, api_profile, count, successful, first, last = row
            try:
                public_id = str(store_id) if _public_id(store_id) else "unknown"
            except (ValueError, OverflowError):
                public_id = "unknown"
            first, last = _time_text(first), _time_text(last)
            groups.append({
                "store_id": public_id,
                "data_origin": origin if origin in ("live", "fixture", "synthetic") else "unknown",
                "api_profile": api_profile if api_profile in ("legacy", "miniapp_gateway") else "unknown",
                "samples": count, "successful_samples": successful, "failed_samples": count-successful,
                "first_received_at": first, "last_received_at": last,
                "first_recorded_at": first, "last_recorded_at": last,
                "record_time_semantics": "response_received_or_preflight_checked",
                "upstream_freshness": "unknown",
                "quality": self._quality_window(store_id, origin, api_profile, count, max_quality_samples),
            })
        return {"schema_version": 2, "database_schema_version": self._schema_version, "groups": groups,
                "limitations": ["采样成功不代表源数据刚更新", "本报告不计算等待时间或真实过号率",
                                "质量统计仅覆盖各来源分组最新的有限记录窗口",
                                "记录时间兼容字段包含本机停止采集的检查时间"]}

    def _quality_window(
        self, store_id: str, origin: str, api_profile: str, total: int, limit: int,
    ) -> dict:
        params = [store_id, origin]
        profile_filter = ""
        if self._schema_version == 2:
            profile_filter = " AND api_profile=?"
            params.append(api_profile)
        params.append(limit)
        # LIMIT is applied to newest IDs before ascending traversal. Oversized JSON
        # is filtered in SQLite so its full content never enters Python memory.
        rows = self.db.execute(
            "SELECT id,run_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? "
            "THEN payload_json ELSE NULL END FROM ("
            "SELECT id,run_id,ok,payload_json FROM samples WHERE store_id=? AND data_origin=?"
            + profile_filter + " ORDER BY id DESC LIMIT ?) ORDER BY id",
            [_MAX_REPORT_PAYLOAD_BYTES, *params],
        )
        phases = {key: 0 for key in ("request", "normalization", "preflight", "unknown")}
        errors = {key: Counter() for key in phases}
        http = {key: Counter() for key in ("request", "normalization")}
        intervals, durations = _empty_summary(), _empty_summary()
        transport_count = 0
        cache_hints = Counter()
        quota_remaining = _empty_summary()
        last_transport = None
        field_presence = {key: {presence: 0 for presence in _REPORT_PRESENCES}
                          for key in (*_SCALAR_FIELDS, "groupQueues")}
        queue_stats = {key: {"presence": {presence: 0 for presence in _REPORT_PRESENCES},
                            "comparable_pairs": 0, "display_set_changes": 0,
                            "display_values_changes": 0} for key in QUEUE_NAMES}
        public = {"valid_successful_payloads": 0, "invalid_successful_payloads": 0,
                  "comparable_pairs": 0, "content_changes": 0, "unchanged_content": 0,
                  "field_changes": {key: 0 for key in _SCALAR_FIELDS}}
        included = invalid = unknown_records = request_records = invalid_timing = reversed_starts = 0
        first_id = last_id = None
        first_request = last_request = None
        previous_snapshot = previous_start = previous_run = previous_run_snapshot = None
        for sample_id, run_id, ok, encoded in rows:
            included += 1
            first_id = sample_id if first_id is None else first_id
            last_id = sample_id
            payload = _report_payload(encoded)
            if payload is None:
                invalid += 1
                if ok == 1:
                    public["invalid_successful_payloads"] += 1
                elif ok == 0:
                    phases["unknown"] += 1
                    errors["unknown"]["unknown"] += 1
                else:
                    unknown_records += 1
                previous_snapshot = previous_start = previous_run = None
                continue
            phase = None
            if ok == 0:
                raw_phase = payload.get("failure_phase", "request")
                phase = raw_phase if isinstance(raw_phase, str) and raw_phase in phases else "unknown"
                phases[phase] += 1
                errors[phase][_safe_error(payload.get("error_code"))] += 1
                if phase in http:
                    http[phase][_http_bucket(payload.get("http_status"))] += 1
            elif ok != 1:
                unknown_records += 1
                previous_snapshot = previous_start = previous_run = None
                continue

            if ok == 1 or phase in ("request", "normalization"):
                request_records += 1
                transport = sanitize_transport(payload.get("transport"))
                if transport is not None:
                    transport_count += 1
                    last_transport = transport
                    cache_hints[transport["gateway_cache"] or "unknown"] += 1
                    if transport["rate_remaining"] is not None:
                        _add_summary(quota_remaining, transport["rate_remaining"])
                timing = payload.get("timing")
                timing = timing if isinstance(timing, dict) else {}
                start, received = _timestamp(timing.get("request_started_at")), _timestamp(timing.get("received_at"))
                elapsed = timing.get("elapsed_ms")
                if (start is not None and received is not None and received >= start
                        and type(elapsed) is int and 0 <= elapsed <= 2**63-1):
                    _add_summary(durations, elapsed)
                    first_request = start if first_request is None else min(first_request, start)
                    last_request = start if last_request is None else max(last_request, start)
                    if previous_start is not None and previous_run == run_id:
                        if start >= previous_start:
                            _add_summary(intervals, round((start-previous_start).total_seconds()*1000))
                        else:
                            reversed_starts += 1
                    previous_start, previous_run = start, run_id
                else:
                    invalid_timing += 1
                    previous_start = previous_run = None
            else:
                previous_start = previous_run = None

            if ok == 1:
                snapshot = _report_snapshot(payload, store_id, origin, api_profile)
                if snapshot is None:
                    public["invalid_successful_payloads"] += 1
                    previous_snapshot = None
                    continue
                public["valid_successful_payloads"] += 1
                for key, counts in field_presence.items():
                    counts[snapshot["normalized"][key]["presence"]] += 1
                for key, stats in queue_stats.items():
                    stats["presence"][snapshot["normalized"]["groupQueues"]["groups"][key]["presence"]] += 1
                if previous_snapshot is not None and previous_run_snapshot == run_id:
                    change = compute_change(previous_snapshot, snapshot)
                    public["comparable_pairs"] += 1
                    public["content_changes" if change["content_status"] == "changed" else "unchanged_content"] += 1
                    for key in change["field_changes"]:
                        public["field_changes"][key] += 1
                    for key, queue in change["queues"].items():
                        if queue["comparable"]:
                            queue_stats[key]["comparable_pairs"] += 1
                            queue_stats[key]["display_set_changes"] += int(queue["display_set_changed"])
                            queue_stats[key]["display_values_changes"] += int(queue["display_values_changed"])
                previous_snapshot, previous_run_snapshot = snapshot, run_id
            else:
                previous_snapshot = None
        return {
            "window": {"selection": "latest_sample_ids", "sample_limit": limit,
                       "included_samples": included, "total_samples": total, "truncated": total > included,
                       "first_sample_id": first_id, "last_sample_id": last_id,
                       "payload_size_limit_bytes": _MAX_REPORT_PAYLOAD_BYTES},
            "invalid_json_or_oversized_payloads": invalid, "unknown_record_types": unknown_records,
            "failures": {"by_phase": phases, "error_codes_by_phase": {key: dict(sorted(value.items()))
                         for key, value in errors.items()},
                         "http_status_by_phase": {key: dict(sorted(value.items())) for key, value in http.items()},
                         "local_stops": phases["preflight"],
                         "actual_request_failures": phases["request"] + phases["normalization"]},
            "requests": {"records": request_records, "invalid_timing_records": invalid_timing,
                         "window_first_request_started_at": _time_text(first_request.isoformat()) if first_request else None,
                         "window_last_request_started_at": _time_text(last_request.isoformat()) if last_request else None,
                         "start_interval_ms": _summary(intervals), "elapsed_ms": _summary(durations),
                         "reversed_start_pairs": reversed_starts,
                         "interval_semantics": "adjacent_valid_request_records_in_same_run"},
            "public_content": public, "field_presence": field_presence, "queues": queue_stats,
            "transport": {"observed_records": transport_count,
                          "gateway_cache_hints": dict(sorted(cache_hints.items())),
                          "rate_remaining": _summary(quota_remaining),
                          "last_observed": last_transport,
                          "source_update_time_verified": False,
                          "rate_window_verified": False},
            "comparison_semantics": "adjacent_valid_successes_in_same_run_failures_or_invalid_payloads_break_chain",
            "upstream_freshness": "unknown",
        }
