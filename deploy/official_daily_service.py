"""Recurring business-hours collection with isolated, immutable daily journals.

The owner enables this service explicitly. Each day has its own finite window,
query allowance and renewal journal. A failed/uncertain supplier or reader is
latched across midnight and restarts, rather than retried with a fresh identity.
Public projection is a separate mode and never reads account material.
"""
from datetime import datetime, timedelta
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from sushiwait.calendar import date_features
from sushiwait.credentials import _read_private_file
from sushiwait.packets import PacketError, _write_packet, encoded


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


campaign = sibling('official_cloud_campaign')
projection = sibling('official_cloud_campaign_sync')
reauthorization = sibling('official_reauthorization')
UTC, CHINA = campaign.UTC, campaign.CHINA
STORES = ['3014', '3004', '2009', '3015', '2001']


def window_for(at):
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('daily_clock_invalid')
    local = at.astimezone(CHINA)
    features = date_features(local.isoformat(), as_of=local.isoformat())
    early = local.weekday() >= 5 or features['date_type'] == 'holiday'
    start = local.replace(hour=10 if early else 11, minute=30 if early else 0,
                          second=0, microsecond=0)
    end = local.replace(hour=22, minute=0, second=0, microsecond=0)
    return start, end


def configured_stores(config):
    return [s for g in config['archive_groups'] for s in g['store_ids']]


def read_config(root):
    value = campaign.legacy._decode(_read_private_file(root / 'daily-config.json'))
    if (set(value) != {'schema_version','first_day','source','supplier_root','archive_groups'}
            or value['schema_version'] not in (1,2)
            or value['first_day'] != ('2026-10-15' if value['schema_version']==1 else '2026-10-16')
            or set(value['source']) != {'app_id','source_version','package_sha256'}
            or value['supplier_root'] != '/home/ubuntu/sushiwait-official-cloud-e0462-20261012'
            or type(value['archive_groups']) is not list):
        raise ValueError('daily_config_invalid')
    groups=value['archive_groups']
    if value['schema_version']==1:
        if ([g.get('store_ids') for g in groups] != [STORES[:3],STORES[3:]]
                or [g.get('name') for g in groups] != ['primary','extra']):
            raise ValueError('daily_archive_scope_invalid')
    else:
        from sushiwait.cityscope import STORES as allowed, SCOPE_ID, validate_scope
        stores=configured_stores(value)
        if (not 1<=len(groups)<=64 or not validate_scope(SCOPE_ID,stores)
                or set(stores)!=set(allowed) or len({g.get('name') for g in groups})!=len(groups)
                or len({g.get('archive') for g in groups})!=len(groups)):
            raise ValueError('daily_archive_scope_invalid')
    import re
    if any(set(g)!={'name','store_ids','archive'} or not re.fullmatch('[a-z]+',g['name'])
           or not 1<=len(g['store_ids'])<=3 or not Path(g['archive']).is_absolute() for g in groups):
        raise ValueError('daily_archive_scope_invalid')
    make_plan(value,datetime.fromisoformat(value['first_day']+'T11:00:00+08:00'))
    return value


def make_plan(config, at):
    start, end = window_for(at)
    if start.date().isoformat() < config['first_day']:
        raise ValueError('daily_start_not_reached')
    stores=configured_stores(config)
    expanded=config['schema_version']==2
    rounds = campaign.legacy.planned_rounds(start, end, 'meal-peaks-v1')
    plan = {**config['source'], 'schema_version': 4 if expanded else 3,
            'plan_id': ('five-cities-' if expanded else 'five-stores-') + start.strftime('%Y%m%d'),
            'store_ids': stores, 'interval_seconds': 600, 'cadence': 'meal-peaks-v1',
            'windows': [{'start': start.isoformat(), 'end': end.isoformat()}],
            'ordinary_query_limit': rounds * len(stores), 'renewal_limit': 14}
    if expanded: plan['fleet_scope']='five-cities-v1'
    campaign.validate_plan(plan)
    return plan


def put_once(path, body):
    if os.path.lexists(path):
        if _read_private_file(path) != body:
            raise ValueError('daily_existing_file_changed')
        return
    result = _write_packet(body, path)
    if not result['durability_confirmed']:
        raise ValueError('daily_file_unconfirmed')


def prepare_day(root, config, at):
    plan = make_plan(config, at)
    day = root / plan['plan_id']
    day.mkdir(mode=0o700, exist_ok=True)
    if day.is_symlink() or day.stat().st_mode & 0o777 != 0o700:
        raise ValueError('daily_root_unsafe')
    put_once(day / 'campaign-plan.json', encoded(plan))
    # Same approved server, same existing account/settings. No old query JWT,
    # Mac data, source package or external upload is involved.
    supplier = Path(config['supplier_root'])
    for name in ('initialization-material.json', 'bootstrap-settings.json', 'renewal-settings.json'):
        put_once(day / name, _read_private_file(supplier / name))
    put_once(day / 'official_business_collect.py', Path(__file__).with_name('official_business_collect.py').read_bytes())
    return day, plan


