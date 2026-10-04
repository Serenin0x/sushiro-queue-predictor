"""Allowlisted HTTP observations; never retain arbitrary response headers.

HTTP Date, cache hints and quota counters describe the response, not the
upstream queue update time, a guaranteed quota window or permission to poll.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any


_NUMBERS = ("age_seconds", "cache_max_age_seconds", "rate_limit", "rate_remaining",
            "retry_after_seconds")
_FLAGS = ("no_store", "no_cache", "must_revalidate")


def _integer(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    return None


def _header_number(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value, re.ASCII):
        return _integer(int(value))
    return None


def sanitize_transport(value: Any) -> dict | None:
    """Revalidate stored/caller metadata instead of copying unknown keys."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1):
        return None
    result = {"schema_version": 1, **{key: _integer(value.get(key)) for key in _NUMBERS}}
    result["gateway_cache"] = (value.get("gateway_cache")
        if value.get("gateway_cache") in ("HIT", "MISS", "BYPASS") else None)
    result["cache_flags"] = {key: (value.get("cache_flags", {}).get(key)
        if isinstance(value.get("cache_flags"), dict)
        and type(value["cache_flags"].get(key)) is bool else None) for key in _FLAGS}
    result["http_date"] = None
    date = value.get("http_date")
    if isinstance(date, str) and len(date) <= 40:
        try:
            parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                result["http_date"] = parsed.astimezone(timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            pass
    result["source_update_time_verified"] = False
    result["rate_window_verified"] = False
    return result


def observe_headers(headers: Any) -> dict | None:
    """Read a fixed header whitelist, with bounded input and typed output."""
    def read(name: str) -> str | None:
        try:
            value = headers.get(name)
            # Real HTTPMessage is case insensitive; offline dicts may be lower case.
            if value is None:
                value = headers.get(name.lower())
            return value if isinstance(value, str) and len(value) <= 512 else None
        except Exception:
            return None

    raw = {"schema_version": 1,
        "age_seconds": _header_number(read("Age")),
        "rate_limit": _header_number(read("X-RateLimit-Limit")),
        "rate_remaining": _header_number(read("X-RateLimit-Remaining")),
        "retry_after_seconds": _header_number(read("Retry-After")),
        "gateway_cache": read("X-Gateway-Cache"), "http_date": None,
        "cache_max_age_seconds": None, "cache_flags": {key: None for key in _FLAGS}}
    cache = read("Cache-Control")
    if cache is not None:
        directives = [part.strip().lower() for part in cache.split(",")]
        raw["cache_flags"] = {key: key.replace("_", "-") in directives for key in _FLAGS}
        ages = [part[8:] for part in directives if part.startswith("max-age=")]
        # Ambiguous duplicate directives are unknown, never choose one arbitrarily.
        if len(ages) == 1:
            raw["cache_max_age_seconds"] = _header_number(ages[0])
    date = read("Date")
    if date is not None:
        try:
            parsed = parsedate_to_datetime(date)
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                raw["http_date"] = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    result = sanitize_transport(raw)
    observed = any(result[key] is not None for key in (*_NUMBERS, "http_date", "gateway_cache"))
    observed = observed or cache is not None
    return result if observed else None
