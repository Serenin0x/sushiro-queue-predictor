"""Immutable private reference archives and explicit date-balanced selection.

Full bounded input is validated before filtering. New receipts never backfill
an earlier decision; no rates are used to select the reference sample.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import os
from uuid import uuid4

from .capture import _open_parent, _check_parent
from .credentials import _private_file, _identity
from .intake import _canonical
from .outcomes import _json, _time, _utc, _uuid
from .packets import _write_packet
from .trendprofiles import (TrendProfileError, validate_profile, _digest,
                            _calendar, _GROUPS, _int, MAX_WINDOWS)

MAX_BYTES = 4*1024*1024
MAX_PROFILES = 512
MAX_ROWS = 24_576
SELECTION_POLICY = 'complete_archive_date_balanced_v1'
SCOPE = ('policy', 'source', 'store_id', 'window_seconds', 'max_gap_seconds',
         'minimum_pairs', 'minimum_coverage_ppm', 'availability_basis')
FIELDS = {'trend_archive_schema_version', 'archive_id', 'revision', 'supersedes_sha256',
          'created_at', 'scope', 'imports', 'archive_sha256'}


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(_canonical({k:v for k,v in value.items()
        if k != 'archive_sha256'}).encode()).hexdigest()


def _scope(profile):
    return {k:profile[k] for k in SCOPE}


def validate_archive(value, *, now=None):
    """Validate all imports, including later receipts and duplicate windows."""
    try:
        clock = _now() if now is None else now
        if (type(value) is not dict or set(value) != FIELDS
                or not _int(value['trend_archive_schema_version'], 1, 1)
                or not _int(value['revision'], 1, 2**31-1)
                or type(value['scope']) is not dict or set(value['scope']) != set(SCOPE)
                or type(value['imports']) is not list or not 1 <= len(value['imports']) <= MAX_PROFILES
                or _time(value['created_at']) > clock
                or _utc(_time(value['created_at'])) != value['created_at']):
            raise ValueError
        _uuid(value['archive_id'])
        prior = value['supersedes_sha256']
        if value['revision'] == 1:
            if prior is not None:raise ValueError
        elif type(prior) is not str or len(prior) != 64 or any(c not in '0123456789abcdef' for c in prior):
            raise ValueError
        seen, rows, last = set(), {}, None
        for item in value['imports']:
            if type(item) is not dict or set(item) != {'profile','received_at'}:
                raise ValueError
            profile = validate_profile(item['profile'], now=clock)
            received = _time(item['received_at'])
            if (profile['trend_profile_schema_version'] != 1 or _scope(profile) != value['scope']
                    or profile['profile_sha256'] in seen or _utc(received) != item['received_at']
                    or not _time(profile['created_at']) <= received <= _time(value['created_at'])
                    or last is not None and received < last):
                raise ValueError
            seen.add(profile['profile_sha256']); last = received
            for row in profile['samples']:
                key = _utc(_time(row[0]))
                if key in rows and rows[key] != row[1:]:
                    raise TrendProfileError('trend_archive_window_conflict')
                rows[key] = row[1:]
                if len(rows) > MAX_ROWS:
                    raise TrendProfileError('trend_archive_row_limit')
        if (value['archive_sha256'] != _hash(value)
                or len(_canonical(value).encode()) > MAX_BYTES):
            raise ValueError
        return deepcopy(value)
    except TrendProfileError:
        raise
    except Exception:
        raise TrendProfileError('trend_archive_invalid') from None


def read_archive(path, *, now=None):
    """Read only one named private immutable file, without directory searches."""
    parent = fd = None
    try:
        parent, name = _open_parent(path, private=True)
        fd = os.open(name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if not _private_file(before) or before.st_size > MAX_BYTES:
            raise ValueError
        chunks, left = [], MAX_BYTES+1
        while left:
            chunk = os.read(fd, min(left, 65536))
            if not chunk:break
            chunks.append(chunk);left -= len(chunk)
        body = b''.join(chunks)
        _check_parent(path, parent, private=True)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not _private_file(named) or _identity(before) != _identity(named)
                or _identity(before) != _identity(os.fstat(fd)) or len(body) != before.st_size):
            raise ValueError
        return validate_archive(_json(body), now=now)
    except Exception:
        raise TrendProfileError('trend_archive_file_invalid') from None
    finally:
        for descriptor in (fd, parent):
            if descriptor is not None:os.close(descriptor)


def append_profiles(profiles, *, archive=None, now=None):
    """Produce a new full snapshot; never replace history or reset receipts."""
    try:
        clock = _now() if now is None else now
        if type(profiles) is not list or not 1 <= len(profiles) <= 16:
            raise ValueError
        safe = [validate_profile(p, now=clock) for p in profiles]
        if any(p['trend_profile_schema_version'] != 1 for p in safe):
            raise ValueError
        value = validate_archive(archive, now=clock) if archive is not None else {
            'trend_archive_schema_version':1, 'archive_id':str(uuid4()), 'revision':1,
            'supersedes_sha256':None, 'created_at':_utc(clock), 'scope':_scope(safe[0]), 'imports':[]}
        if any(_scope(p) != value['scope'] for p in safe):
            raise ValueError
        known = {item['profile']['profile_sha256'] for item in value['imports']}
        added = []
        for p in safe:
            if p['profile_sha256'] not in known:
                added.append({'profile':p,'received_at':_utc(clock)});known.add(p['profile_sha256'])
        if not added:return value
        if archive is not None:
            value['revision'] += 1
            value['supersedes_sha256'] = value['archive_sha256']
        value['created_at'] = _utc(clock)
        value['imports'] += added
        value['archive_sha256'] = _hash(value)
        return validate_archive(value, now=clock)
    except TrendProfileError:
        raise
    except Exception:
        raise TrendProfileError('trend_archive_append_failed') from None


def write_archive(value, destination):
    safe = validate_archive(value)
    result = _write_packet(_canonical(safe).encode(), destination)
    return {**result, 'artifact_written':True, 'archive_revision':safe['revision'],
        'reference_imports':len(safe['imports']), 'network_performed':False,
        'historical_availability_verified':False, 'eta_available':False}


def _spread(values, count):
    if count == 1:return [values[(len(values)-1)//2]]
    return [values[i*(len(values)-1)//(count-1)] for i in range(count)]


def _balanced(rows, maximum, per_day):
    bags = {}
    for row, calendar in rows:bags.setdefault(calendar['date'], []).append(row)
    dates = sorted(bags)
    if len(dates) > maximum:dates = _spread(dates, maximum)
    quotas = {day:0 for day in dates}
    remaining = maximum
    while remaining:
        advanced = False
        for day in dates:
            if quotas[day] < min(len(bags[day]), per_day):
                quotas[day] += 1;remaining -= 1;advanced = True
                if not remaining:break
        if not advanced:break
    selected = [row for day in dates for row in _spread(bags[day], quotas[day]) if quotas[day]]
    return sorted(selected, key=lambda row:_time(row[0]))


def select_profile(archive, *, reference_for, cadence, now=None,
                   maximum_windows=96, maximum_per_day=8, minimum_windows=8, minimum_days=2):
    """Choose by known dates/cadence only; never select using the observed rate."""
    try:
        clock = _now() if now is None else now
        archive = validate_archive(archive, now=clock)
        target = _time(reference_for)
        if (target > clock or not _int(maximum_windows, 2, MAX_WINDOWS)
                or not _int(maximum_per_day, 1, MAX_WINDOWS)
                or not _int(minimum_windows, 2, maximum_windows)
                or not _int(minimum_days, 1, maximum_windows)
                or cadence is not None and (type(cadence) is not list or len(cadence) != 2
                    or not _int(cadence[0], 1, archive['scope']['window_seconds']*1000)
                    or not _int(cadence[1], 1, 10000))):
            raise ValueError
        scope = archive['scope'];start = target-timedelta(seconds=scope['window_seconds'])
        known, imports = {}, 0
        for item in archive['imports']:
            if _time(item['received_at']) > target:continue
            imports += 1
            for row in item['profile']['samples']:
                if _time(row[0]) <= start:known.setdefault(_utc(_time(row[0])), row)
        current = _calendar(reference_for, reference_for)
        eligible = [(row, _calendar(row[0], reference_for)) for row in known.values()
            if cadence is not None and Fraction(4,5) <= Fraction(row[2],row[1])/Fraction(*cadence) <= Fraction(5,4)]
        eligible.sort(key=lambda pair:_time(pair[0][0]))
        selected, matched, matched_rows, matched_days = [], None, 0, 0
        if current['day_type'] != 'unknown':
            for name, keys in _GROUPS:
                matching = [(row, cal) for row, cal in eligible
                    if cal['holiday_name'] == current['holiday_name']
                    and all(current[key] is not None and cal[key] == current[key] for key in keys)]
                if len(matching) < minimum_windows:continue
                candidate = _balanced(matching, maximum_windows, maximum_per_day)
                days = len({_calendar(row[0], reference_for)['date'] for row in candidate})
                if len(candidate) >= minimum_windows and days >= minimum_days:
                    selected, matched = candidate, name
                    matched_rows = len(matching)
                    matched_days = len({cal['date'] for _,cal in matching})
                    break
        value = {'trend_profile_schema_version':2, **scope, 'created_at':_utc(clock),
            'history_cutoff':_utc(target), 'samples':deepcopy(selected), 'selection':{
                'policy':SELECTION_POLICY, 'archive_sha256':archive['archive_sha256'],
                'archive_revision':archive['revision'], 'archive_imports_known':imports,
                'archive_rows_known':len(known), 'reference_for':_utc(target), 'cadence':cadence,
                'matched_group':matched, 'matching_windows':matched_rows, 'matching_days':matched_days,
                'selected_windows':len(selected), 'selected_days':len({_calendar(r[0], reference_for)['date'] for r in selected}),
                'maximum_windows':maximum_windows, 'maximum_per_day':maximum_per_day,
                'minimum_windows':minimum_windows, 'minimum_days':minimum_days}}
        value['profile_sha256'] = _digest(value)
        return validate_profile(value, now=clock)
    except TrendProfileError:
        raise
    except Exception:
        raise TrendProfileError('trend_archive_selection_failed') from None
