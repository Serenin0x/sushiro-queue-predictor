"""Read-only, as-of window statistics for finite displayed label sets.

Labels stay opaque. Turnover is an observation statistic, never a call speed,
number of people, no-show rate, notification decision or waiting-time estimate.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from .calendar import CalendarError, _instant, date_features
from .observations import QUEUE_NAMES, _validate_api_profile
from .storage import SnapshotStore, _MAX_REPORT_PAYLOAD_BYTES, _report_payload, _report_snapshot


class SignalError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _time(value: object) -> datetime | None:
    try:
        return _instant(value)
    except CalendarError:
        return None


def _text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _stats() -> dict:
    return {"comparable_pairs": 0, "observed_seconds": 0.0,
            "display_set_changed_pairs": 0, "display_values_changed_pairs": 0,
            "added_labels": 0, "removed_labels": 0}


def _add(stats: dict, old: list[str], new: list[str], seconds: float) -> None:
    before, after = set(old), set(new)
    stats["comparable_pairs"] += 1
    stats["observed_seconds"] += seconds
    stats["display_set_changed_pairs"] += int(before != after)
    stats["display_values_changed_pairs"] += int(old != new)
    stats["added_labels"] += len(after - before)
    stats["removed_labels"] += len(before - after)


def _finish(stats: dict, window_seconds: float) -> dict:
    seconds = stats["observed_seconds"]
    return {**stats, "observed_seconds": round(seconds, 6),
            "observed_fraction_of_window": round(seconds / window_seconds, 6),
            "added_labels_per_observed_minute": round(stats["added_labels"] * 60 / seconds, 6) if seconds else None,
            "removed_labels_per_observed_minute": round(stats["removed_labels"] * 60 / seconds, 6) if seconds else None}


def _comparison(previous: dict, recent: dict, availability: str) -> dict:
    result = {"status": "insufficient_pairs", "removed_label_rate_ratio": None,
              "direction": "unknown", "minimum_pairs_per_half": 2}
    if availability != "recent_observations":
        return {**result, "status": "window_not_current"}
    if min(previous["comparable_pairs"], recent["comparable_pairs"]) < 2:
        return result
    # Compare unrounded values; rounding is only an output convention.
    old = previous["removed_labels"] / previous["observed_seconds"]
    new = recent["removed_labels"] / recent["observed_seconds"]
    direction = "higher" if new > old else "lower" if new < old else "equal"
    return {**result, "status": "previous_zero" if old == 0 else "available",
            "direction": direction, "removed_label_rate_ratio": round(new / old, 6) if old else None}


def signal_report(
    store: SnapshotStore, store_id: str, *, data_origin: str, api_profile: str,
    as_of: str, window_seconds: int = 120, max_gap_seconds: int = 90,
    sample_limit: int = 10_000,
) -> dict:
    """Stream one isolated group under a consistent SQLite read transaction."""
    if (not isinstance(store_id, str) or not store_id.isascii() or not store_id.isdecimal()
            or not 1 <= len(store_id) <= 19 or not 0 < int(store_id) <= 2**63 - 1):
        raise SignalError("invalid_signal_store_id")
    store_id = str(int(store_id))
    if data_origin not in ("live", "fixture", "synthetic"):
        raise SignalError("invalid_signal_origin")
    try:
        api_profile = _validate_api_profile(api_profile)
    except ValueError:
        raise SignalError("invalid_signal_profile") from None
    if (type(window_seconds) is not int or not 30 <= window_seconds <= 3600
            or type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 3600
            or type(sample_limit) is not int or not 1 <= sample_limit <= 10_000):
        raise SignalError("invalid_signal_bounds")
    at = _time(as_of)
    if at is None:
        raise SignalError("invalid_signal_time")
    try:
        start = at - timedelta(seconds=window_seconds)
    except OverflowError:
        raise SignalError("invalid_signal_time") from None
    calendar = date_features(_text(at), as_of=_text(at))
    middle = start + timedelta(seconds=window_seconds / 2)
    if store.db.in_transaction:
        raise SignalError("signal_requires_idle_connection")
    parameters = [store_id, data_origin]
    profile_filter = ""
    if store._schema_version == 2:
        profile_filter = " AND api_profile=?"
        parameters.append(api_profile)
    elif api_profile != "legacy":
        profile_filter = " AND 0"
    queues = {key: {"whole": _stats(), "previous": _stats(), "recent": _stats(),
                    "presence": Counter(), "cross_midpoint_pairs": 0} for key in QUEUE_NAMES}
    breaks = Counter()
    counts = Counter()
    latest = None
    last_kind = None
    previous = None
    watermark = watermark_start = None
    count_pairs, count_delta = 0, 0
    store.db.execute("BEGIN")
    try:
        total = store.db.execute(
            "SELECT COUNT(*) FROM samples WHERE store_id=? AND data_origin=?" + profile_filter,
            parameters).fetchone()[0]
        rows = store.db.execute(
            "SELECT id,CASE WHEN length(run_id)<=128 THEN run_id ELSE NULL END,ok,"
            "CASE WHEN length(received_at)<=32 THEN received_at ELSE NULL END,"
            "CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json ELSE NULL END "
            "FROM (SELECT id,run_id,ok,received_at,payload_json FROM samples "
            "WHERE store_id=? AND data_origin=?" + profile_filter +
            " ORDER BY id DESC LIMIT ?) ORDER BY id",
            [_MAX_REPORT_PAYLOAD_BYTES, *parameters, sample_limit])
        for _, run, ok, recorded, encoded in rows:
            counts["scanned_rows"] += 1
            received = _time(recorded)
            if received is None:
                counts["unknown_record_time_rows"] += 1
                previous = None
                last_kind = "unknown"
                continue
            if received > at:
                counts["future_rows_excluded"] += 1
                continue
            if received < start:
                counts["older_rows_excluded"] += 1
                previous = None
                continue
            counts["within_window_rows"] += 1
            payload = _report_payload(encoded)
            snapshot = (_report_snapshot(payload, store_id, data_origin, api_profile)
                        if payload is not None and type(ok) is int and ok == 1 else None)
            timing = snapshot["timing"] if snapshot else {}
            began, payload_received = _time(timing.get("request_started_at")), _time(timing.get("received_at"))
            elapsed = timing.get("elapsed_ms")
            identities = [snapshot["normalized"][key] for key in ("id", "storeId")] if snapshot else []
            identity_valid = (any(field["presence"] == "present" for field in identities)
                              and all(field["presence"] in ("missing", "null") or
                                      field["presence"] == "present" and str(int(field["value"])) == store_id
                                      for field in identities))
            valid = (snapshot is not None and isinstance(run, str) and bool(run)
                     and identity_valid
                     and began is not None and payload_received == received and received >= began
                     and type(elapsed) is int and 0 <= elapsed <= 2**63 - 1)
            if not valid:
                reason = "failed_record" if type(ok) is int and ok == 0 else "invalid_record"
                counts[reason + "s"] += 1
                breaks[reason] += 1
                previous = None
                last_kind = "interrupted"
                continue
            if watermark is not None and (received <= watermark or began < watermark_start):
                counts["out_of_order_successful_rows"] += 1
                breaks["non_increasing_time"] += 1
                previous = None
                last_kind = "interrupted"
                continue
            watermark, watermark_start = received, began
            counts["valid_successful_rows"] += 1
            last_kind = "success"
            latest = max(received, latest) if latest is not None else received
            fields = snapshot["normalized"]
            groups = fields["groupQueues"]["groups"]
            for key in QUEUE_NAMES:
                queues[key]["presence"][groups[key]["presence"]] += 1
            if previous is not None:
                old, old_run, old_received, old_began = previous
                seconds = (received - old_received).total_seconds()
                reason = ("run_changed" if run != old_run else
                          "sampling_gap" if seconds > max_gap_seconds else None)
                if reason:
                    breaks[reason] += 1
                else:
                    old_fields = old["normalized"]
                    for key in QUEUE_NAMES:
                        before = old_fields["groupQueues"]["groups"][key]
                        after = groups[key]
                        if before["presence"] != "present" or after["presence"] != "present":
                            continue
                        _add(queues[key]["whole"], before["value"], after["value"], seconds)
                        if received <= middle:
                            _add(queues[key]["previous"], before["value"], after["value"], seconds)
                        elif old_received >= middle:
                            _add(queues[key]["recent"], before["value"], after["value"], seconds)
                        else:
                            queues[key]["cross_midpoint_pairs"] += 1
                    before, after = old_fields["groupQueuesCount"], fields["groupQueuesCount"]
                    if (before["presence"] == after["presence"] == "present"
                            and max(before["value"], after["value"]) <= 2**63 - 1):
                        count_pairs += 1
                        count_delta += after["value"] - before["value"]
            previous = snapshot, run, received, began
    finally:
        store.db.rollback()
    age = (at - latest).total_seconds() if latest is not None else None
    availability = ("unknown" if last_kind == "unknown" else
                    "interrupted" if last_kind == "interrupted" else
                    "no_observations" if age is None else
                    "stale" if age > max_gap_seconds else "recent_observations")
    output = {}
    for key, stats in queues.items():
        output[key] = {"presence": dict(sorted(stats["presence"].items())),
                       "whole": _finish(stats["whole"], window_seconds),
                       "previous_half": _finish(stats["previous"], window_seconds / 2),
                       "recent_half": _finish(stats["recent"], window_seconds / 2),
                       "cross_midpoint_pairs": stats["cross_midpoint_pairs"],
                       "rate_comparison": _comparison(stats["previous"], stats["recent"], availability)}
    return {"schema_version": 1, "feature_policy": "display-window-v1",
            "store_id": store_id, "data_origin": data_origin, "api_profile": api_profile,
            "as_of": _text(at), "window_started_at": _text(start),
            "window_seconds": window_seconds, "max_gap_seconds": max_gap_seconds,
            "availability": availability, "last_success_age_seconds": age,
            "scan": {"selection": "latest_sample_ids_then_as_of_time_filter",
                     "sample_limit": sample_limit, "total_group_rows": total,
                     "truncated": total > sample_limit, **dict(sorted(counts.items()))},
            "chain_breaks": dict(sorted(breaks.items())), "queues": output,
            "reported_count": {"field": "groupQueuesCount", "unit": "unknown",
                               "comparable_pairs": count_pairs,
                               "sum_of_pair_deltas": count_delta if count_pairs else None},
            "calendar_at_as_of": calendar, "upstream_freshness": "unknown",
            "eta_available": False, "true_no_show_rate": None, "network_performed": False,
            "limitations": ["Finite display-set turnover does not identify queue events or people.",
                            "Rates describe observed pairs, excluding failed, missing and gapped intervals.",
                            "Half-window comparisons are descriptive, not calibrated anomaly alerts.",
                            "A truncated scan may omit usable past rows; diagnostics are not model inputs.",
                            "Response availability is not verified store source-update time."]}
