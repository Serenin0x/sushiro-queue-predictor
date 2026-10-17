"""Explicit operator recovery of one recorded fleet GET timeout.

No network or credential access. Preserve the stopped state and raw failure;
only add the missing store cooldown. A clearance is bound to this exact block,
plan, state and database row, and never clears a later authentication incident.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from pathlib import Path

from sushiwait.credentials import _read_private_file
from sushiwait.packets import encoded
from sushiwait.storage import SnapshotStore


def digest(body):
    return hashlib.sha256(body).hexdigest()


def paths(root, block):
    day = Path(root)/('five-cities-'+block['value']['day'].replace('-', ''))
    stem = 'fleet-timeout-'+block['sha256']
    return (day, day/(stem+'-before.json'), day/(stem+'-after.json'),
            Path(root)/'fleet-timeout-clearances'/(block['sha256']+'.json'))


def checked(root, block, recovery, value):
    day, before_path, after_path, _ = paths(root, block)
    if (set(value) != {'schema_version','policy','block_sha256','at','failure_id',
                       'before_sha256','after_sha256','failure_sha256'}
            or value['schema_version'] != 1 or value['policy'] != 'recorded-get-timeout-v1'
            or value['block_sha256'] != block['sha256']
            or block['value']['error_code'] != 'daily_collection_failed'
            or type(value['failure_id']) is not int or value['failure_id'] < 1):
        raise ValueError('timeout_clearance_invalid')
    before, after = recovery.read(before_path), recovery.read(after_path)
    if (digest(_read_private_file(before_path)) != value['before_sha256']
            or digest(_read_private_file(after_path)) != value['after_sha256']):
        raise ValueError('timeout_clearance_changed')
    plan = recovery.read(day/'campaign-plan.json')
    start, end, _ = recovery.campaign.validate_plan(plan)[0]
    now = datetime.fromisoformat(value['at'])
    if now.utcoffset() is None or not start <= now < end:
        raise ValueError('timeout_recovery_window_invalid')
    with SnapshotStore(day/'observations.sqlite', read_only=True) as db:
        rows = db.db.execute('SELECT id,store_id,received_at,ok,payload_json FROM samples '
            'WHERE id<=? ORDER BY id', (value['failure_id'],)).fetchall()
    if (not rows or rows[-1][0] != value['failure_id']
            or digest(rows[-1][4].encode()) != value['failure_sha256']):
        raise ValueError('timeout_failure_changed')
    row = rows[-1]
    import json
    failure = json.loads(row[4])
    status = failure.get('http_status')
    if (row[3] != 0 or failure.get('failure_phase') != 'request'
            or failure.get('error_code') != 'timeout'
            or not (status is None or type(status) is int and 200 <= status < 300)
            or failure.get('data_origin') != 'live'
            or failure.get('api_profile') != 'miniapp_gateway'):
        raise ValueError('timeout_failure_not_retryable')
    if (before['plan_sha256'] != digest(encoded(plan)) or before['pending'] is not None
            or before['policy'] != 'store-isolation-v1' or before['schema_version'] != 1
            or before['issued'] != len(rows) or before['successful'] != len(rows)-1
            or before['failed'] != 1 or sum(r[3] for r in rows) != len(rows)-1
            or before['last_progress_at'] != row[2]
            or before['rate_limit_until'] is not None or before['rate_limit_streak'] != 0
            or set(before['stores']) != set(plan['store_ids'])):
        raise ValueError('timeout_accounting_invalid')
    for store, item in before['stores'].items():
        if (item[1] != sum(r[1] == store for r in rows) or item[2] != 0):
            raise ValueError('timeout_accounting_invalid')
    expected = deepcopy(before)
    expected['stores'][row[1]][0] = min(end, now+timedelta(minutes=30)).isoformat()
    expected['stores'][row[1]][2] = 1
    if after != expected:
        raise ValueError('timeout_recovery_changed_accounting')
    return day, before, after


def verify(root, block, recovery):
    _, _, _, receipt = paths(root, block)
    checked(root, block, recovery, recovery.read(receipt))
    return True


def prepare(root, recovery, now):
    """Run explicitly with collector stopped and its lock held by the caller."""
    block = recovery.latest_block(root)
    if block is None:
        raise ValueError('timeout_block_missing')
    day, before_path, after_path, receipt = paths(root, block)
    if receipt.exists():
        verify(root, block, recovery)
        return {'already_prepared': True}
    plan = recovery.read(day/'campaign-plan.json')
    state, _ = recovery.selected(day, plan)
    ledger = recovery.campaign.CampaignLedger(state, plan)
    if ledger.value['finished_windows']:
        raise ValueError('timeout_window_finished')
    current = _read_private_file(day/'fleet-state.json')
    before_body = _read_private_file(before_path) if before_path.exists() else current
    before = recovery.campaign.legacy._decode(before_body)
    with SnapshotStore(day/'observations.sqlite', read_only=True) as db:
        row = db.db.execute('SELECT id,store_id,payload_json FROM samples ORDER BY id DESC LIMIT 1').fetchone()
    if row is None or row[1] not in before['stores']:
        raise ValueError('timeout_failure_missing')
    after = deepcopy(before)
    _, end, _ = recovery.campaign.validate_plan(plan)[0]
    after['stores'][row[1]][0] = min(end, now+timedelta(minutes=30)).isoformat()
    after['stores'][row[1]][2] = 1
    # Immutable preparation survives interruption before the final clearance.
    preparation = day/('fleet-timeout-'+block['sha256']+'-preparation.json')
    value = {'schema_version':1,'policy':'recorded-get-timeout-v1','block_sha256':block['sha256'],
             'at':now.isoformat(),'failure_id':row[0], 'before_sha256':digest(before_body),
             'after_sha256':digest(encoded(after)), 'failure_sha256':digest(row[2].encode())}
    if preparation.exists():
        value = recovery.read(preparation)
        after = recovery.read(after_path)
    recovery.put_once(before_path, before)
    recovery.put_once(after_path, after)
    recovery.put_once(preparation, value)
    checked(root, block, recovery, value)
    if digest(current) not in {value['before_sha256'], value['after_sha256']}:
        raise ValueError('timeout_active_state_changed')
    import importlib.util
    spec = importlib.util.spec_from_file_location('timeout_fleet',Path(__file__).with_name('official_fleet_collect.py'))
    fleet = importlib.util.module_from_spec(spec); spec.loader.exec_module(fleet)
    fleet.save(day/'fleet-state.json', after)
    receipt.parent.mkdir(mode=0o700, exist_ok=True)
    recovery.put_once(receipt, value)
    return {'prepared':True,'preserved_attempts':before['issued'],
            'preserved_failures':before['failed'],'isolated_store':row[1]}
