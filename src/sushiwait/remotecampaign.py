"""A finite multi-day campaign of immutable collection windows.

One original deadline and one pair budget cover every window and restart.
Normal window completion can advance; failed or uncertain queries cannot.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .capture import _open_parent, _check_parent
from .credentials import _identity, _private_file, _read_private_file
from .remote import SOURCE, MAX_RECORD, RemoteClient, RemoteStore, validate_record
from .remotetasks import RemoteTask, RemoteTaskError, _at, _now, _json, _integer
from .remotewindow import (
    MAX_DURATION, MAX_PAIRS, RemoteWindowTask, PersistentWindowSchedule,
    _plans, _digest, _decode_window, collect_remote_window, window_config,
)
from .remoteservice import RemoteQueueService
from .planupdates import read_update, check_successor

MAX_CAMPAIGN_DURATION = 14 * 86400
MAX_WINDOWS = 14
_SUMMARY_KEYS = {'cursor', 'successful', 'failed', 'uncertain',
                 'recorded_http_attempts', 'state', 'end_reason', 'pending'}
_ENTRY_KEYS = {'name', 'created_at', 'duration_seconds', 'max_pairs', 'updated_at',
               'summary', 'starts', 'checkpoint_digest', 'database_digest'}
_CONFIG_KEYS = {'root', 'db', 'plan_file', 'plan_digest', 'store_ids',
                'base_interval', 'duration_seconds', 'window_seconds', 'max_pairs',
                'plan_updates_file'}


def _name(index):
    return f'window-{index:02d}'


def campaign_config(root, plan_file, store_ids, base_interval=300,
                    duration_seconds=7 * 86400, window_seconds=86400,
                    max_pairs=6300, *, now, plan_updates_file=None, business_hours=None,
                    transient_recovery_limit=0):
    if not _integer(transient_recovery_limit, 0, 3):
        raise RemoteTaskError('remote_campaign_invalid_recovery_limit')
    root = os.path.abspath(root)
    plan = os.path.abspath(plan_file)
    update = None if plan_updates_file is None else os.path.abspath(plan_updates_file)
    if (len(root) > 512 or len(plan) > 512 or update is not None and len(update) > 512
            or Path(plan).is_relative_to(root)
            or update is not None and (Path(update).is_relative_to(root) or update == plan)
            or not _integer(duration_seconds, 30, MAX_CAMPAIGN_DURATION)
            or not _integer(window_seconds, 30, MAX_DURATION)
            or math.ceil(duration_seconds / window_seconds) > MAX_WINDOWS
            or not _integer(max_pairs, 1, MAX_WINDOWS * MAX_PAIRS)):
        raise RemoteTaskError('remote_campaign_invalid_config')
    first = str(Path(root) / _name(1) / 'remote.sqlite3')
    # Reuse the existing scope/plan validation without creating a window.
    initial = window_config(first, plan, store_ids, base_interval, 30, 1, now=now)
    result = {'root': root, 'db': first, 'plan_file': plan,
            'plan_digest': initial['plan_digest'], 'store_ids': list(store_ids),
            'base_interval': base_interval, 'duration_seconds': duration_seconds,
            'window_seconds': window_seconds, 'max_pairs': max_pairs,
            'plan_updates_file': update}
    if business_hours is not None:
        from .businesshours import validate_hours
        result['business_hours'] = validate_hours(business_hours)
    if transient_recovery_limit:
        result['transient_recovery_limit'] = transient_recovery_limit
    return result


def _hash_ok(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _decode_campaign(body, *, _recovery_limit=0):
    try:
        if len(body) > 16 * 1024:
            raise ValueError
        value = _json(body)
        if type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 3:
            c = value.get('config')
            if type(c) is not dict or not _integer(c.get('transient_recovery_limit'), 1, 3):
                raise ValueError
            base = deepcopy(value)
            limit = base['config'].pop('transient_recovery_limit')
            base['schema_version'] = 2 if 'business_hours' in base['config'] else 1
            _decode_campaign(json.dumps(base,separators=(',',':')).encode(), _recovery_limit=limit)
            return value
        if type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] == 2:
            from .businesshours import validate_hours
            if type(value.get('config')) is not dict or 'business_hours' not in value['config']:
                raise ValueError
            validate_hours(value['config']['business_hours'])
            base = deepcopy(value)
            base['schema_version'] = 1
            base['config'].pop('business_hours')
            for entry in base['windows']:
                s = entry['summary']
                if (type(s) is not dict or not _integer(s.get('scheduled_pauses'),0,s.get('cursor',-1))
                        or not _integer(s.get('successful'),0,s.get('cursor',-1))):
                    raise ValueError
                s['successful'] += s.pop('scheduled_pauses')
            _decode_campaign(json.dumps(base,separators=(',',':')).encode(), _recovery_limit=_recovery_limit)
            return value
        if (type(value) is not dict or set(value) != {'schema_version', 'source', 'config',
                'created_at', 'deadline_at', 'updated_at', 'state', 'end_reason', 'windows', 'plan_context'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['source'] != SOURCE):
            raise ValueError
        c = value['config']
        if (type(c) is not dict or set(c) != _CONFIG_KEYS
                or type(c['store_ids']) is not list or not 1 <= len(c['store_ids']) <= 3
                or len(set(c['store_ids'])) != len(c['store_ids'])
                or not _integer(c['base_interval'], 60, 3600)
                or not _integer(c['duration_seconds'], 30, MAX_CAMPAIGN_DURATION)
                or not _integer(c['window_seconds'], 30, MAX_DURATION)
                or math.ceil(c['duration_seconds'] / c['window_seconds']) > MAX_WINDOWS
                or not _integer(c['max_pairs'], 1, MAX_WINDOWS * MAX_PAIRS)
                or not _hash_ok(c['plan_digest'])):
            raise ValueError
        from .remote import _id
        for store in c['store_ids']:
            _id(store)
        for key in ('root', 'db', 'plan_file'):
            if type(c[key]) is not str or len(c[key]) > 560 or not Path(c[key]).is_absolute():
                raise ValueError
        if c['db'] != str(Path(c['root']) / _name(1) / 'remote.sqlite3'):
            raise ValueError
        update = c['plan_updates_file']
        if (Path(c['plan_file']).is_relative_to(c['root'])
                or update is not None and (type(update) is not str or len(update) > 512
                    or not Path(update).is_absolute() or Path(update).is_relative_to(c['root'])
                    or update == c['plan_file'])):
            raise ValueError
        created, deadline, updated = (_at(value[k]) for k in ('created_at', 'deadline_at', 'updated_at'))
        if deadline != created + timedelta(seconds=c['duration_seconds']) or updated < created:
            raise ValueError
        windows = value['windows']
        if type(windows) is not list or len(windows) > MAX_WINDOWS:
            raise ValueError
        consumed = 0
        failures = 0
        previous = created
        for index, entry in enumerate(windows, 1):
            if type(entry) is not dict or set(entry) != _ENTRY_KEYS or entry['name'] != _name(index):
                raise ValueError
            start, end = _at(entry['created_at']), _at(entry['updated_at'])
            if (not previous <= start <= end <= updated or start >= deadline
                    or not _integer(entry['duration_seconds'], 30, min(MAX_DURATION, c['window_seconds'] + 29))
                    or start + timedelta(seconds=entry['duration_seconds']) > deadline
                    or not _integer(entry['max_pairs'], 1, min(MAX_PAIRS, c['max_pairs'] - consumed))):
                raise ValueError
            s = entry['summary']
            if (type(s) is not dict or set(s) != _SUMMARY_KEYS
                    or not all(_integer(s[k], 0, entry['max_pairs']) for k in ('cursor', 'successful', 'failed', 'uncertain'))
                    or s['cursor'] != s['successful'] + s['failed'] + s['uncertain'] or s['failed'] > 1
                    or not _integer(s['recorded_http_attempts'], 0, 2 * (s['successful'] + s['failed']))
                    or type(s['pending']) is not bool
                    or s['state'] not in ('ready', 'running', 'completed', 'failed')
                    or (s['state'] == 'running') != s['pending']
                    or (s['state'] == 'failed') != bool(s['failed'])
                    or s['end_reason'] not in (None, 'deadline', 'budget', 'monotonic_duration')
                    or (s['state'] == 'completed') != (s['end_reason'] is not None)
                    or s['pending'] and s['cursor'] >= entry['max_pairs']):
                raise ValueError
            failures += s['failed']
            recovered_boundary = bool(_recovery_limit and s['failed'] and not s['uncertain']
                                      and failures <= _recovery_limit)
            starts = entry['starts']
            if (type(starts) is not dict or set(starts) - set(c['store_ids'])
                    or any(not start <= _at(t) <= end for t in starts.values())):
                raise ValueError
            archived = entry['checkpoint_digest'] is not None
            if (archived != (entry['database_digest'] is not None)
                    or archived and (not _hash_ok(entry['checkpoint_digest']) or not _hash_ok(entry['database_digest'])
                        or s['state'] not in ('completed', 'failed') and not (s['state'] == 'ready' and s['uncertain']) or s['pending'])
                    or not archived and index != len(windows)
                    or index < len(windows) and not recovered_boundary
                        and (s['end_reason'] != 'deadline' or s['failed'] or s['uncertain'])):
                raise ValueError
            consumed += s['cursor'] + int(s['pending'])
            previous = end
        if consumed > c['max_pairs']:
            raise ValueError
        reason = value['end_reason']
        if (value['state'] not in ('active', 'completed', 'failed')
                or (value['state'] == 'active') != (reason is None)
                or value['state'] == 'completed' and reason not in ('deadline', 'budget', 'monotonic_duration')
                or value['state'] == 'failed' and reason not in ('query_failed', 'uncertain_attempt', 'window_budget', 'window_monotonic_duration')
                or reason == 'deadline' and updated < deadline
                or reason == 'budget' and consumed != c['max_pairs']):
            raise ValueError
        context = value['plan_context']
        if (update is None) != (context is None):
            # A new campaign has not yet accepted the first feed.
            if not (update is not None and context is None and not windows):
                raise ValueError
        if context is not None:
            from uuid import UUID
            from .planupdates import MAX_REVISION
            if (type(context) is not dict or set(context) != {'series_id', 'revision', 'document_digest', 'declared_at', 'applied_at', 'unobserved_revisions'}
                    or str(UUID(context['series_id'])) != context['series_id'] or UUID(context['series_id']).version != 4
                    or not _integer(context['revision'], 1, MAX_REVISION)
                    or not _hash_ok(context['document_digest'])
                    or not _integer(context['unobserved_revisions'], 0, context['revision'] - 1)
                    or _at(context['applied_at']) > updated
                    or _at(context['declared_at']).replace(microsecond=(_at(context['declared_at']).microsecond // 1000) * 1000) > _at(context['applied_at'])):
                raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
        raise RemoteTaskError('remote_campaign_invalid') from None


def campaign_status(value):
    c = value['config']
    totals = {k: sum(e['summary'][k] for e in value['windows'])
              for k in ('cursor', 'successful', 'failed', 'uncertain', 'recorded_http_attempts')}
    pending = any(e['summary']['pending'] for e in value['windows'])
    result = {'campaign_schema_version': value['schema_version'], 'source': SOURCE, 'mode': 'bounded_multi_day_campaign',
            'state': value['state'], 'end_reason': value['end_reason'], 'store_ids': list(c['store_ids']),
            'base_interval_seconds': c['base_interval'], 'duration_seconds': c['duration_seconds'],
            'window_seconds': c['window_seconds'], 'created_at': value['created_at'],
            'deadline_at': value['deadline_at'], 'updated_at': value['updated_at'],
            'windows_created': len(value['windows']),
            'windows_archived': sum(e['checkpoint_digest'] is not None for e in value['windows']),
            'maximum_pair_budget': c['max_pairs'], 'maximum_request_budget': 2 * c['max_pairs'],
            'completed_pair_slots': totals['cursor'], 'successful_pairs': totals['successful'],
            'failed_pairs': totals['failed'], 'uncertain_pair_slots': totals['uncertain'],
            'recorded_http_attempts': totals['recorded_http_attempts'], 'pending_attempt': pending,
            'unrecorded_http_attempts': 'unknown' if totals['uncertain'] or pending else 0,
            'automatic_normal_window_transition': True, 'automatic_failure_retry': False,
            'catch_up_requests': 0, 'process_liveness': 'unknown',
            'status_semantics': 'last_published_campaign_checkpoint',
            'accepted_plan_revision': value['plan_context']['revision'] if value['plan_context'] else None,
            'source_freshness': 'unknown', 'eta_available': False, 'verified_training_labels': 0}
    limit = c.get('transient_recovery_limit', 0)
    result.update(automatic_failure_retry=bool(limit), maximum_transient_recoveries=limit,
        transient_recoveries_used=sum(e['summary']['failed'] for e in value['windows'][:-1]),
        recovery_does_not_replay_failed_query=True)
    if 'business_hours' in c:
        result.update(business_hours_enabled=True,business_hours_source='user_assumed',business_hours_verified=False,
            scheduled_pause_slots=sum(e['summary']['scheduled_pauses'] for e in value['windows']))
    return result


def remote_campaign_status(root):
    return campaign_status(_decode_campaign(_read_private_file(Path(root) / 'campaign.json')))


def _summary(task):
    result = {key: (task['pending'] is not None if key == 'pending' else task[key]) for key in _SUMMARY_KEYS}
    if 'scheduled_pauses' in task:
        result['scheduled_pauses'] = task['scheduled_pauses']
    return result


def _file_digest(path):
    parent = descriptor = None
    try:
        parent, name = _open_parent(path, private=True)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not _private_file(before) or stat.S_IMODE(before.st_mode) != 0o600:
            raise RemoteTaskError('remote_campaign_archive_unsafe')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        _check_parent(path, parent, private=True)
        if (_identity(before) != _identity(os.fstat(descriptor))
                or _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
            raise RemoteTaskError('remote_campaign_archive_changed')
        return digest.hexdigest()
    finally:
        for fd in (descriptor, parent):
            if fd is not None:
                os.close(fd)


class RemoteCampaign(RemoteTask):
    """Reuse the private atomic checkpoint/lock, with separate window databases."""
    decode = staticmethod(_decode_campaign)

    @staticmethod
    def initial_value(config, now):
        created = _now(now)
        return {'schema_version': 3 if 'transient_recovery_limit' in config else 2 if 'business_hours' in config else 1, 'source': SOURCE, 'config': deepcopy(config),
                'created_at': created,
                'deadline_at': _now(_at(created) + timedelta(seconds=config['duration_seconds'])),
                'updated_at': created, 'state': 'active', 'end_reason': None,
                'windows': [], 'plan_context': None}

    def __init__(self, *, config, now, resume=False, resume_if_present=False):
        super().__init__(Path(config['root']) / 'campaign.json', config=config, now=now,
                         resume=resume, resume_if_present=resume_if_present)
        self.history_checked = False
        try:
            self._namespace_guard()
            if _at(_now(now)) < _at(self.value['updated_at']):
                raise RemoteTaskError('remote_campaign_clock_rollback')
            if not self.loaded:
                self._commit(deepcopy(self.value))
        except BaseException:
            self.close()
            raise

    def _namespace_guard(self):
        self._guard()
        expected = {self.name, self.lock_name} | {e['name'] for e in self.value['windows']}
        actual = set(os.listdir(self.parent_fd))
        if actual - expected:
            raise RemoteTaskError('remote_campaign_orphan_or_foreign_state')
        for entry in self.value['windows']:
            info = os.stat(entry['name'], dir_fd=self.parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RemoteTaskError('remote_campaign_window_directory_unsafe')

    def _config_for(self, entry, now):
        c = self.value['config']
        # An archived window must not depend on today's mutable feed. New and
        # active tasks validate the actual plan/feed themselves before polling.
        config = {'db': str(Path(c['root']) / entry['name'] / 'remote.sqlite3'),
                  'plan_file': c['plan_file'], 'plan_digest': c['plan_digest'],
                  'store_ids': list(c['store_ids']), 'base_interval': c['base_interval'],
                  'duration_seconds': entry['duration_seconds'], 'max_pairs': entry['max_pairs']}
        if c['plan_updates_file'] is not None:
            config['plan_updates_file'] = c['plan_updates_file']
        if 'business_hours' in c:
            config['business_hours'] = deepcopy(c['business_hours'])
        return config

    def _archive(self, entry, *, restore=None):
        directory = Path(self.value['config']['root']) / entry['name']
        task_path, db_path = directory / 'task.json', directory / 'remote.sqlite3'
        raw = _read_private_file(task_path)
        task = _decode_window(raw)
        if (task['config'] != self._config_for(entry, _at(task['updated_at']))
                or task['created_at'] != entry['created_at'] or task['updated_at'] != entry['updated_at']
                or _summary(task) != entry['summary'] or task['starts'] != entry['starts']):
            raise RemoteTaskError('remote_campaign_window_conflict')
        with RemoteStore(db_path, read_only=True) as database:
            if list(database.identity) != task['database_identity']:
                raise RemoteTaskError('remote_campaign_archive_identity_changed')
            hashes = hashlib.sha256(raw).hexdigest(), _file_digest(db_path)
            if entry['checkpoint_digest'] is not None and hashes != (entry['checkpoint_digest'], entry['database_digest']):
                raise RemoteTaskError('remote_campaign_archive_changed')
            if restore is not None:
                restore(database)
                if hashes[1] != _file_digest(db_path) or raw != _read_private_file(task_path):
                    raise RemoteTaskError('remote_campaign_archive_changed')
        return hashes

    def restore_history(self, restore=lambda _: None):
        self._namespace_guard()
        # No live database is opened here. The active child is restored by its
        # owning worker after binding and reconciling its original checkpoint.
        for entry in self.value['windows']:
            if entry['checkpoint_digest'] is not None:
                self._archive(entry, restore=restore)
        if self.value['state'] != 'active' and self.value['windows']:
            entry = self.value['windows'][-1]
            if entry['checkpoint_digest'] is None:
                directory = Path(self.value['config']['root']) / entry['name']
                with RemoteStore(directory / 'remote.sqlite3', read_only=True) as database:
                    restore(database)
        self.history_checked = True

    def _publish_child(self, task, *, new=False):
        value = deepcopy(self.value)
        entry = {'name': _name(len(value['windows']) + 1), 'created_at': task['created_at'],
                 'duration_seconds': task['config']['duration_seconds'], 'max_pairs': task['config']['max_pairs'],
                 'updated_at': task['updated_at'], 'summary': _summary(task), 'starts': deepcopy(task['starts']),
                 'checkpoint_digest': None, 'database_digest': None} if new else value['windows'][-1]
        entry.update(updated_at=task['updated_at'], summary=_summary(task), starts=deepcopy(task['starts']))
        if new:
            value['windows'].append(entry)
        value['updated_at'] = task['updated_at']
        if 'plan_context' in task:
            value['plan_context'] = deepcopy(task['plan_context'])
        self._commit(value)

    def _finish(self, reason, now):
        value = deepcopy(self.value)
        value.update(state='completed' if reason in ('deadline', 'budget', 'monotonic_duration') else 'failed',
                     end_reason=reason, updated_at=_now(now))
        self._commit(value)

    def _recovery_due(self, entry, failures):
        """Only a verified closed failed window can authorize a new window.

        Failed records and their immutable archive stay intact. A new sample
        after backoff uses the original campaign deadline and remaining budget.
        """
        limit = self.value['config'].get('transient_recovery_limit', 0)
        if not limit or not 1 <= failures <= limit or not entry['summary']['failed']:
            return None
        self._archive(entry)
        path = Path(self.value['config']['root']) / entry['name'] / 'remote.sqlite3'
        with RemoteStore(path, read_only=True) as database:
            database._guard()
            row = database.db.execute('SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=? '
                'THEN payload_json END FROM remote_samples ORDER BY id DESC LIMIT 1', (MAX_RECORD,)).fetchone()
            if row is None or row[0] is None:
                raise RemoteTaskError('remote_campaign_missing_failure_record')
            record = validate_record(_json(row[0]))
        if record['ok'] or record['requested_store_id'] not in self.value['config']['store_ids']:
            raise RemoteTaskError('remote_campaign_failure_record_conflict')
        failed = [q for q in record['queries'].values() if q['attempted'] and not q['ok']]
        if len(failed) != 1:
            return None
        q = failed[0]
        transient = q['error_code'] in {'timeout', 'network_error', 'tls_error'} or (
            q['error_code'] == 'http_error' and q['http_status'] in {502, 503, 504})
        return _at(entry['updated_at']) + timedelta(seconds=60 * 2 ** (failures - 1)) if transient else None

    def collect(self, *, wall_clock, monotonic_clock, sleep, emit, should_stop=lambda: False,
                client_factory=RemoteClient, restore=lambda _: None):
        c = self.value['config']
        if not self.history_checked:
            self.restore_history(restore)
        parent_mono = monotonic_clock() + max(0, (_at(self.value['deadline_at']) - wall_clock()).total_seconds())
        carried_mono = {}
        recovery_due = {}
        restart_mono = monotonic_clock() if self.loaded else None
        while self.value['state'] == 'active' and not should_stop():
            self._namespace_guard()
            now = wall_clock()
            if _at(_now(now)) < _at(self.value['updated_at']):
                raise RemoteTaskError('remote_campaign_clock_rollback')
            entries = self.value['windows']
            current = entries[-1] if entries and entries[-1]['checkpoint_digest'] is None else None
            if current is None:
                status = campaign_status(self.value)
                if status['uncertain_pair_slots']:
                    self._finish('uncertain_attempt', now)
                    break
                if status['completed_pair_slots'] >= c['max_pairs']:
                    self._finish('budget', now)
                    break
                if now >= _at(self.value['deadline_at']):
                    self._finish('deadline', now)
                    break
                if monotonic_clock() >= parent_mono:
                    self._finish('monotonic_duration', now)
                    break
                due = None
                if entries and entries[-1]['summary']['failed']:
                    name = entries[-1]['name']
                    if name not in recovery_due:
                        recovery_due[name] = self._recovery_due(entries[-1], status['failed_pairs'])
                    due = recovery_due[name]
                if entries and entries[-1]['summary']['failed'] and due is None:
                    self._finish('query_failed', now)
                    break
                if due is not None and now < due:
                    sleep(min(1, (due - now).total_seconds(), max(0, parent_mono-monotonic_clock())))
                    continue
                if due is not None:
                    self._archive(entries[-1])
                if entries and not entries[-1]['summary']['failed'] and entries[-1]['summary']['end_reason'] != 'deadline':
                    reason = 'window_budget' if entries[-1]['summary']['end_reason'] == 'budget' else 'window_monotonic_duration'
                    self._finish(reason, now)
                    break
                remaining = math.floor((_at(self.value['deadline_at']) - now).total_seconds())
                if remaining < 30:
                    sleep(min(1, max(0, parent_mono - monotonic_clock())))
                    continue
                if len(entries) >= MAX_WINDOWS:
                    raise RemoteTaskError('remote_campaign_window_limit')
                if c['plan_updates_file'] is not None and self.value['plan_context'] is not None:
                    update = read_update(c['plan_updates_file'], stores=c['store_ids'], base_interval=c['base_interval'], now=now)
                    check_successor(self.value['plan_context'], update, old_digest=self.value['plan_context']['document_digest'])
                current = {'name': _name(len(entries) + 1),
                           'duration_seconds': min(c['window_seconds'], remaining),
                           'max_pairs': min(MAX_PAIRS, c['max_pairs'] - status['completed_pair_slots'])}
                # Avoid a final unobservable tail shorter than the child
                # window's minimum. Adjust the adjacent boundary, never the
                # original campaign deadline or the per-window 72h bound.
                tail = remaining - current['duration_seconds']
                if 0 < tail < 30:
                    current['duration_seconds'] = remaining if remaining <= MAX_DURATION else remaining - 30
                os.mkdir(current['name'], 0o700, dir_fd=self.parent_fd)
                os.fsync(self.parent_fd)
                new = True
            else:
                new = False
            directory = Path(c['root']) / current['name']
            config = self._config_for(current, now)
            if config['plan_digest'] != c['plan_digest']:
                raise RemoteTaskError('remote_window_plan_changed')
            with RemoteWindowTask(directory / 'task.json', config=config, resume=not new, now=now) as task:
                task.prepare_database()
                with RemoteStore(config['db'], exclusive_create=new) as database:
                    task.bind(database, now=wall_clock())
                    self._publish_child(task.value, new=new)
                    restore(database)
                    prior = {}
                    for older in self.value['windows'][:-1]:
                        prior.update(older['starts'])
                    schedule = PersistentWindowSchedule(task, wall=wall_clock(), monotonic=monotonic_clock(),
                        previous_starts=prior, previous_monotonic=carried_mono)
                    # The restart barrier belongs to the entire campaign. A
                    # window expiring during it must neither erase nor restart it.
                    if restart_mono is not None:
                        schedule.resume_mono = restart_mono
                    schedule.deadline_mono = min(schedule.deadline_mono, parent_mono)
                    def child_event(event):
                        self._publish_child(task.value)
                        if 'record' in event:
                            emit({**event, 'window_index': len(self.value['windows'])})
                    if not task.value['uncertain']:
                        client = None if task.value['state'] in ('completed', 'failed') else client_factory()
                        collect_remote_window(task, client, wall_clock=wall_clock, monotonic_clock=monotonic_clock,
                            sleep=sleep, emit=child_event, should_stop=should_stop, schedule=schedule)
                    last = deepcopy(task.value)
                    carried_mono = deepcopy(schedule.starts_mono)
            if last['state'] not in ('completed', 'failed') and not last['uncertain']:
                break
            hashes = self._archive(self.value['windows'][-1])
            value = deepcopy(self.value)
            value['windows'][-1].update(checkpoint_digest=hashes[0], database_digest=hashes[1])
            self._commit(value)
            if last['uncertain']:
                self._finish('uncertain_attempt', wall_clock())
            elif last['failed']:
                failed_entry = self.value['windows'][-1]
                due = self._recovery_due(failed_entry, campaign_status(self.value)['failed_pairs'])
                if due is None:
                    self._finish('query_failed', wall_clock())
                else:
                    recovery_due[failed_entry['name']] = due
                    emit({'remote_campaign_recovery': {'not_before_at': _now(due),
                        'failed_window_preserved': True, 'failed_query_replayed': False}})
            elif last['end_reason'] == 'budget':
                reason = 'budget' if campaign_status(self.value)['completed_pair_slots'] == c['max_pairs'] else 'window_budget'
                self._finish(reason, wall_clock())
            elif last['end_reason'] == 'monotonic_duration':
                reason = 'monotonic_duration' if monotonic_clock() >= parent_mono else 'window_monotonic_duration'
                self._finish(reason, wall_clock())
        result = campaign_status(self.value)
        result.update(ok=self.value['state'] == 'completed' and result['failed_pairs'] == 0 and result['uncertain_pair_slots'] == 0,
                      stopped_by_request=should_stop())
        emit({'remote_campaign_summary': result})
        return result


class RemoteCampaignService(RemoteQueueService):
    def __init__(self, *, root, plan_file, store_ids, base_interval=300,
                 duration_seconds=7 * 86400, window_seconds=86400, max_pairs=6300,
                 plan_updates_file=None, resume=False, resume_if_present=False, business_hours=None,
                 transient_recovery_limit=0, **kwargs):
        super().__init__(db=str(Path(root) / _name(1) / 'remote.sqlite3'), task_file=str(Path(root) / 'campaign.json'),
                         store_ids=store_ids, interval=base_interval, samples=1, resume=resume, **kwargs)
        self.resume_if_present = resume_if_present
        self.config = campaign_config(root, plan_file, store_ids, base_interval, duration_seconds,
                                      window_seconds, max_pairs, now=self.wall_clock(), plan_updates_file=plan_updates_file,
                                      business_hours=business_hours, transient_recovery_limit=transient_recovery_limit)
        if business_hours is not None:
            from .dailyview import DailyView
            self.daily_view=DailyView(store_ids,hours=business_hours,base_interval=base_interval)

    def _run(self):
        try:
            with RemoteCampaign(config=self.config, now=self.wall_clock(), resume=self.resume,
                                resume_if_present=self.resume_if_present) as campaign:
                campaign.restore_history(self._restore)
                self._set('ready', task=campaign_status(campaign.value))
                self.ready.set()
                self.activated.wait()
                if self.stop_event.is_set():
                    self._set('stopped', task=campaign_status(campaign.value))
                    return
                self._set('running', task=campaign_status(campaign.value))
                def emit(event):
                    if 'record' in event:
                        if self.daily_view is not None:self.daily_view.committed(event)
                        self.view.publish(event['record'])
                    self._set('running', task=campaign_status(campaign.value))
                def sleep(seconds):
                    if self.wait is None:
                        self.stop_event.wait(seconds)
                    else:
                        self.wait(seconds, self.stop_event)
                result = campaign.collect(wall_clock=self.wall_clock, monotonic_clock=self.monotonic_clock,
                    sleep=sleep, emit=emit, should_stop=self.stop_event.is_set,
                    client_factory=self.client_factory, restore=self._restore)
                state = campaign.value['state'] if campaign.value['state'] != 'active' else 'stopped'
                self._set(state, error='remote_service_query_failed' if state == 'failed' else None, task=result)
        except BaseException as error:
            code = str(error) if isinstance(error, RemoteTaskError) else 'remote_service_storage_or_input_error'
            self._set('failed', error=code)
        finally:
            self.ready.set()

    def status(self):
        result = super().status()
        result.update(mode='bounded_multi_day_campaign', automatic_normal_window_transition=True,
                      automatic_failure_retry=bool(self.config.get('transient_recovery_limit', 0)))
        if 'business_hours' in self.config:
            from .businesshours import BusinessHours
            hours = BusinessHours(self.config['business_hours'])
            result.update(business_hours_enabled=True,business_hours_source='user_assumed',business_hours_verified=False,
                business_windows={store:hours.decision(store,self.wall_clock()) for store in self.view.stores},
                off_hours_queries_allowed=False)
            result['collection_phase']=('collecting_business_window' if any(w['is_open_window'] for w in result['business_windows'].values())
                else 'waiting_business_hours') if result['service_state']=='running' and result['worker_alive'] else result['service_state']
        return result
