"""Finite multi-day controller for a separately approved campaign.

This does not relax the legacy trial validator or extend any running trial.
Use a NEW private root and explicit plan, after the operator approves dates,
stores, budgets and supplier access. Importing does not read plans or account
material and does not query any endpoint.
"""
from datetime import datetime, timezone, timedelta
from sushiwait.cityscope import validate_scope
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

_spec = importlib.util.spec_from_file_location(
    'campaign_legacy', Path(__file__).with_name('official_cloud_controller.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)
TrialError = legacy.TrialError
UTC = timezone.utc
CHINA = timezone(timedelta(hours=8))
STORES = legacy.STORES | {'3015', '2001'}


def validate_plan(value):
    keys = {'schema_version', 'plan_id', 'app_id', 'source_version', 'package_sha256',
            'store_ids', 'windows', 'renewal_limit', 'interval_seconds', 'ordinary_query_limit'}
    try:
        if (type(value) is not dict or set(value) not in (keys, keys | {'cadence'}, keys | {'cadence','fleet_scope'})
                or type(value['schema_version']) is not int or value['schema_version'] != (4 if 'fleet_scope' in value else 3 if 'cadence' in value else 2)
                or 'cadence' in value and value['cadence'] not in ('meal-peaks-v1','calendar-meal-peaks-v1')
                or value.get('cadence')=='calendar-meal-peaks-v1' and 'fleet_scope' not in value
                or type(value['plan_id']) is not str
                or not re.fullmatch('[a-z][a-z0-9-]{3,63}', value['plan_id'])
                or type(value['app_id']) is not str
                or not re.fullmatch('wx[0-9a-f]{16}', value['app_id'])
                or type(value['source_version']) is not int or value['source_version'] != 181
                or type(value['package_sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', value['package_sha256'])
                or type(value['store_ids']) is not list or not 1 <= len(value['store_ids']) <= (128 if 'fleet_scope' in value else 5 if 'cadence' in value else 3)
                or not (validate_scope(value['fleet_scope'], value['store_ids']) if 'fleet_scope' in value else all(type(s) is str and s in (STORES if 'cadence' in value else legacy.STORES) for s in value['store_ids']))
                or len(set(value['store_ids'])) != len(value['store_ids'])
                or type(value['interval_seconds']) is not int or value['interval_seconds'] != 600
                or type(value['windows']) is not list or not 1 <= len(value['windows']) <= 7
                or type(value['renewal_limit']) is not int
                or not 1 <= value['renewal_limit'] <= 14 * len(value['windows'])
                or type(value['ordinary_query_limit']) is not int):
            raise ValueError
        windows = []
        for item in value['windows']:
            if type(item) is not dict or set(item) != {'start', 'end'}:
                raise ValueError
            if any(type(item[k]) is not str for k in ('start', 'end')):
                raise ValueError
            start, end = (datetime.fromisoformat(item[k]) for k in ('start', 'end'))
            seconds = (end - start).total_seconds()
            if (start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8)
                    or start.date() != end.date() or start.microsecond or end.microsecond
                    or not 0 < seconds <= 41400
                    or windows and start.date() <= windows[-1][0].astimezone(CHINA).date()):
                raise ValueError
            windows.append((start.astimezone(UTC), end.astimezone(UTC), legacy.planned_rounds(start,end,value['cadence']) if 'cadence' in value else math.ceil(seconds / 600)))
        if (windows[-1][0].astimezone(CHINA).date()
                - windows[0][0].astimezone(CHINA).date()).days > 6:
            raise ValueError
        maximum = sum(rounds * len(value['store_ids']) for _, _, rounds in windows)
        if not 1 <= value['ordinary_query_limit'] <= maximum:
            raise ValueError
        return windows
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TrialError('cloud_campaign_plan_invalid') from None


def task_path(root, start, cadence=None):
    suffix = 'smooth' if cadence else '600'
    return Path(root) / f'collection-{start.astimezone(CHINA).date()}-{suffix}.json'


def query_usage(root, plan):
    """Count completed (including failed/unknown) and pending reservations."""
    used, histories = 0, {}
    for start, _, maximum in validate_plan(plan):
        path = task_path(root, start, plan.get('cadence'))
        if not os.path.lexists(path):
            continue
        status = legacy.task_status(path)
        if (status['store_ids'] != plan['store_ids']
                or status['api_profile'] != 'miniapp_gateway'
                or status['interval_seconds'] != 600 or status['target_rounds'] > maximum
                or bool(plan.get('cadence')) != ('cadence_policy' in status)
                or plan.get('cadence') and status['cadence_policy']['id'] != plan['cadence']):
            raise TrialError('cloud_task_budget_invalid')
        used += status['completed_slots'] + int(status['pending_attempt'])
        histories[path] = status
    if used > plan['ordinary_query_limit']:
        raise TrialError('cloud_task_budget_invalid')
    return used, histories


def window_budget(root, plan, start):
    windows = validate_plan(plan)
    matching = [rounds for beginning, _, rounds in windows if beginning == start]
    if len(matching) != 1:
        raise TrialError('cloud_campaign_plan_invalid')
    used, histories = query_usage(root, plan)
    task = task_path(root, start, plan.get('cadence'))
    previous = histories.get(task)
    remaining = plan['ordinary_query_limit'] - used
    if previous is not None:
        unspent = previous['target_slots'] - previous['completed_slots'] - int(previous['pending_attempt'])
        if unspent > remaining:
            raise TrialError('cloud_task_budget_invalid')
        return task, previous['target_rounds'], True, previous
    return task, min(matching[0], remaining // len(plan['store_ids'])), False, None


class CampaignLedger(legacy.Ledger):
    def __init__(self, root, plan):
        validate_plan(plan)
        # Initial + reservation/acceptance per refresh + each finished day + halt.
        self.checkpoint_limit = 2 * plan['renewal_limit'] + len(plan['windows']) + 2
        super().__init__(root, plan)


def run_campaign(root, plan, ledger, stopped, *, clock=None, popen=None, prepare=None, budget=None):
    """Shared validated initialization pipeline; no implicit retry or catch-up."""
    clock = legacy.utc_now if clock is None else clock
    popen = subprocess.Popen if popen is None else popen
    prepare = legacy.prepare_material_source if prepare is None else prepare
    budget = window_budget if budget is None else budget
    controller = child = None
    try:
        for index, (start, end, _) in enumerate(validate_plan(plan)):
            if index in ledger.value['finished_windows']:
                continue
            while clock() < start and not stopped.is_set():
                stopped.wait(min(10, max(0, (start - clock()).total_seconds())))
            if stopped.is_set():
                return 0
            if clock() >= end:
                ledger.finish(index)
                print(json.dumps({'event': 'cloud_window_missed', 'index': index, 'backfill': False}), flush=True)
                continue
            task, rounds, resume, previous = budget(root, plan, start)
            if rounds == 0:
                print(json.dumps({'event': 'cloud_ordinary_budget_exhausted',
                                  'limit': plan['ordinary_query_limit'], 'automatic_retry': False}), flush=True)
                ledger.finish(index)
                return 0
            if resume:
                if (previous['failed_slots'] or previous['uncertain_slots'] or previous['pending_attempt']
                        or previous['stop_reason'] not in {None, 'auth_expiring', 'auth_declared_expired'}):
                    raise TrialError('cloud_reader_failed')
                if previous['state'] == 'completed':
                    ledger.finish(index)
                    continue
            if controller is None:
                source = prepare(bundle_file=Path(root) / 'initialization-material.json',
                                 expected_app_id=plan['app_id'], expected_source_version=plan['source_version'],
                                 expected_package_sha256=plan['package_sha256'])
                controller = legacy.Controller(root, plan, source, ledger)
            controller.ensure_refresh(start, end, now=clock())
            child = popen(legacy.collect_arguments(root, plan['store_ids'], rounds, task, resume,
                                                   start, end, interval=600, cadence=plan.get('cadence')),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(json.dumps({'event': 'cloud_reader_started', 'window': index,
                              'pid': child.pid, 'resume': resume, 'target_rounds': rounds}), flush=True)
            while clock() < end and not stopped.is_set():
                if child.poll() is not None:
                    if child.returncode != 0:
                        raise TrialError('cloud_reader_failed')
                    break
                controller.ensure_refresh(start, end, now=clock())
                stopped.wait(1)
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=20)
            if stopped.is_set():
                return 0
            status = legacy.task_status(task)
            if status['failed_slots'] or status['uncertain_slots'] or status['pending_attempt']:
                raise TrialError('cloud_reader_failed')
            if status['state'] != 'completed' and clock() < end - timedelta(seconds=16):
                raise TrialError('cloud_reader_failed')
            ledger.finish(index)
            print(json.dumps({'event': 'cloud_window_finished', 'window': index,
                              'successful_slots': status['successful_slots'], 'backfill': False}), flush=True)
            child = None
        print('{"event":"cloud_finite_campaign_finished"}', flush=True)
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main():
    if len(sys.argv) != 2:
        print('{"ok":false,"error_code":"cloud_root_required"}')
        return 2
    os.umask(0o077)
    lock = ledger = None
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        root = Path(sys.argv[1])
        if not root.is_absolute() or root.name == '' or root.is_symlink():
            raise TrialError('cloud_campaign_plan_invalid')
        plan = legacy._decode(legacy._read_private_file(root / 'campaign-plan.json'))
        validate_plan(plan)
        # Binding the new root name prevents accidentally spending an old trial's
        # budget with this controller. The immutable ledger additionally pins SHA.
        if root.name != plan['plan_id'] or os.path.lexists(root / 'trial-plan.json'):
            raise TrialError('cloud_campaign_root_invalid')
        lock = os.open(root / 'controller.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = root / 'controller-state'
        state.mkdir(mode=0o700, exist_ok=True)
        ledger = CampaignLedger(state, plan)
        return run_campaign(root, plan, ledger, stopped)
    except Exception as error:
        allowed = {'cloud_campaign_plan_invalid', 'cloud_campaign_root_invalid', 'cloud_task_budget_invalid', 'cloud_json_invalid',
                   'cloud_reader_failed', 'cloud_refresh_failed', 'cloud_refresh_limit',
                   'cloud_refresh_outcome_unknown', 'cloud_trial_halted', 'cloud_ledger_invalid',
                   'cloud_checkpoint_limit', 'cloud_checkpoint_unconfirmed', 'cloud_context_changed',
                   'cloud_context_missing', 'cloud_context_invalid', 'cloud_auth_timing_unknown',
                   'cloud_business_window_closed', 'cloud_revision_invalid'}
        code = str(error) if isinstance(error, TrialError) and str(error) in allowed else 'cloud_trial_failed'
        if ledger is not None:
            try:
                ledger.halt('cloud_reader_failed' if code == 'cloud_reader_failed' else 'cloud_trial_failed')
            except Exception:
                pass
        print(json.dumps({'ok': False, 'event': 'cloud_campaign_stopped',
                          'error_code': code, 'automatic_retry': False}), flush=True)
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
