"""Single-store daily lifecycle; each date owns a new bounded campaign.

The continuous controller never replenishes an existing day's budget. Its
private root is an explicit writer namespace, not host-wide process discovery.
Completed raw windows and daily projections remain separate immutable records.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from uuid import uuid4
from zoneinfo import ZoneInfo

from .businesshours import BusinessHours, validate_hours
from .capture import _open_parent, _check_parent
from .dailyview import DailyView, MAX_BODY
from .remote import SOURCE, RemoteClient, _id
from .remotecampaign import RemoteCampaign, campaign_config, campaign_status
from .remotetasks import RemoteTask, RemoteTaskError, _at, _now, _json, _integer

ZONE = ZoneInfo('Asia/Shanghai')
MIN_FREE_BYTES = 1024**3


def _private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RemoteTaskError('daily_controller_unsafe_directory')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_daily_archive(path):
    """Read one explicitly selected immutable private projection, no discovery."""
    parent, name = _open_parent(Path(path), private=True)
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or not 1 <= before.st_size <= MAX_BODY):
            raise RemoteTaskError('daily_controller_archive_unsafe')
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            opened = os.fstat(stream.fileno()); raw = stream.read(MAX_BODY+1)
            after = os.fstat(stream.fileno())
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        identity = lambda i: (i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns, i.st_mode, i.st_uid)
        if len(raw) != before.st_size or any(identity(i) != identity(before) for i in (opened, after, named)):
            raise RemoteTaskError('daily_controller_archive_changed')
        _check_parent(Path(path), parent, private=True)
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _immutable(path, value):
    """Publish without overwrite; a conflicting existing result is an error."""
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > MAX_BODY:
        raise RemoteTaskError('daily_controller_archive_too_large')
    if path.exists() or path.is_symlink():
        old = read_daily_archive(path)
        if _json(old) != value:
            raise RemoteTaskError('daily_controller_archive_conflict')
        return old
    parent, name = _open_parent(path, private=True)
    temporary = '.daily-' + uuid4().hex
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        pending = memoryview(raw)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError
            pending = pending[written:]
        os.fsync(descriptor)
        _check_parent(path, parent, private=True)
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
        _check_parent(path, parent, private=True)
        return raw
    except OSError:
        raise RemoteTaskError('daily_controller_archive_durability_unconfirmed') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def daily_config(root, store_id, hours, *, not_before, daily_pair_cap=1500):
    _id(store_id)
    rules = validate_hours(hours)
    absolute = os.path.abspath(root)
    if len(absolute) > 400 or not _integer(daily_pair_cap, 1, 1500):
        raise RemoteTaskError('daily_controller_invalid_config')
    # Bound the controller checkpoint independently of the rules' own limit.
    if len(json.dumps(rules).encode()) > 6000:
        raise RemoteTaskError('daily_controller_rules_too_large')
    return {'root': absolute, 'db': str(Path(absolute)/'unused.sqlite3'), 'store_id': store_id,
        'business_hours': rules, 'not_before': _now(_at(not_before)),
        'base_interval': 60, 'daily_pair_cap': daily_pair_cap, 'transient_recovery_limit': 3}


def _decode(body):
    try:
        value = _json(body)
        if (len(body) > 16384 or type(value) is not dict or set(value) !=
                {'schema_version', 'source', 'config', 'updated_at', 'state', 'error_code',
                 'current', 'last_finished_date', 'calendar_dates_not_observed'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['source'] != SOURCE or value['state'] not in ('active', 'halted')):
            raise ValueError
        c = value['config']
        if c != daily_config(c['root'], c['store_id'], c['business_hours'],
                not_before=c['not_before'], daily_pair_cap=c['daily_pair_cap']):
            raise ValueError
        updated = _at(value['updated_at'])
        if updated < _at(c['not_before']) and value['current'] is not None:
            raise ValueError
        last = value['last_finished_date']
        if last is not None and date.fromisoformat(last).isoformat() != last:
            raise ValueError
        if not _integer(value['calendar_dates_not_observed']):
            raise ValueError
        if value['error_code'] not in (None, 'daily_controller_day_failed', 'daily_controller_storage_or_input_error'):
            raise ValueError
        if (value['state'] == 'halted') != (value['error_code'] is not None):
            raise ValueError
        current = value['current']
        if current is not None:
            if type(current) is not dict or set(current) != {'local_date', 'created_at', 'deadline_at',
                    'duration_seconds', 'maximum_pair_budget', 'business_hours', 'phase'}:
                raise ValueError
            day = date.fromisoformat(current['local_date'])
            created, end = _at(current['created_at']), _at(current['deadline_at'])
            if (day.isoformat() != current['local_date'] or created.astimezone(ZONE).date() != day
                    or end.astimezone(ZONE).date() != day or created < _at(c['not_before'])
                    or created > updated or end <= created
                    or (end-created).total_seconds() != current['duration_seconds']
                    or not _integer(current['duration_seconds'], 30, 86400)
                    or not _integer(current['maximum_pair_budget'], 1, c['daily_pair_cap'])
                    or current['phase'] not in ('collecting', 'archiving')
                    or last is not None and current['local_date'] <= last):
                raise ValueError
            validate_hours(current['business_hours'])
        return value
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise RemoteTaskError('daily_controller_invalid') from None


class DailyController(RemoteTask):
    """Persistent controller lock spans waiting, collecting and archiving."""
    decode = staticmethod(_decode)

    @staticmethod
    def initial_value(config, now):
        return {'schema_version': 1, 'source': SOURCE, 'config': deepcopy(config),
            'updated_at': _now(now), 'state': 'active', 'error_code': None,
            'current': None, 'last_finished_date': None, 'calendar_dates_not_observed': 0}

    def __init__(self, *, config, now, resume_if_present=True):
        root = Path(config['root'])
        _private_dir(root)
        super().__init__(root/'controller.json', config=config, now=now,
            resume=False, resume_if_present=resume_if_present)
        try:
            if not self.loaded:
                # Never adopt a day tree after loss of its controller checkpoint.
                if set(os.listdir(self.parent_fd)) - {self.lock_name}:
                    raise RemoteTaskError('daily_controller_missing_with_existing_state')
                self._commit(deepcopy(self.value))
            self._clock(now)
        except BaseException:
            self.close(); raise

    def _clock(self, now):
        self._guard()
        if _at(_now(now)) < _at(self.value['updated_at']):
            raise RemoteTaskError('daily_controller_clock_rollback')

    def _check_storage(self):
        self._guard()
        usage = os.fstatvfs(self.parent_fd)
        if usage.f_bavail * usage.f_frsize < MIN_FREE_BYTES:
            raise RemoteTaskError('daily_controller_storage_reserve_exhausted')

    def _halt(self, now, code):
        value = deepcopy(self.value)
        value.update(state='halted', error_code=code, updated_at=_now(now))
        self._commit(value)

    def _day_config(self):
        c, day = self.value['config'], self.value['current']
        folder = Path(c['root'])/day['local_date']
        _private_dir(folder)
        _private_dir(folder/'campaign')
        _immutable(folder/'plans.json', {'schema_version': 1, 'plans': []})
        return campaign_config(folder/'campaign', folder/'plans.json', [c['store_id']],
            c['base_interval'], day['duration_seconds'], day['duration_seconds'],
            day['maximum_pair_budget'], now=_at(day['created_at']),
            business_hours=day['business_hours'], transient_recovery_limit=c['transient_recovery_limit'])

    def _prepare(self, now):
        c = self.value['config']; hours = BusinessHours(c['business_hours'])
        day = now.astimezone(ZONE).date()
        spans, _ = hours.intervals_for(c['store_id'], day, as_of=now)
        if (now < _at(c['not_before']) or not any(a <= now < b for a, b in spans)
                or self.value['last_finished_date'] == day.isoformat()):
            return False
        end = spans[-1][1]; duration = math.floor((end-now).total_seconds())
        if duration < 30:
            return False
        budget = sum(math.ceil((b-a).total_seconds()/c['base_interval']) for a, b in spans) + len(spans) + 3
        if budget > c['daily_pair_cap']:
            raise RemoteTaskError('daily_controller_daily_cap_insufficient')
        frozen = deepcopy(c['business_hours'])
        frozen['date_overrides'] = [{'date': day.isoformat(), 'store_id': c['store_id'],
            'intervals': [[a.astimezone(ZONE).strftime('%H:%M'), b.astimezone(ZONE).strftime('%H:%M')] for a, b in spans]}]
        value = deepcopy(self.value)
        last = value['last_finished_date']
        if last is not None:
            value['calendar_dates_not_observed'] += max(0, (day-date.fromisoformat(last)).days-1)
        start = _at(_now(now))
        # Millisecond canonical creation anchors the original day's deadline.
        duration = int((end-start).total_seconds())
        end = start + timedelta(seconds=duration)
        value.update(updated_at=_now(now), current={'local_date': day.isoformat(),
            'created_at': _now(start), 'deadline_at': _now(end), 'duration_seconds': duration,
            'maximum_pair_budget': budget, 'business_hours': frozen, 'phase': 'collecting'})
        self._commit(value)
        return True

    def tick(self, *, wall_clock, monotonic_clock, sleep, emit, should_stop=lambda: False,
            client_factory=RemoteClient, on_view=lambda view, day: None):
        """Collect at most the current/frozen day, or wait with zero queries."""
        now = wall_clock(); self._clock(now)
        if self.value['state'] == 'halted' or should_stop():
            return self.status()
        try:
            if self.value['current'] is None and not self._prepare(now):
                return self.status()
            day = self.value['current']; c = self.value['config']
            view = DailyView([c['store_id']], hours=day['business_hours'], base_interval=c['base_interval'])
            on_view(view, day['local_date'])
            config = self._day_config()
            with RemoteCampaign(config=config, now=wall_clock(), resume_if_present=True) as campaign:
                campaign.restore_history(view.restore)
                if day['phase'] == 'archiving' and campaign.value['state'] == 'active':
                    raise RemoteTaskError('daily_controller_archive_without_terminal_day')
                def saved(event):
                    if 'record' in event:
                        view.committed(event)
                    emit(event)
                if day['phase'] == 'collecting':
                    result = campaign.collect(wall_clock=wall_clock, monotonic_clock=monotonic_clock,
                        sleep=sleep, emit=saved, should_stop=should_stop, client_factory=client_factory,
                        restore=view.restore, before_attempt=self._check_storage)
                else:
                    result = campaign_status(campaign.value)
                if campaign.value['state'] == 'active':
                    return self.status()
                result = campaign_status(campaign.value)
                if campaign.value['state'] == 'failed' or result['uncertain_pair_slots'] or result['pending_attempt']:
                    self._halt(wall_clock(), 'daily_controller_day_failed')
                    return self.status()
                value = deepcopy(self.value)
                value['current']['phase'] = 'archiving'; value['updated_at'] = _now(wall_clock())
                self._commit(value)
                folder = Path(c['root'])/day['local_date']
                # Canonical closing cutoff makes archive retry deterministic.
                projection = view.detail(c['store_id'], day['local_date'], now=_at(campaign.value['updated_at']))
                raw = _immutable(folder/'projection.json', projection)
                _immutable(folder/'result.json', {'daily_controller_schema_version': 1, 'source': SOURCE,
                    'store_id': c['store_id'], 'local_date': day['local_date'], 'task': result,
                    'projection_sha256': hashlib.sha256(raw).hexdigest(),
                    'policy_sha256': _digest({k: v for k, v in day.items() if k != 'phase'}),
                    'official_requests_added_by_archive': 0, 'independent_backup': False,
                    'actual_call_verified': False, 'eta_available': False})
            value = deepcopy(self.value)
            value.update(current=None, last_finished_date=day['local_date'], updated_at=_now(wall_clock()))
            self._commit(value)
            emit({'daily_controller_day_archived': {'store_id': c['store_id'], 'local_date': day['local_date'],
                'successful_pairs': result['successful_pairs'], 'failed_pairs': result['failed_pairs'],
                'official_requests_added_by_archive': 0}})
            return self.status()
        except (OSError, ValueError) as error:
            if not should_stop():
                self._halt(wall_clock(), 'daily_controller_storage_or_input_error')
            raise RemoteTaskError('daily_controller_storage_or_input_error') from None

    def run(self, *, wall_clock, monotonic_clock, sleep, emit, should_stop, client_factory=RemoteClient):
        while not should_stop() and self.value['state'] == 'active':
            self.tick(wall_clock=wall_clock, monotonic_clock=monotonic_clock, sleep=sleep,
                emit=emit, should_stop=should_stop, client_factory=client_factory)
            if self.value['state'] == 'active' and not should_stop():
                sleep(60)
        return self.status()

    def status(self):
        value = self.value; c = value['config']
        return {'daily_controller_schema_version': 1, 'source': SOURCE, 'state': value['state'],
            'error_code': value['error_code'], 'store_id': c['store_id'], 'current_date':
            value['current']['local_date'] if value['current'] else None,
            'phase': value['current']['phase'] if value['current'] else 'waiting_business_day',
            'last_finished_date': value['last_finished_date'], 'daily_pair_cap': c['daily_pair_cap'],
            'calendar_dates_not_observed': value['calendar_dates_not_observed'], 'updated_at': value['updated_at'],
            'off_hours_queries_allowed': False, 'catch_up_requests': 0, 'process_liveness': 'unknown',
            'source_freshness': 'unknown', 'eta_available': False}


def daily_controller_status(root):
    """Offline last-checkpoint report; does not acquire the writer lock."""
    value = _decode(read_daily_archive(Path(root)/'controller.json'))
    # No constructor: reporting must not create files, campaigns or clients.
    report = object.__new__(DailyController)
    report.value = value
    return report.status()
