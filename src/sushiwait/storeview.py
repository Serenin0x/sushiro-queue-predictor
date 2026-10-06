"""A bounded local display projection, independent of network polling and ETA.

The selected response's age is local. It never certifies an upstream update,
a complete queue, a call event, or an individual's ticket status.
"""

from __future__ import annotations

from collections import Counter

from .packets import PacketError, public_record
from .signals import _text, _time
from .storage import SnapshotStore
from .tasks import linked_task_status


class StoreViewError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.error_code = code


def validate_view_scope(store_id: str, *, data_origin: str, api_profile: str,
                        as_of: str, max_age_seconds: int, sample_limit: int) -> tuple:
    if (type(store_id) is not str or not store_id.isascii() or not store_id.isdecimal()
            or not 1 <= len(store_id) <= 19 or not 0 < int(store_id) <= 2**63 - 1):
        raise StoreViewError("invalid_store_view_id")
    if (type(data_origin) is not str or data_origin not in {"live", "fixture", "synthetic"}
            or type(api_profile) is not str or api_profile not in {"legacy", "miniapp_gateway"}):
        raise StoreViewError("invalid_store_view_scope")
    at = _time(as_of)
    if at is None:
        raise StoreViewError("invalid_store_view_time")
    if (type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600
            or type(sample_limit) is not int or not 1 <= sample_limit <= 10_000):
        raise StoreViewError("invalid_store_view_bounds")
    return str(int(store_id)), at


def store_view(store: SnapshotStore, store_id: str, *, data_origin: str,
               api_profile: str, as_of: str, max_age_seconds: int = 90,
               sample_limit: int = 1000, task_file: str | None = None) -> dict:
    """Read one scoped tail in a consistent transaction without creating records.

    Corrupt or non-increasing observations block a recent-display claim. A
    previous valid response may remain visible, explicitly as last known data.
    Future observations are excluded before examining their display fields.
    """
    store_id, at = validate_view_scope(store_id, data_origin=data_origin,
        api_profile=api_profile, as_of=as_of, max_age_seconds=max_age_seconds,
        sample_limit=sample_limit)
    if store._schema_version != 2:
        raise StoreViewError("store_view_requires_database_schema_2")
    if store.db.in_transaction:
        raise StoreViewError("store_view_requires_idle_connection")
    counts = Counter()
    latest = last_success = None
    last_kind = "no_observations"
    watermark = None
    store.db.execute("BEGIN")
    try:
        # One extra ID determines truncation; no unbounded COUNT or payload scan.
        rows = store.db.execute(
            "SELECT id,CASE WHEN length(run_id)<=36 THEN run_id ELSE NULL END,ok,"
            "CASE WHEN length(received_at)<=32 THEN received_at ELSE NULL END,"
            "CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json ELSE NULL END "
            "FROM samples WHERE store_id=? AND data_origin=? AND api_profile=? "
            "ORDER BY id DESC LIMIT ?",
            (store_id, data_origin, api_profile, sample_limit + 1)).fetchall()
        checkpoint = (linked_task_status(store, task_file, store_id=store_id,
            api_profile=api_profile, data_origin=data_origin, as_of=at,
            max_age_seconds=max_age_seconds) if task_file is not None else None)
        truncated = len(rows) > sample_limit
        for local_id, run, ok, recorded, payload in reversed(rows[:sample_limit]):
            counts["scanned_rows"] += 1
            recorded_at = _time(recorded)
            if recorded_at is not None and recorded_at > at:
                counts["future_rows_excluded"] += 1
                continue
            if recorded_at is None:
                counts["invalid_rows"] += 1
                latest = None
                last_kind = "invalid_record"
                continue
            try:
                record = public_record({"id": local_id, "run_id": run, "ok": ok,
                    "store_id": store_id, "data_origin": data_origin,
                    "api_profile": api_profile, "received_at": recorded,
                    "payload_json": payload}, as_of=_text(at), store_ids=[store_id],
                    api_profile=api_profile, data_origin=data_origin)
            except PacketError:
                counts["invalid_rows"] += 1
                latest = None
                last_kind = "invalid_record"
                continue
            if watermark is not None and recorded_at <= watermark:
                counts["out_of_order_rows"] += 1
                latest = None
                last_kind = "invalid_record"
                continue
            watermark = recorded_at
            phase = "response" if record["ok"] else record["failure_phase"]
            latest = {"recorded_at": _text(recorded_at), "phase": phase,
                      "timing": record["timing"]}
            if record["ok"]:
                counts["valid_responses"] += 1
                last_kind = "response_received"
                # No raw metadata, headers, run IDs, packet IDs, or personal data.
                last_success = {"received_at": _text(recorded_at),
                    "request_started_at": record["timing"]["request_started_at"],
                    "elapsed_ms": record["timing"]["elapsed_ms"],
                    "display": {key: record["display"][key]
                                for key in ("storeStatus", "groupQueuesCount", "groupQueues")}}
            else:
                counts["valid_failures"] += 1
                if phase != "preflight":
                    # A failed socket/TLS attempt need not have an HTTP response.
                    latest["timing"] = {**record["timing"], "semantics": "query_attempt_finished"}
                latest.update(error_code=record["error_code"], http_status=record["http_status"])
                last_kind = "preflight_stopped" if phase == "preflight" else "query_failed"
    finally:
        store.db.rollback()
    age = ((at - _time(last_success["received_at"])).total_seconds()
           if last_success is not None else None)
    if last_kind == "invalid_record":
        availability = "invalid_latest_observation"
    elif last_success is None:
        availability = "unavailable"
    elif age > max_age_seconds:
        availability = "stale"
    elif last_kind != "response_received":
        availability = "last_known_only"
    else:
        availability = "recent_response"
    if (availability == "recent_response" and checkpoint is not None
            and (checkpoint["state"] in {"stopped", "completed"}
                 or checkpoint["checkpoint_age_status"] == "stale")):
        availability = "last_known_only"
    return {"view_schema_version": 1, "store_id": store_id, "api_profile": api_profile,
            "data_origin": data_origin, "as_of": _text(at), "availability": availability,
            "refresh_state": last_kind, "max_age_seconds": max_age_seconds,
            "last_response_age_seconds": round(age, 6) if age is not None else None,
            "last_response": last_success, "latest_observation": latest,
            "display_is_last_known": last_success is not None and availability != "recent_response",
            "scan": {"sample_limit": sample_limit, "truncated": truncated, **counts},
            "source_freshness": "unknown", "source_updated_at": None,
            "complete_queue_available": False, "personal_ticket_status_available": False,
            "collector_state_available": checkpoint is not None,
            "collector_checkpoint": checkpoint, "collector_liveness": "unknown",
            "eta_available": False, "network_performed": False,
            "output_requires_private_handling": True}
