"""Explicit catalog, shared process/transport pace, isolated daily store writers.

The catalog is historical evidence, never an assertion of current nationwide
coverage. Every HTTP read uses writer-owned views or verified day archives.
"""
from collections import deque
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import stat
from types import SimpleNamespace

from .dailyarchive import selected_month
from .dailycontroller import _private_dir, _immutable, ZONE
from .dailyservice import DailyCollectorService
from .remote import RemoteClient, SOURCE, _id
from .remoteservice import RemoteServiceError, _utc
from .remotetasks import RemoteTaskError, _at, _now, _json

MAX_STORES = 256
INDEX_MAX_BYTES = 8 * 1024 * 1024
SUMMARY_KEYS = ('store_id', 'local_date', 'observations', 'successful_pairs', 'failed_pairs',
    'scheduled_pause_slots', 'expected_background_slots_so_far', 'observed_background_slots',
    'observed_slot_fraction_so_far', 'last_observation_at')


def validate_catalog(value):
    try:
        keys = {'schema_version', 'directory_observed_at', 'directory_source',
            'directory_currentness_verified', 'current_mainland_completeness_verified', 'stores'}
        if (type(value) is not dict or set(value) != keys or type(value['schema_version']) is not int
                or value['schema_version'] != 1 or value['directory_source'] != 'official_normal_response_provided_by_user'
                or value['directory_currentness_verified'] is not False
                or value['current_mainland_completeness_verified'] is not False
                or type(value['stores']) is not list or not 1 <= len(value['stores']) <= MAX_STORES):
            raise ValueError
        _at(value['directory_observed_at'])
        seen = set()
        for row in value['stores']:
            if type(row) is not dict or set(row) != {'store_id', 'directory_name', 'area'}:
                raise ValueError
            store = _id(row['store_id'])
            if int(store) > 2**31-1 or store in seen:
                raise ValueError
            for key in ('directory_name', 'area'):
                if (type(row[key]) is not str or not 1 <= len(row[key]) <= 100
                        or any(ord(c) < 32 for c in row[key])):
                    raise ValueError
            seen.add(store)
        return deepcopy(value)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise RemoteServiceError('fleet_catalog_invalid') from None


def read_catalog(path):
    # Catalog contains public store names only; bound input and reject symlinks.
    p = Path(path)
    if p.is_symlink() or not p.is_file() or not 1 <= p.stat().st_size <= 128 * 1024:
        raise RemoteServiceError('fleet_catalog_invalid')
    return validate_catalog(_json(p.read_bytes()))


class OriginGate:
    """No accumulated tokens/bursts; persistent halt on upstream 403/429.

    Guards are checked after every wait and again by RemoteClient immediately
    before transport. Admissions are not HTTP counts. Daily journals retain the
    actual attempted flags; closing while queued must not become an attempt.
    """
    def __init__(self, *, root, requests_per_second=5, inflight=8, stop=None):
        if (type(requests_per_second) not in (int, float) or not 0.25 <= requests_per_second <= 6
                or type(inflight) is not int or not 1 <= inflight <= 8):
            raise RemoteServiceError('fleet_transport_bounds_invalid')
        self.root = Path(root)
        self.interval = 1 / requests_per_second
        self.stop = threading.Event() if stop is None else stop
        self.slots = threading.BoundedSemaphore(inflight)
        self.condition = threading.Condition()
        self.waiting, self.next_at = deque(), 0.0
        self.admissions, self.inflight, self.peak_inflight = 0, 0, 0
        marker = self.root/'origin-halted.json'
        self.halted = marker.exists() or marker.is_symlink()

    def enter(self, guard):
        while not self.slots.acquire(timeout=0.1):
            if self.stop.is_set() or self.halted:
                raise RemoteTaskError('fleet_origin_halted_or_stopped')
            if not guard(): return False
        token = object()
        admitted = False
        try:
            with self.condition:
                self.waiting.append(token)
                while True:
                    if self.halted or self.stop.is_set():
                        raise RemoteTaskError('fleet_origin_halted_or_stopped')
                    if not guard(): return False
                    left = self.next_at - time.monotonic()
                    if self.waiting[0] is token and left <= 0:
                        self.next_at = time.monotonic() + self.interval
                        self.admissions += 1
                        self.inflight += 1
                        self.peak_inflight = max(self.peak_inflight, self.inflight)
                        admitted = True
                        return True
                    self.condition.wait(min(0.1, max(0.001, left)))
        finally:
            with self.condition:
                if token in self.waiting: self.waiting.remove(token)
                self.condition.notify_all()
            if not admitted: self.slots.release()

    def leave(self, result=None):
        try:
            with self.condition:
                self.inflight -= 1
                if result is not None and result.http_status in (403, 429):
                    self.halted = True
                    _immutable(self.root/'origin-halted.json', {'schema_version': 1,
                        'reason': 'upstream_denied_or_limited', 'automatic_resume': False})
                self.condition.notify_all()
        finally:
            self.slots.release()

    def status(self):
        with self.condition:
            return {'maximum_request_starts_per_second': 1/self.interval,
                'transport_admissions_this_process': self.admissions,
                'inflight': self.inflight, 'peak_inflight': self.peak_inflight,
                'origin_halted': self.halted, 'automatic_resume_after_origin_halt': False}