def publish_status(root, mode, value):
    value = {'schema_version': 1, 'at': datetime.now(UTC).isoformat(), **value}
    path = root / (mode + '-status.json')
    pending = path.with_suffix('.pending')
    body = encoded(value)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def latched(root):
    return reauthorization.active_block(root) is not None


def latch(root, code, day):
    reauthorization.latch(root, code, day, datetime.now(UTC))


def accept_reauthorization(root, config, now):
    """Only new signed owner confirmations may clear a stopped incident."""
    settings_file = root / 'recovery-settings.json'
    if not settings_file.exists():
        return False
    settings = reauthorization.read(settings_file)
    if (set(settings) != {'schema_version', 'receipt_directory', 'signing_key_file'}
            or settings['schema_version'] != 1):
        raise ValueError('daily_recovery_settings_invalid')
    receipt_dir, key_file = Path(settings['receipt_directory']), Path(settings['signing_key_file'])
    if not receipt_dir.is_absolute() or not key_file.is_absolute():
        raise ValueError('daily_recovery_settings_invalid')
    key = _read_private_file(key_file)
    if len(key) != 32:
        raise ValueError('daily_recovery_key_invalid')
    block = reauthorization.active_block(root)
    if not block:
        return False
    if not reauthorization.requires_scan(root, block):
        return False
    paths = sorted(receipt_dir.glob('*.json'))
    if len(paths) > 1000:
        raise ValueError('daily_recovery_receipt_limit')
    for path in reversed(paths):
        try:
            value = reauthorization.read(path)
            reauthorization.validate_receipt(value, block, key, now)
        except (ValueError, TypeError, KeyError):
            continue
        target = now
        if now >= window_for(now)[1]:
            target = now + timedelta(days=1)
        day, plan = prepare_day(root, config, target)
        return reauthorization.prepare_recovery(root, day, plan, value, key, now)
    return False


def run_collection(root, config, stopped):
    """Across-day loop; every upstream call remains inside the daily guard."""
    while not stopped.is_set():
        now = datetime.now(UTC)
        start, end = window_for(now)
        day_text = start.date().isoformat()
        if latched(root):
            try:
                if accept_reauthorization(root, config, now):
                    print('{"event":"daily_owner_reauthorization_accepted"}', flush=True)
                    continue
            except Exception:
                print('{"event":"daily_owner_reauthorization_not_accepted"}', flush=True)
            publish_status(root, 'collection', {'state': 'needs_attention', 'day': day_text,
                           'error_code': 'collection_latched', 'store_ids': configured_stores(config)})
            stopped.wait(10)
            continue
        deferred = reauthorization.resume_not_before(root)
        if deferred and now < deferred:
            publish_status(root, 'collection', {'state': 'closed', 'day': day_text,
                           'next_open_at': deferred.isoformat(), 'store_ids': configured_stores(config)})
            stopped.wait(min(60, max(1, (deferred-now).total_seconds())))
            continue
        if not start <= now < end:
            next_at = start if now < start else window_for(now + timedelta(days=1))[0]
            publish_status(root, 'collection', {'state': 'closed', 'day': day_text,
                           'next_open_at': next_at.isoformat(), 'store_ids': configured_stores(config)})
            stopped.wait(min(60, max(1, (next_at - now).total_seconds())))
            continue
        try:
            day, plan = prepare_day(root, config, now)
            state, selected_task = reauthorization.selected(day, plan)
            state.mkdir(mode=0o700, exist_ok=True)
            ledger = campaign.CampaignLedger(state, plan)
            if ledger.value['finished_windows']:
                publish_status(root, 'collection', {'state': 'completed', 'day': day_text,
                               'store_ids': configured_stores(config)})
                stopped.wait(min(60, max(1, (end - now).total_seconds())))
                continue
            publish_status(root, 'collection', {'state': 'starting', 'day': day_text,
                           'opened_at': start.isoformat(), 'closes_at': end.isoformat(),
                           'cadence': plan['cadence'], 'store_ids': configured_stores(config)})
            if config['schema_version']==2 and (root/'fleet-policy.json').exists():
                policy=reauthorization.read(root/'fleet-policy.json')
                if policy != {'schema_version':1,'policy':'store-isolation-v1'}:
                    raise ValueError('fleet_policy_invalid')
                sibling('official_fleet_collect').run(__import__('types').SimpleNamespace(campaign=campaign, reauthorization=reauthorization, publish_status=publish_status, CHINA=CHINA), root, day, plan, ledger, stopped)
                continue
            last_pulse = None
            def pulse():
                nonlocal last_pulse
                at = datetime.now(UTC)
                if last_pulse is None or (at - last_pulse).total_seconds() >= 30:
                    task = selected_task
                    status = campaign.legacy.task_status(task) if task.exists() else None
                    publish_status(root, 'collection', {'state': 'running' if status else 'starting',
                        'day': day_text, 'opened_at': start.isoformat(), 'closes_at': end.isoformat(),
                        'cadence': plan['cadence'], 'store_ids': configured_stores(config),
                        'successful_slots': status['successful_slots'] if status else 0,
                        'failed_slots': status['failed_slots'] if status else 0,
                        'last_progress_at': status['updated_at'] if status else None})
                    last_pulse = at
                return at
            result = campaign.run_campaign(day, plan, ledger, stopped, clock=pulse,
                                           budget=reauthorization.window_budget)
            if result != 0:
                raise campaign.TrialError('cloud_reader_failed')
        except Exception as error:
            code = str(error) if isinstance(error, campaign.TrialError) else 'daily_collection_failed'
            if str(error) == 'fleet_shared_authorization_failure':
                code = 'cloud_query_authorization_failed'
            allowed = {'cloud_reader_failed', 'cloud_refresh_failed', 'cloud_refresh_limit',
                       'cloud_refresh_outcome_unknown', 'cloud_trial_halted', 'cloud_ledger_invalid',
                       'cloud_query_authorization_failed'}
            code = code if code in allowed else 'daily_collection_failed'
            # A new date never grants a fresh retry of yesterday's failed session.
            latch(root, code, day_text)
            publish_status(root, 'collection', {'state': 'needs_attention', 'day': day_text,
                           'error_code': code, 'store_ids': configured_stores(config)})
            print(json.dumps({'event': 'daily_collection_needs_attention', 'error_code': code}), flush=True)


