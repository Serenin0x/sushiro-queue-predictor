"""Offline calendar features; official publication is separate from local review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE_URL = "https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html"
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_NAMES = frozenset(("new_year", "spring_festival", "qingming", "labour_day",
                    "dragon_boat", "mid_autumn", "national_day"))


class CalendarError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _instant(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 32 or not _TIME.fullmatch(value):
        raise CalendarError("invalid_calendar_time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        result = None
    if result is None:
        raise CalendarError("invalid_calendar_time")
    return result


def _day(value: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("invalid_date")
    return date.fromisoformat(value)


def _keys(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid_fields")


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _finite_only(value: str) -> None:
    raise ValueError("invalid_number")


@dataclass(frozen=True)
class _Calendar:
    year: int
    revision: str
    published_on: date
    available_after: datetime
    reviewed_on: date
    sha256: str
    holidays: tuple[tuple[str, date, date], ...]
    makeup_workdays: tuple[tuple[date, str], ...]


def _parse_calendar(raw: bytes) -> _Calendar:
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= 32768:
            raise ValueError("invalid_size")
        data = json.loads(raw, object_pairs_hook=_unique, parse_constant=_finite_only)
        _keys(data, {"schema_version", "year", "region", "timezone", "revision", "source_url",
                     "published_on", "available_after", "reviewed_on", "holidays", "makeup_workdays"})
        if (type(data["schema_version"]) is not int or data["schema_version"] != 1
                or type(data["year"]) is not int or data["year"] != 2026
                or data["region"] != "CN-mainland-national" or data["timezone"] != "Asia/Shanghai"
                or data["source_url"] != SOURCE_URL
                or data["revision"] != "cn-mainland-2026.20261004.1"):
            raise ValueError("invalid_metadata")
        published = _day(data["published_on"])
        reviewed = _day(data["reviewed_on"])
        available = _instant(data["available_after"])
        # Only a publication date is used: availability starts after that local day.
        expected = datetime.combine(published + timedelta(days=1), datetime.min.time(),
                                    timezone(timedelta(hours=8))).astimezone(timezone.utc)
        if available != expected or reviewed < published:
            raise ValueError("invalid_availability")
        if not isinstance(data["holidays"], list) or len(data["holidays"]) != 7:
            raise ValueError("invalid_holidays")
        periods, names, days = [], set(), set()
        for period in data["holidays"]:
            _keys(period, {"name", "start", "end"})
            name, start, end = period["name"], _day(period["start"]), _day(period["end"])
            if name not in _NAMES or name in names or start.year != 2026 or end.year != 2026:
                raise ValueError("invalid_period")
            duration = (end - start).days + 1
            if not 1 <= duration <= 14:
                raise ValueError("invalid_duration")
            period_days = {start + timedelta(days=i) for i in range(duration)}
            if period_days & days:
                raise ValueError("overlapping_holidays")
            days |= period_days
            names.add(name)
            periods.append((name, start, end))
        if names != _NAMES or not isinstance(data["makeup_workdays"], list) or len(data["makeup_workdays"]) != 6:
            raise ValueError("invalid_makeup_days")
        makeup, seen = [], set()
        for item in data["makeup_workdays"]:
            _keys(item, {"date", "for"})
            day = _day(item["date"])
            if (day.year != 2026 or day.isoweekday() < 6 or day in days or day in seen
                    or item["for"] not in names):
                raise ValueError("invalid_makeup_day")
            seen.add(day)
            makeup.append((day, item["for"]))
        result = _Calendar(2026, data["revision"], published, available, reviewed,
            hashlib.sha256(raw).hexdigest(), tuple(periods), tuple(makeup))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        result = None
    if result is None:
        raise CalendarError("calendar_data_invalid")
    return result


@lru_cache(maxsize=1)
def _load_calendar() -> _Calendar:
    try:
        raw = files("sushiwait").joinpath("data", "cn-mainland-2026.json").read_bytes()
    except (OSError, TypeError, ModuleNotFoundError):
        raw = None
    if raw is None:
        raise CalendarError("calendar_data_unavailable")
    return _parse_calendar(raw)


def date_features(at: str, *, as_of: str) -> dict:
    """Reconstruct national date attributes known by as_of, never store opening or ETA."""
    target, known_at = _instant(at), _instant(as_of)
    try:
        local = target.astimezone(ZoneInfo("Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        local = None
    except (ValueError, OverflowError):
        raise CalendarError("invalid_calendar_time") from None
    if local is None:
        raise CalendarError("calendar_timezone_unavailable")
    calendar = _load_calendar()
    day = local.date()
    kind, holiday, makeup_for = "unknown", None, None
    status = "outside_coverage" if day.year != calendar.year else "not_yet_published"
    if day.year == calendar.year and known_at >= calendar.available_after:
        status = "available"
        kind = "ordinary_weekend" if day.isoweekday() >= 6 else "ordinary_workday"
        for name, start, end in calendar.holidays:
            if start <= day <= end:
                kind = "holiday"
                holiday = {"name": name, "day_index": (day - start).days + 1,
                           "length_days": (end - start).days + 1}
                break
        for makeup_day, name in calendar.makeup_workdays:
            if day == makeup_day:
                kind, makeup_for = "makeup_workday", name
                break
    return {"schema_version": 1, "feature_policy": "cn-calendar-v1",
        "at": target.isoformat().replace("+00:00", "Z"),
        "as_of": known_at.isoformat().replace("+00:00", "Z"), "timezone": "Asia/Shanghai",
        "local_date": day.isoformat(), "iso_weekday": day.isoweekday(),
        "is_weekend_by_weekday": day.isoweekday() >= 6,
        "minute_of_day": local.hour * 60 + local.minute, "month": local.month,
        "quarter": (local.month - 1) // 3 + 1,
        "calendar_season": ("winter" if local.month in (12, 1, 2) else
                            "spring" if local.month <= 5 else "summer" if local.month <= 8 else "autumn"),
        "date_type": kind, "holiday": holiday, "makeup_for": makeup_for,
        "calendar_status": status,
        "calendar_provenance": {"revision": calendar.revision, "sha256": calendar.sha256,
            "region": "CN-mainland-national", "coverage_year": calendar.year,
            "source_url": SOURCE_URL, "published_on": calendar.published_on.isoformat(),
            "available_after": calendar.available_after.isoformat().replace("+00:00", "Z"),
            "reviewed_on": calendar.reviewed_on.isoformat(),
            "knowledge_basis": "official_publication_reconstruction"},
        "store_open_status": "unknown", "eta_available": False, "network_performed": False}
