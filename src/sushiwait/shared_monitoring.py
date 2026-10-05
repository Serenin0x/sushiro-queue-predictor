"""Offline store demand coalescing; never starts a scheduler or a query."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import re

from .credentials import CredentialError, _read_private_file
from .monitoring import MonitoringError, _stamp, _time, polling_policy


class SharedMonitoringError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


_PLAN_FIELDS = frozenset({'store_id', 'desired_arrival_at', 'call_offset_minutes',
                         'plan_status', 'earliest_call_at', 'accelerated_display_turnover'})


def _store(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[1-9][0-9]{0,11}', value) is None:
        raise SharedMonitoringError('shared_monitor_invalid_input')
    return value


def shared_polling_policy(document: dict, *, as_of: str, base_interval: int) -> dict:
    """At most 128 plans / 3 stores. Emit no per-person plan or arrival times.

    last_poll_started_at means an actual query start supplied by the caller,
    not an upstream update or successful receipt. It cannot lie in the future.
    All times and due flags are offline requests, not permission to query.
    """
    if (type(document) is not dict or set(document) - {'schema_version', 'plans', 'last_poll_started_at'}
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or type(document.get('plans')) is not list or len(document['plans']) > 128
            or type(base_interval) is not int or not 60 <= base_interval <= 3600):
        raise SharedMonitoringError('shared_monitor_invalid_input')
    try:
        current = _time(as_of)
        grouped = {}
        for plan in document['plans']:
            if (type(plan) is not dict or set(plan) - _PLAN_FIELDS
                    or not {'store_id', 'desired_arrival_at'} <= set(plan)):
                raise SharedMonitoringError('shared_monitor_invalid_input')
            store_id = _store(plan['store_id'])
            policy = polling_policy(**{key: value for key, value in plan.items() if key != 'store_id'},
                                    as_of=as_of, base_interval=base_interval)
            grouped.setdefault(store_id, []).append(policy)
            if len(grouped) > 3:
                raise SharedMonitoringError('shared_monitor_store_limit')
        previous = document.get('last_poll_started_at', {})
        if type(previous) is not dict or set(previous) - set(grouped):
            raise SharedMonitoringError('shared_monitor_invalid_input')
        last = {}
        for store_id, value in previous.items():
            started = _time(value)
            if started > current:
                raise SharedMonitoringError('shared_monitor_future_poll_start')
            last[store_id] = started
        stores = []
        for store_id in sorted(grouped):
            policies = grouped[store_id]
            active = [p for p in policies if p['requested_interval_seconds'] is not None]
            interval = min((p['requested_interval_seconds'] for p in active), default=None)
            due = transition = wake = None
            if interval is not None:
                due = (max(current, last[store_id] + timedelta(seconds=interval))
                       if store_id in last else current)
                transitions = []
                for policy in active:
                    if policy['accelerated_display_turnover_input']:
                        continue
                    horizon = _time(policy['monitor_horizon_at'])
                    for minutes in (30, 15):
                        try:
                            boundary = horizon - timedelta(minutes=minutes)
                        except OverflowError:
                            continue  # Below year 1 is earlier than every valid as_of.
                        if boundary > current:
                            transitions.append(boundary)
                transition = min(transitions, default=None)
                wake = min(due, transition) if transition is not None else due
            stores.append({'store_id': store_id, 'plan_count': len(policies),
                'waiting_plan_count': len(active), 'terminal_plan_count': len(policies) - len(active),
                'requested_interval_seconds': interval,
                'requested_poll_due': due == current if due is not None else False,
                'next_poll_target_at': _stamp(due) if due is not None else None,
                'next_policy_transition_at': _stamp(transition) if transition is not None else None,
                'next_recheck_target_at': _stamp(wake) if wake is not None else None,
                'requested_queries_per_due': 1 if active else 0,
                'reestimate_requested': any(p['reestimate_requested'] for p in active)})
    except MonitoringError as error:
        raise SharedMonitoringError('shared_' + error.error_code) from None
    except OverflowError:
        raise SharedMonitoringError('shared_monitor_invalid_time') from None
    wakes = [s['next_recheck_target_at'] for s in stores if s['next_recheck_target_at'] is not None]
    return {'shared_policy_schema_version': 1, 'as_of': _stamp(current),
            'plan_count': len(document['plans']), 'store_count': len(stores), 'stores': stores,
            'next_recheck_target_at': min(wakes, default=None), 'catch_up_requests': 0,
            'personal_plan_details_in_output': False, 'output_requires_private_handling': True,
            'last_poll_input_verified': False, 'scheduler_applied': False,
            'polling_performed': False, 'network_performed': False, 'credentials_accessed': False,
            'notification_sent': False, 'business_operation_performed': False,
            'upstream_frequency_verified': False, 'source_freshness': 'unknown',
            'eta_available': False, 'true_no_show_rate': None,
            'missed_call_prevention_guaranteed': False}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SharedMonitoringError('shared_monitor_invalid_plan_file')
        value[key] = item
    return value


def read_plan_file(path: str | Path) -> dict:
    """Explicit private 16KiB file, strict JSON; no discovery or environment."""
    try:
        raw = _read_private_file(path)
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError
        return value
    except (CredentialError, ValueError, RecursionError, UnicodeError, TypeError):
        raise SharedMonitoringError('shared_monitor_invalid_plan_file') from None
