"""Keep public observations without inventing queue events or field semantics.

The accepted response shapes are source-code compatibility clues, not a verified
upstream contract. Only explicitly allowed public fields survive normalization.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .transport import sanitize_transport


API_PROFILES = ("legacy", "miniapp_gateway")
QUEUE_NAMES = (
    "reservationQueue",
    "counterQueue",
    "boothQueue",
    "mixedQueue",
)
_TEXT_FIELDS = (
    "name", "address", "area", "storeStatus", "netTicketStatus",
    "reservationStatus",
)
_IDENTITY_FIELDS = ("id", "storeId", "name", "address", "area")
_SIGNED_INT_FIELDS = ("waitTimeCounter", "waitTimeCap")
_SCALAR_FIELDS = (
    "id", "storeId", *_TEXT_FIELDS, "groupQueuesCount", "raw_wait", *_SIGNED_INT_FIELDS,
)
_KNOWN_KEYS = {
    "data", "id", "storeId", *_TEXT_FIELDS, "groupQueuesCount", "groupQueues",
    "wait", *QUEUE_NAMES, "code", "errorCode", "error_code", "errCode",
    "success", "error", "message", "status", *_SIGNED_INT_FIELDS,
}
_INFERENCE_LIMITS = (
    "Displayed numbers are observable labels, not a complete queue or global cursor.",
    "Added or removed labels do not identify calls, cancellations, no-shows or people.",
    "No queue speed, true no-show rate or waiting-time estimate is inferred.",
    "Unchanged normalized content does not establish upstream freshness or caching.",
)


def _public_id(value: Any) -> bool:
    return ((type(value) is int and value > 0) or
            (isinstance(value, str) and value.isascii() and value.isdecimal()
             and int(value) > 0))


def _validate_api_profile(value: Any) -> str:
    if not isinstance(value, str) or value not in API_PROFILES:
        raise ValueError("invalid_api_profile")
    return value


def _nonnegative_int(value: Any) -> bool:
    # bool is an int subclass, but is not a reported count.
    return type(value) is int and value >= 0


def _signed_int(value: Any) -> bool:
    # Preserve signed source values, including -1, without decoding sentinels.
    return type(value) is int


def _string(value: Any) -> bool:
    return isinstance(value, str)


def _string_array(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _issue(issues: list[dict], path: str, code: str) -> None:
    issues.append({"field": path, "code": code})


def _field(
    source: dict, key: str, validator: Callable[[Any], bool],
    issues: list[dict], *, path: str | None = None,
) -> dict:
    path = path or key
    if key not in source:
        return {"presence": "missing", "value": None}
    if source[key] is None:
        return {"presence": "null", "value": None}
    if not validator(source[key]):
        _issue(issues, path, "invalid_type_or_value")
        return {"presence": "invalid", "value": None}
    value = source[key]
    return {"presence": "present", "value": list(value) if isinstance(value, list) else value}


def _safe_keys(source: dict, issues: list[dict], path: str) -> list[str]:
    """Keep field names; omit keys that could themselves contain secret values."""
    keys = []
    omitted = False
    for key in source:
        safe = isinstance(key, str) and (
            key in _KNOWN_KEYS or (
                len(key) <= 64 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                and not re.fullmatch(r"[A-Fa-f0-9]{16,}", key)
                and not re.fullmatch(r"[A-Za-z0-9_]{32,}", key)
                and not re.search(r"\d{11}", key)
                and not key.startswith(("Bearer", "eyJ", "sk_", "sk_live_"))
            )
        )
        if safe:
            keys.append(key)
        else:
            omitted = True
    if omitted:
        _issue(issues, path, "unsafe_inventory_keys_omitted")
    return sorted(keys)


def _reject_explicit_error(payload: dict) -> None:
    # These are compatibility checks from the reviewed clients. Unknown status
    # values are not treated as business success/failure indicators.
    for key in ("code", "errorCode", "error_code", "errCode"):
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            continue
        if type(value) is int and value in (0, 200):
            continue
        if isinstance(value, str) and value.strip().upper() in ("", "0", "200", "OK", "SUCCESS"):
            continue
        raise ValueError("response contains an explicit business error")
    if "success" in payload and payload["success"] is not True:
        raise ValueError("response contains an explicit business error")
    if payload.get("error"):
        raise ValueError("response contains an explicit business error")


def _detail_data(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("detail response must be an object")
    _reject_explicit_error(payload)
    if "id" not in payload and "storeId" not in payload and "data" in payload:
        if not isinstance(payload["data"], dict):
            raise ValueError("detail data must be an object")
        data = payload["data"]
        _reject_explicit_error(data)
        return data
    # Do not scan arbitrary nested objects to find something resembling a store.
    detail_keys = {"id", "storeId", *_TEXT_FIELDS, "groupQueuesCount", "groupQueues", "wait", *_SIGNED_INT_FIELDS}
    if not any(key in payload for key in detail_keys):
        raise ValueError("detail response has an unrecognized structure")
    return payload


def _parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observation time must be an ISO timestamp with timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("observation time must be an ISO timestamp with timezone") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("observation time must include a timezone")
    return result


def _identity_fields(data: dict, issues: list[dict]) -> dict:
    return {
        key: _field(data, key, _public_id if key in ("id", "storeId") else _string, issues)
        for key in _IDENTITY_FIELDS
    }


def _canonical_id(data: dict) -> str:
    identifiers = [data[key] for key in ("id", "storeId") if key in data]
    if not identifiers or not all(_public_id(value) for value in identifiers):
        raise ValueError("store identity is missing or invalid")
    if len({int(value) for value in identifiers}) != 1:
        raise ValueError("store identity fields disagree")
    return str(int(identifiers[0]))


def normalize_snapshot(
    payload: dict, store_id: str, *, request_started_at: str, received_at: str,
    elapsed_ms: int, data_origin: str, api_profile: str = "legacy",
    transport: dict | None = None,
) -> dict:
    """Normalize a successful store response; never retain an entire response."""
    api_profile = _validate_api_profile(api_profile)
    if not _public_id(store_id):
        raise ValueError("requested store identity is invalid")
    if data_origin not in ("live", "fixture", "synthetic"):
        raise ValueError("data_origin must be live, fixture or synthetic")
    started = _parse_time(request_started_at)
    received = _parse_time(received_at)
    if not _nonnegative_int(elapsed_ms):
        raise ValueError("elapsed_ms must be a nonnegative integer")
    data = _detail_data(payload)
    issues: list[dict] = []
    normalized = _identity_fields(data, issues)
    if any(key in data for key in ("id", "storeId")):
        if _canonical_id(data) != str(int(store_id)):
            raise ValueError("response identity does not match requested store")
    else:
        _issue(issues, "id", "response_identity_missing")
    for key in ("storeStatus", "netTicketStatus", "reservationStatus"):
        normalized[key] = _field(data, key, _string, issues)
    normalized["groupQueuesCount"] = _field(data, "groupQueuesCount", _nonnegative_int, issues)
    normalized["groupQueuesCount"]["unit"] = "unknown"
    normalized["raw_wait"] = _field(data, "wait", _nonnegative_int, issues)
    normalized["raw_wait"]["unit"] = "unknown"
    for key in _SIGNED_INT_FIELDS:
        normalized[key] = _field(data, key, _signed_int, issues)
        normalized[key]["unit"] = "unknown"

    group = _field(data, "groupQueues", lambda value: isinstance(value, dict), issues)
    group_source = data["groupQueues"] if group["presence"] == "present" else {}
    groups = {
        key: _field(group_source, key, _string_array, issues, path=f"groupQueues.{key}")
        for key in QUEUE_NAMES
    }
    # A null/invalid parent gives no observable child arrays; it is not empty.
    if group["presence"] in ("null", "invalid"):
        for field in groups.values():
            field["presence"] = group["presence"]
    normalized["groupQueues"] = {"presence": group["presence"], "groups": groups}

    inventory = {
        "response": _safe_keys(payload, issues, "field_inventory.response"),
        "data": _safe_keys(data, issues, "field_inventory.data"),
        "groupQueues": _safe_keys(group_source, issues, "field_inventory.groupQueues"),
    }
    if received < started:
        _issue(issues, "timing", "received_before_request_started")
    content = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot = {
        "schema_version": 1,
        "store_id": str(int(store_id)),
        "api_profile": api_profile,
        "timing": {
            "request_started_at": started.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "received_at": received.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "elapsed_ms": elapsed_ms,
        },
        "source_updated_at": None,
        "upstream_freshness": "unknown",
        "normalized": normalized,
        "data_origin": data_origin,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_hash_scope": "normalized_public_fields",
        "field_inventory": inventory,
        "issues": issues,
    }
    safe_transport = sanitize_transport(transport)
    if safe_transport is not None:
        snapshot["transport"] = safe_transport
    return snapshot


def normalize_directory(payload: dict, *, api_profile: str = "legacy") -> list[dict]:
    """Accept only a bare directory array or the reviewed data-array envelope."""
    api_profile = _validate_api_profile(api_profile)
    if isinstance(payload, list):
        data = payload
    elif isinstance(payload, dict):
        _reject_explicit_error(payload)
        if "data" not in payload or not isinstance(payload["data"], list):
            raise ValueError("directory data must be an array")
        data = payload["data"]
    else:
        raise ValueError("directory response has an unrecognized structure")
    result = []
    for store in data:
        if not isinstance(store, dict):
            raise ValueError("directory entry must be an object")
        _reject_explicit_error(store)
        store_id = _canonical_id(store)
        if not isinstance(store.get("name"), str) or not store["name"].strip():
            raise ValueError("directory store name is missing or invalid")
        issues: list[dict] = []
        result.append({
            "schema_version": 1,
            "store_id": store_id,
            "api_profile": api_profile,
            "normalized": _identity_fields(store, issues),
            "field_inventory": {"data": _safe_keys(store, issues, "field_inventory.data")},
            "issues": issues,
        })
    return result


def compute_change(previous: dict | None, current: dict) -> dict:
    """Compare displayed labels and reported fields, without event attribution."""
    api_profile = _validate_api_profile(current.get("api_profile", "legacy"))
    comparable = previous is not None and (
        previous.get("schema_version") == current.get("schema_version") == 1
        and previous.get("store_id") == current.get("store_id")
        and previous.get("data_origin") == current.get("data_origin")
        and previous.get("api_profile", "legacy") == api_profile
    )
    issues: list[dict] = []
    elapsed_ms = None
    if comparable:
        try:
            interval = (_parse_time(current["timing"]["received_at"])
                        - _parse_time(previous["timing"]["received_at"]))
            if interval.total_seconds() >= 0:
                elapsed_ms = round(interval.total_seconds() * 1000)
            else:
                _issue(issues, "timing", "observation_order_reversed")
        except (ValueError, KeyError, TypeError):
            _issue(issues, "timing", "observation_interval_unknown")
    fields = dict(current.get("normalized", {}))
    old_fields = dict(previous.get("normalized", {})) if comparable else {}
    # Newly supported fields were unobserved in older stored snapshots. Supply
    # missing metadata for comparison only, without inventing values or changing
    # either persisted snapshot. Missing -> missing is not a source value change.
    for observation in (fields, old_fields):
        for key in _SIGNED_INT_FIELDS:
            observation.setdefault(key, {"presence": "missing", "value": None, "unit": "unknown"})
    changes = {}
    if comparable:
        for key in _SCALAR_FIELDS:
            old = old_fields.get(key, {"presence": "missing", "value": None})
            new = fields.get(key, {"presence": "missing", "value": None})
            if old != new:
                changes[key] = {"previous": old, "current": new}
    groups = fields.get("groupQueues", {}).get("groups", {})
    old_groups = old_fields.get("groupQueues", {}).get("groups", {})
    queue_changes = {}
    for key in QUEUE_NAMES:
        old = old_groups.get(key, {"presence": "missing", "value": None})
        new = groups.get(key, {"presence": "missing", "value": None})
        observed = comparable and old.get("presence") == new.get("presence") == "present"
        observed = bool(observed and _string_array(old.get("value")) and _string_array(new.get("value")))
        added = removed = set_changed = values_changed = None
        if observed:
            old_set, new_set = set(old["value"]), set(new["value"])
            added = list(dict.fromkeys(value for value in new["value"] if value not in old_set))
            removed = list(dict.fromkeys(value for value in old["value"] if value not in new_set))
            set_changed = old_set != new_set
            values_changed = old["value"] != new["value"]
        queue_changes[key] = {
            "comparable": observed,
            "previous_presence": old.get("presence") if previous is not None and comparable else None,
            "current_presence": new.get("presence"),
            "added": added,
            "removed": removed,
            "display_set_changed": set_changed,
            "display_values_changed": values_changed,
        }
    if previous is None:
        comparison, content_status = "initial", "initial"
    elif not comparable:
        comparison, content_status = "incomparable", "unknown"
    else:
        comparison = "comparable"
        content_status = "unchanged" if old_fields == fields else "changed"
    return {
        "schema_version": 1,
        "store_id": current.get("store_id"),
        "api_profile": api_profile,
        "comparison": comparison,
        "observation_interval_ms": elapsed_ms,
        "content_status": content_status,
        "upstream_freshness": "unknown",
        "queues": queue_changes,
        "field_changes": changes,
        "inference_limits": list(_INFERENCE_LIMITS),
        "issues": issues,
    }