class FleetRemoteClient(RemoteClient):
    def __init__(self, gate, **kwargs):
        super().__init__(**kwargs)
        self.gate = gate

    def fetch(self, endpoint, store_id):
        if self.gate.halted or self.gate.stop.is_set():
            raise RemoteTaskError('fleet_origin_halted_or_stopped')
        original_guard = self.request_guard
        admitted = False
        def paced_guard(store):
            nonlocal admitted
            guard = lambda: original_guard is None or original_guard(store)
            admitted = self.gate.enter(guard)
            return admitted and guard()
        # Opener/TLS setup is performed first by RemoteClient. Pacing then
        # occurs at its transport guard, rather than before variable setup.
        self.request_guard = paced_guard
        result = None
        try:
            result = super().fetch(endpoint, store_id)
            return result
        finally:
            self.request_guard = original_guard
            if admitted: self.gate.leave(result)


class DailyFleetService:
    def __init__(self, *, root, catalog, business_hours, not_before, daily_pair_cap=1500,
                 legacy_exports_root=None, legacy_through_date=None, legacy_store_ids=(),
                 requests_per_second=5, wall_clock=_utc, child_factory=DailyCollectorService,
                 opener_factory=None):
        self.catalog = validate_catalog(catalog)
        self.root = Path(os.path.abspath(root))
        if len(str(self.root)) > 370: raise RemoteServiceError('fleet_root_invalid')
        self.names = {s['store_id']: s['directory_name'] for s in self.catalog['stores']}
        if (len(set(legacy_store_ids)) != len(legacy_store_ids)
                or set(legacy_store_ids) - set(self.names)
                or (legacy_exports_root is None) != (legacy_through_date is None)
                or bool(legacy_store_ids) != bool(legacy_exports_root)):
            raise RemoteServiceError('fleet_legacy_scope_invalid')
        self.view = SimpleNamespace(stores=tuple(self.names))
        self.daily_view = True
        self.wall_clock = wall_clock
        self.stop = threading.Event()
        self.gate = OriginGate(root=self.root, requests_per_second=requests_per_second, stop=self.stop)
        self.children = {}
        self.lock_fd = None
        self.started = False
        for store in self.names:
            def factory():
                return FleetRemoteClient(self.gate,
                    **({'opener': opener_factory()} if opener_factory is not None else {}))
            self.children[store] = child_factory(root=self.root/('store-'+store), store_id=store,
                business_hours=business_hours, not_before=not_before, daily_pair_cap=daily_pair_cap,
                legacy_exports_root=legacy_exports_root if store in legacy_store_ids else None,
                legacy_through_date=legacy_through_date if store in legacy_store_ids else None,
                wall_clock=wall_clock, client_factory=factory)
        self.configuration = {'schema_version': 1, 'catalog': self.catalog,
            'business_hours': deepcopy(business_hours), 'not_before': _now(_at(not_before)),
            'daily_pair_cap': daily_pair_cap, 'requests_per_second': requests_per_second,
            'legacy_exports_root': legacy_exports_root, 'legacy_through_date': legacy_through_date,
            'legacy_store_ids': list(legacy_store_ids)}

    def start(self):
        if self.started: raise RemoteServiceError('remote_service_already_started')
        _private_dir(self.root)
        self.lock_fd = os.open(self.root/'fleet.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise RemoteServiceError('fleet_lock_unsafe')
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _immutable(self.root/'fleet-config.json', self.configuration)
            if self.gate.halted: raise RemoteServiceError('fleet_origin_halt_requires_review')
            self.started = True
            for child in self.children.values(): child.start()
        except BaseException:
            self.shutdown()
            raise RemoteServiceError('fleet_startup_unconfirmed') from None

    def shutdown(self):
        self.stop.set()
        # Signal every child before joining any, so shutdown has one transport
        # timeout rather than N consecutive transport waits.
        for child in self.children.values():
            child.stop_event.set(); child.activated.set()
        deadline = time.monotonic() + 35
        for child in self.children.values():
            if child.thread is not None: child.thread.join(max(0, deadline-time.monotonic()))
        pending = any(c.thread is not None and c.thread.is_alive() for c in self.children.values())
        if not pending and self.lock_fd is not None:
            os.close(self.lock_fd); self.lock_fd = None
        if pending: raise RemoteServiceError('fleet_shutdown_unconfirmed')

    def child(self, store):
        if store not in self.children: raise RemoteServiceError('fleet_store_scope')
        return self.children[store]

    def status(self, store=None):
        if store is not None:
            return {**self.child(store).status(), 'store_names': deepcopy(self.names),
                'fleet_store_ids': list(self.names), 'status_scope': 'selected_store_worker',
                'origin_gate': self.gate.status()}
        states = {s: c.status() for s, c in self.children.items()}
        failures = [s for s, v in states.items() if v['service_state'] == 'failed']
        alive = sum(v['worker_alive'] for v in states.values())
        return {'service_schema_version': 1, 'source': SOURCE,
            'service_state': 'failed' if self.gate.halted else 'running' if alive else 'not_started' if not self.started else 'stopped',
            'worker_alive': bool(alive), 'store_ids': list(self.names), 'store_names': deepcopy(self.names),
            'alive_store_workers': alive, 'failed_store_ids': failures,
            'shared_process': True, 'bounded_daily_tasks': True, 'automatic_task_restart': False,
            'network_performed_by_read': False, 'source_freshness': 'unknown', 'eta_available': False,
            'verified_training_labels': 0, 'origin_gate': self.gate.status(),
            'directory_observed_at': self.catalog['directory_observed_at'],
            'current_mainland_completeness_verified': False}

    def store_view(self, store): return self.child(store).store_view(store)
    def monitor_history(self, store): return self.child(store).monitor_history(store)
    def fusion_context(self, store): return self.child(store).fusion_context(store)
    def tracking_projection(self, store): return self.child(store).tracking_projection(store)
    def daily_index(self, store, month=None): return self.child(store).daily_index(store, month)
    def daily_detail(self, store, day): return self.child(store).daily_detail(store, day)

    def daily_batch_index(self, month=None):
        month = month or self.wall_clock().astimezone(ZONE).strftime('%Y-%m')
        selected_month(month)
        days, unavailable = {}, []
        for store in self.names:
            try:
                value = self.daily_index(store, month)
                if value['unavailable_archive_dates']: unavailable.append(store)
                for summary in value['days']:
                    days.setdefault(summary['local_date'], {})[store] = {k: summary[k] for k in SUMMARY_KEYS}
            except (RemoteServiceError, RemoteTaskError, OSError, ValueError):
                unavailable.append(store)
        status=self.status()
        failures=status['failed_store_ids']
        if status['origin_gate']['origin_halted']:
            failures=list(self.names)
        unavailable=list(dict.fromkeys(unavailable+failures))
        result = {'daily_schema_version': 1, 'source': SOURCE, 'month': month,
            'days': days, 'store_names': deepcopy(self.names), 'configured_store_ids': list(self.names),
            'unavailable_store_ids': unavailable,
            'collector_failed_store_ids': failures,
            'collector_process_state': status['service_state'],
            'origin_halted': status['origin_gate']['origin_halted'],
            'cohort_id': hashlib.sha256(json.dumps(list(self.names), separators=(',', ':')).encode()).hexdigest(),
            'summary_projection': 'compact_calendar_only_full_summary_in_day_detail',
            'heatmap_semantics': 'coverage_only_traffic_not_calibrated', 'actual_called_count': None,
            'directory_observed_at': self.catalog['directory_observed_at'],
            'current_mainland_completeness_verified': False,
            'network_performed_by_read': False, 'eta_available': False}
        if len(json.dumps(result, ensure_ascii=False).encode()) > INDEX_MAX_BYTES:
            raise RemoteServiceError('fleet_index_too_large')
        return result
