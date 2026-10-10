"""Single-store continuous worker with bounded current-day memory and archives."""
import threading
import time
from collections import OrderedDict

from .dailyarchive import read_selected_day, read_month, legacy_reader_config
from .dailycontroller import DailyController, daily_config, ZONE
from .dailyview import DailyView
from .remote import RemoteClient
from .remoteservice import RemoteQueueService, LiveRemoteView, RemoteServiceError, _utc
from .remotetasks import RemoteTaskError


class DailyCollectorService(RemoteQueueService):
    def __init__(self, *, root, store_id, business_hours, not_before, daily_pair_cap=1500,
                 legacy_exports_root=None, legacy_through_date=None,
                 wall_clock=_utc, monotonic_clock=time.monotonic, wait=None, client_factory=RemoteClient,
                 base_interval=60):
        self.config = daily_config(root, store_id, business_hours,
            not_before=not_before, daily_pair_cap=daily_pair_cap, base_interval=base_interval)
        self.legacy_reader = legacy_reader_config(legacy_exports_root, legacy_through_date,
            activation=not_before, daily_root=root)
        self.view = LiveRemoteView([store_id], stale_after_seconds=120)
        self.daily_view = DailyView([store_id], hours=business_hours, base_interval=base_interval)
        self.active_day = None
        self.archive_lock, self.archive_cache = threading.Lock(), OrderedDict()
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self.wait, self.client_factory = wait, client_factory
        self.stop_event, self.ready, self.activated = threading.Event(), threading.Event(), threading.Event()
        self.lock = threading.Lock(); self.thread = None
        self.state, self.error_code, self.task_status = 'not_started', None, None

    def _run(self):
        try:
            with DailyController(config=self.config, now=self.wall_clock()) as controller:
                self._set('ready', task=controller.status()); self.ready.set(); self.activated.wait()
                while not self.stop_event.is_set() and controller.value['state'] == 'active':
                    self._set('running', task=controller.status())
                    def attach(view, day):
                        with self.lock:
                            self.daily_view, self.active_day = view, day
                    def emit(event):
                        if 'record' in event:
                            self.view.publish(event['record'])
                        self._set('running', task=controller.status())
                    def sleep(seconds):
                        if self.wait is None: self.stop_event.wait(seconds)
                        else: self.wait(seconds, self.stop_event)
                    controller.tick(wall_clock=self.wall_clock, monotonic_clock=self.monotonic_clock,
                        sleep=sleep, emit=emit, should_stop=self.stop_event.is_set,
                        client_factory=self.client_factory, on_view=attach)
                    self._set('running', task=controller.status())
                    if not self.stop_event.is_set() and controller.value['state'] == 'active': sleep(60)
                self._set('failed' if controller.value['state'] == 'halted' else 'stopped',
                    error=controller.value['error_code'], task=controller.status())
        except BaseException as error:
            self._set('failed', error=str(error) if isinstance(error, RemoteTaskError)
                else 'daily_service_storage_or_input_error')
        finally:
            self.ready.set()

    def status(self):
        value = super().status()
        value.update(bounded_task=False, bounded_daily_tasks=True, automatic_task_restart=False,
            daily_lifecycle=True, phase='daily_tasks_with_waiting',
            duplicate_guard_scope='explicit_store_root_only', network_performed_by_read=False)
        return value

    def daily_index(self, store_id, month=None):
        if store_id != self.config['store_id']: raise RemoteServiceError('daily_service_store_scope')
        now = self.wall_clock()
        month = month or now.astimezone(ZONE).strftime('%Y-%m')
        with self.archive_lock:
            value = read_month(self.config['root'], store_id, month, now=now,
                _cache=self.archive_cache, legacy=self.legacy_reader)
        with self.lock: view, active = self.daily_view, self.active_day
        if active is not None and active.startswith(month+'-'):
            live = view.detail(store_id, active, now=now)
            if live['summary'] is not None:
                value['days'] = [d for d in value['days'] if d['local_date'] != active] + [live['summary']]
                value['days'].sort(key=lambda d: d['local_date'])
                value['missing_archive_dates'] = [d for d in value['missing_archive_dates'] if d != active]
                value['archive_origins'][active] = 'live_current_day'
        return value

    def daily_batch_index(self, month=None):
        store = self.config['store_id']; value = self.daily_index(store, month)
        return {**value, 'days': {d['local_date']: {store: d} for d in value['days']},
            'store_names': {store: '门店 '+store}, 'configured_store_ids': [store],
            'unavailable_store_ids': [store] if value['unavailable_archive_dates'] else []}

    def daily_detail(self, store_id, day):
        if store_id != self.config['store_id']: raise RemoteServiceError('daily_service_store_scope')
        with self.lock: view, active = self.daily_view, self.active_day
        if day == active: return view.detail(store_id, day, now=self.wall_clock())
        try:
            return read_selected_day(self.config['root'], store_id, day, legacy=self.legacy_reader)
        except FileNotFoundError:
            return DailyView([store_id], hours=self.config['business_hours'],
                base_interval=self.config['base_interval']).detail(
                store_id, day, now=self.wall_clock())
        except (ValueError, OSError):
            raise RemoteServiceError('daily_archive_unavailable_or_changed') from None
