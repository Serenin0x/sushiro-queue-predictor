"""Bounded adaptive scheduling; explicit private plans and normal read-only GETs."""
from copy import deepcopy
import math
from .shared_monitoring import shared_polling_policy, SharedMonitoringError, read_plan_file
from .monitoring import _time, _stamp


class ScheduleError(ValueError):
    def __init__(self, error_code):
        super().__init__(error_code)
        self.error_code = error_code


def schedule_from_file(path, *, allowed_stores, base_interval, duration_seconds,
                       max_queries, wall, monotonic):
    try:
        document = read_plan_file(path)
    except SharedMonitoringError:
        raise ScheduleError('schedule_invalid_private_plan') from None
    return AdaptiveSchedule(document, allowed_stores=allowed_stores, base_interval=base_interval,
        duration_seconds=duration_seconds, max_queries=max_queries, wall=wall, monotonic=monotonic)


class AdaptiveSchedule:
    def __init__(self, document, *, allowed_stores, base_interval, duration_seconds,
                 max_queries, wall, monotonic):
        if (type(allowed_stores) is not list or not 1 <= len(allowed_stores) <= 3
                or any(type(s) is not str or not s.isascii() or not s.isdecimal()
                       or s.startswith('0') or not 1 <= len(s) <= 12 for s in allowed_stores)
                or len(set(allowed_stores)) != len(allowed_stores)
                or type(duration_seconds) is not int or not 30 <= duration_seconds <= 3600
                or type(max_queries) is not int or not 1 <= max_queries <= 360
                or type(document) is not dict or document.get('last_poll_started_at') not in (None, {})):
            raise ScheduleError('schedule_invalid_input')
        self.document = deepcopy(document)
        self.allowed = set(allowed_stores)
        self.base = base_interval
        self.deadline = self.number(monotonic) + duration_seconds
        self.maximum = max_queries
        self.starts = {}
        self.defer = {}
        self.count = 0
        self.last_clock = None
        self.last_wall = None
        self.decision(wall=wall, monotonic=monotonic)

    @staticmethod
    def number(value):
        if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
            raise ScheduleError('schedule_invalid_clock')
        return value

    def clock(self, wall, monotonic):
        try:
            at = _time(wall)
        except ValueError:
            raise ScheduleError('schedule_invalid_clock') from None
        mono = self.number(monotonic)
        if (self.last_clock is not None and mono < self.last_clock
                or self.last_wall is not None and at < self.last_wall):
            raise ScheduleError('schedule_clock_backwards')
        self.last_clock, self.last_wall = mono, at
        return at, mono

    def decision(self, *, wall, monotonic):
        at, mono = self.clock(wall, monotonic)
        if mono >= self.deadline or self.count >= self.maximum:
            return {'done': True, 'due_stores': [], 'wake_monotonic': None}
        doc = deepcopy(self.document)
        doc['last_poll_started_at'] = {s: _stamp(v[0]) for s, v in self.starts.items()}
        try:
            policy = shared_polling_policy(doc, as_of=_stamp(at), base_interval=self.base)
        except (SharedMonitoringError, TypeError, ValueError):
            raise ScheduleError('schedule_invalid_plan') from None
        if any(s['store_id'] not in self.allowed for s in policy['stores']):
            raise ScheduleError('schedule_store_scope')
        active, due, wakes = 0, [], []
        for store in policy['stores']:
            s, interval = store['store_id'], store['requested_interval_seconds']
            if interval is None:
                continue
            active += 1
            target = mono if s not in self.starts else self.starts[s][1] + interval
            target = max(target, self.defer.get(s, mono))
            if target <= mono:
                due.append(s)
            wake = max(mono, target)
            transition = store['next_policy_transition_at']
            if transition is not None:
                wake = min(wake, mono + (_time(transition) - at).total_seconds())
            wakes.append(wake)
        return {'done': active == 0, 'due_stores': due,
                'wake_monotonic': min([self.deadline, *wakes]) if active else None}

    def mark_actual_start(self, store_id, *, wall, monotonic):
        # The caller must supply the instant immediately before its real GET.
        result = self.decision(wall=wall, monotonic=monotonic)
        if store_id not in result['due_stores']:
            raise ScheduleError('schedule_start_not_due')
        self.starts[store_id] = (_time(wall), self.number(monotonic))
        self.count += 1

    def resumed(self, *, wall, monotonic):
        # Recovery adds a fresh interval; actual request starts stay unchanged.
        at, mono = self.clock(wall, monotonic)
        doc = deepcopy(self.document)
        doc['last_poll_started_at'] = {s: _stamp(v[0]) for s, v in self.starts.items()}
        policy = shared_polling_policy(doc, as_of=_stamp(at), base_interval=self.base)
        for store in policy['stores']:
            interval = store['requested_interval_seconds']
            if interval is not None:
                self.defer[store['store_id']] = mono + interval


def run_adaptive(schedule, session, database, *, wall_clock, monotonic_clock,
                 observe, record_stop, emit, preflight_stop):
    """Bounded normal GETs, fail-stop on invalid auth.

    A normal credential update is supplied separately. This driver doesn't
    initiate authentication, run native commands or create long-lived jobs.
    """
    succeeded = 0
    while True:
        decision = schedule.decision(wall=wall_clock().isoformat(), monotonic=monotonic_clock())
        if decision['done']:
            emit({'event':'adaptive_collection_finished','queries_started':schedule.count,
                  'successful_queries':succeeded,'scheduler_applied':True,'in_process_only':True,
                  'eta_available':False,'upstream_frequency_verified':False,
                  'true_no_show_rate':None,'notification_sent':False,
                  'business_operation_performed':False,
                  'missed_call_prevention_guaranteed':False,
                  'source_freshness':'unknown','output_requires_private_handling':True})
            return 0
        if not decision['due_stores']:
            try:
                session.wait_until(decision['wake_monotonic'])
            except preflight_stop as stop:
                # Pick only an active scope; the stop belongs to the session,
                # not a failed HTTP request or an individual diner.
                active = next(p['store_id'] for p in schedule.document['plans']
                              if p.get('plan_status','waiting') == 'waiting')
                record_stop(database, active, session.args.api_profile, stop)
                return 1
            continue
        store_id = decision['due_stores'][0]
        try:
            client = session.client()
        except preflight_stop as stop:
            record_stop(database, store_id, session.args.api_profile, stop)
            return 1
        current = schedule.decision(wall=wall_clock().isoformat(), monotonic=monotonic_clock())
        if current['done']:
            continue
        if store_id not in current['due_stores']:
            continue
        schedule.mark_actual_start(store_id,wall=wall_clock().isoformat(),monotonic=monotonic_clock())
        if not observe(client,store_id,database,api_profile=session.args.api_profile):
            return 1
        succeeded += 1