def run_projection(root, config, stopped):
    """Project both groups into existing archives, keeping all historic days."""
    active_day, projections, deferred_counts = None, [], {}
    try:
        while not stopped.is_set():
            now = datetime.now(UTC)
            plan = make_plan(config, now)
            day = root / plan['plan_id']
            if active_day != day:
                for item in projections:
                    item.close()
                projections = []
                active_day = day
                deferred_counts = {}
            if not (day / 'observations.sqlite').exists():
                publish_status(root, 'projection', {'state': 'waiting', 'day': now.astimezone(CHINA).date().isoformat()})
                stopped.wait(10)
                continue
            if not projections:
                saved_plan = campaign.legacy._decode(_read_private_file(day / 'campaign-plan.json'))
                if saved_plan != plan:
                    raise ValueError('daily_plan_changed')
                rounds = campaign.validate_plan(plan)[0][2]
                for group in config['archive_groups']:
                    scoped = {**plan, 'store_ids': group['store_ids'],
                              'ordinary_query_limit': rounds * len(group['store_ids'])}
                    projections.append(projection.Projection(day, group['archive'], scoped,
                                            namespace='public-outbox-' + group['name']))
            deferred = []
            for group, item in zip(config['archive_groups'], projections):
                name=group['name']
                try:
                    # The collector can commit between groups. A timestamp from
                    # the beginning of this 28-group pass is already stale.
                    result = item.step(datetime.now(UTC))
                except PacketError as error:
                    if error.error_code != 'packet_future_record':
                        raise
                    # A row committed just after our read clock is safe to
                    # defer. Keep the cursor and packet validation unchanged.
                    deferred_counts[name]=deferred_counts.get(name,0)+1
                    deferred.append(name)
                    print(json.dumps({'event':'daily_public_deferred', 'group':name,
                        'error_code':'packet_future_record','cursor_advanced':False,
                        'consecutive_checks':deferred_counts[name]}),flush=True)
                    continue
                deferred_counts[name]=0
                if result:
                    print(json.dumps({'event': 'daily_public_saved', 'after_id': result['after_id'],
                                      'inserted_records': result['inserted_records']}), flush=True)
            persistent=any(deferred_counts[name]>=3 for name in deferred)
            publish_status(root, 'projection', {'state': 'needs_attention' if persistent else 'running',
                           'day': now.astimezone(CHINA).date().isoformat(),
                           'error_code':'packet_future_record' if persistent else None,
                           'deferred_groups':deferred,
                           'store_ids': configured_stores(config), 'cursors': [p.after for p in projections]})
            stopped.wait(10)
    finally:
        for item in projections:
            item.close()


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in {'collect', 'project'}:
        return 2
    root, mode = Path(sys.argv[1]), sys.argv[2]
    os.umask(0o077)
    if not root.is_absolute() or root.is_symlink() or root.stat().st_mode & 0o777 != 0o700:
        return 2
    lock = os.open(root / (mode + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        config = read_config(root)
        (run_collection if mode == 'collect' else run_projection)(root, config, stopped)
        return 0
    except Exception as error:
        code=getattr(error,'error_code',str(error))
        allowed={'packet_future_record','packet_database_changed','packet_database_error',
                 'cloud_public_checkpoint_limit','cloud_public_checkpoint_invalid',
                 'cloud_public_checkpoint_changed','cloud_public_pending_unconfirmed',
                 'cloud_public_commit_unconfirmed','cloud_public_delivery_ambiguous'}
        code=code if code in allowed else 'daily_service_failed'
        publish_status(root, mode, {'state': 'needs_attention', 'error_code': code})
        print(json.dumps({'event':'daily_service_failed','error_code':code,'sensitive_details_omitted':True}),flush=True)
        return 1
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
