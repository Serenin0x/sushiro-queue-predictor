"""Declared local opening windows, never a claim that a store is actually open.

Rules are embedded in the immutable task configuration. Unknown calendar years
follow natural weekdays; a national makeup Saturday remains a Saturday here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .calendar import date_features
from .remote import _id
from .remotetasks import _json, _now, RemoteTaskError

MAX_RULE_BYTES = 16 * 1024
_KEYS = {'schema_version', 'status', 'timezone', 'source', 'declared_on',
    'business_hours_verified', 'apply_when_store_specific_rule_missing',
    'weekday_intervals', 'known_statutory_holiday_intervals', 'makeup_workday_policy',
    'unknown_holiday_policy', 'interval_boundary', 'collect_outside_window',
    'store_overrides', 'date_overrides'}
_CLOCK = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z', re.ASCII)


def _day(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError
    result = date.fromisoformat(value)
    if not 2020 <= result.year <= 2100:
        raise ValueError
    return result


def _intervals(value):
    if type(value) is not list or len(value) > 4:
        raise ValueError
    previous = None
    for pair in value:
        if (type(pair) is not list or len(pair) != 2
                or any(type(t) is not str or not _CLOCK.fullmatch(t) for t in pair)
                or pair[0] >= pair[1] or previous is not None and pair[0] < previous):
            # Midnight-spanning service must use separate per-date windows.
            raise ValueError
        previous = pair[1]


def validate_hours(value):
    try:
        if (type(value) is not dict or set(value) != _KEYS
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['status'] != 'executable_declared_schedule'
                or value['timezone'] != 'Asia/Shanghai' or value['source'] != 'user_assumed'
                or value['business_hours_verified'] is not False
                or value['apply_when_store_specific_rule_missing'] is not True
                or value['collect_outside_window'] is not False
                or value['interval_boundary'] != 'start_inclusive_end_exclusive'
                or value['makeup_workday_policy'] != 'follow_natural_weekday_unless_specific_override'
                or value['unknown_holiday_policy'] != 'retain_unknown_and_follow_declared_weekday'):
            raise ValueError
        _day(value['declared_on'])
        weekdays = value['weekday_intervals']
        if type(weekdays) is not dict or set(weekdays) != set('1234567'):
            raise ValueError
        for intervals in weekdays.values():
            _intervals(intervals)
        _intervals(value['known_statutory_holiday_intervals'])
        seen = set()
        if type(value['store_overrides']) is not list or len(value['store_overrides']) > 32:
            raise ValueError
        for rule in value['store_overrides']:
            if type(rule) is not dict or set(rule) != {'store_id', 'weekday_intervals', 'known_statutory_holiday_intervals'}:
                raise ValueError
            _id(rule['store_id'])
            if rule['store_id'] in seen:
                raise ValueError
            seen.add(rule['store_id'])
            if type(rule['weekday_intervals']) is not dict or set(rule['weekday_intervals']) != set('1234567'):
                raise ValueError
            for intervals in rule['weekday_intervals'].values():
                _intervals(intervals)
            _intervals(rule['known_statutory_holiday_intervals'])
        seen = set()
        if type(value['date_overrides']) is not list or len(value['date_overrides']) > 64:
            raise ValueError
        for rule in value['date_overrides']:
            if type(rule) is not dict or set(rule) != {'date', 'store_id', 'intervals'}:
                raise ValueError
            _day(rule['date'])
            if rule['store_id'] is not None:
                _id(rule['store_id'])
            key = (rule['date'], rule['store_id'])
            if key in seen:
                raise ValueError
            seen.add(key)
            _intervals(rule['intervals'])
        if len(__import__('json').dumps(value).encode()) > MAX_RULE_BYTES:
            raise ValueError
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RemoteTaskError('business_hours_invalid') from None


def read_hours(path):
    """Read one explicit bounded, non-symlink configuration; no discovery."""
    descriptor = None
    try:
        import stat
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_RULE_BYTES:
            raise ValueError
        body = os.read(descriptor, MAX_RULE_BYTES + 1)
        after = os.fstat(descriptor)
        if len(body) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError
        return validate_hours(_json(body))
    except (OSError, ValueError, RecursionError):
        raise RemoteTaskError('business_hours_unavailable_or_invalid') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class BusinessHours:
    def __init__(self, rules):
        self.rules = validate_hours(rules)
        self.zone = ZoneInfo('Asia/Shanghai')

    def intervals_for(self, store_id, day, *, as_of):
        _id(store_id)
        specific = next((r for r in self.rules['date_overrides']
            if r['date'] == day.isoformat() and r['store_id'] == store_id), None)
        general = next((r for r in self.rules['date_overrides']
            if r['date'] == day.isoformat() and r['store_id'] is None), None)
        stamp = datetime.combine(day, time(12), self.zone)
        features = date_features(_now(stamp), as_of=_now(as_of))
        if specific is not None or general is not None:
            rule = specific if specific is not None else general
            intervals, scope = rule['intervals'], 'store_date_override' if specific else 'date_override'
        else:
            rule = next((r for r in self.rules['store_overrides'] if r['store_id'] == store_id), self.rules)
            intervals = (rule['known_statutory_holiday_intervals'] if features['date_type'] == 'holiday'
                else rule['weekday_intervals'][str(day.isoweekday())])
            scope = 'store_override' if rule is not self.rules else 'default'
        spans = [(datetime.combine(day, time.fromisoformat(a), self.zone).astimezone(timezone.utc),
                  datetime.combine(day, time.fromisoformat(b), self.zone).astimezone(timezone.utc)) for a, b in intervals]
        return spans, {'local_date': day.isoformat(), 'date_type': features['date_type'],
            'calendar_status': features['calendar_status'], 'rule_scope': scope,
            'source': 'user_assumed', 'business_hours_verified': False}

    def decision(self, store_id, now):
        # _now validates aware instants before comparing them.
        _now(now)
        day = now.astimezone(self.zone).date()
        spans, metadata = self.intervals_for(store_id, day, as_of=now)
        for start, end in spans:
            if start <= now < end:
                return {**metadata, 'is_open_window': True, 'window_start_at': _now(start),
                    'window_end_at': _now(end), 'next_open_at': None}
        upcoming = next((start for start, _ in spans if start > now), None)
        if upcoming is None:
            # A configuration can explicitly close every day. Never guess an
            # opening when no declaration is present in the bounded horizon.
            for offset in range(1, 15):
                future, _ = self.intervals_for(store_id, day + timedelta(days=offset), as_of=now)
                if future:
                    upcoming = future[0][0]
                    break
        return {**metadata, 'is_open_window': False, 'window_start_at': None,
            'window_end_at': None, 'next_open_at': _now(upcoming) if upcoming else None}
