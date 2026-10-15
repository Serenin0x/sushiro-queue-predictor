"""Append-only owner reauthorization receipts and collection recovery epochs.

No login or HTTP calls occur here. A fresh, signed confirmation is bound to the
exact stopped incident. Old task/checkpoint bytes and all spent attempts remain.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path
import re

from sushiwait.credentials import _read_private_file
from sushiwait.packets import _write_packet, encoded

spec = importlib.util.spec_from_file_location('recovery_campaign',
    Path(__file__).with_name('official_cloud_campaign.py'))
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)
UTC = timezone.utc


def read(path):
    return campaign.legacy._decode(_read_private_file(path))


def put_once(path, value):
    body = encoded(value)
    if os.path.lexists(path):
        if _read_private_file(path) != body:
            raise ValueError('recovery_existing_file_changed')
    elif not _write_packet(body, path)['durability_confirmed']:
        raise ValueError('recovery_write_unconfirmed')


def sequenced(directory, pattern):
    if not directory.exists():
        return []
    if directory.is_symlink() or directory.stat().st_mode & 0o777 != 0o700:
        raise ValueError('recovery_directory_unsafe')
    files = sorted(directory.glob('*.json'))
    if len(files) > 999 or any(p.name != pattern.format(i + 1) for i, p in enumerate(files)):
        raise ValueError('recovery_sequence_invalid')
    return files


def latest_block(root):
    root = Path(root)
    files = sequenced(root / 'blocks', 'blocked-{:03}.json')
    path = files[-1] if files else root / 'collection-blocked.json'
    if not os.path.lexists(path):
        return None
    body = _read_private_file(path)
    value = campaign.legacy._decode(body)
    if (set(value) != {'schema_version', 'at', 'day', 'error_code', 'automatic_retry'}
            or value['schema_version'] != 1 or value['automatic_retry'] is not False
            or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', value['day'])):
        raise ValueError('recovery_block_invalid')
    return {'sha256': hashlib.sha256(body).hexdigest(), 'value': value}


def active_block(root):
    block = latest_block(root)
    if block is None:
        return None
    clearance = Path(root) / 'recovery-clearances' / (block['sha256'] + '.json')
    if not os.path.lexists(clearance):
        return block
    value = read(clearance)
    if (value.get('schema_version') != 1 or value.get('block_sha256') != block['sha256']
            or set(value) != {'schema_version', 'block_sha256', 'receipt_sha256',
                              'selection_file', 'selection_sha256'}):
        raise ValueError('recovery_clearance_invalid')
    selection = Path(root) / value['selection_file']
    if (selection.parent.parent != Path(root)
            or not re.fullmatch(r'five-stores-20\d{6}', selection.parent.name)
            or not re.fullmatch(r'recovery-\d{3}\.json', selection.name)
            or hashlib.sha256(_read_private_file(selection)).hexdigest() != value['selection_sha256']):
        raise ValueError('recovery_clearance_changed')
    return None


def latch(root, code, day, now):
    root = Path(root)
    if active_block(root) is not None:
        return
    value = {'schema_version': 1, 'at': now.isoformat(), 'day': day,
             'error_code': code, 'automatic_retry': False}
    if latest_block(root) is None:
        path = root / 'collection-blocked.json'
    else:
        directory = root / 'blocks'
        directory.mkdir(mode=0o700, exist_ok=True)
        files = sequenced(directory, 'blocked-{:03}.json')
        path = directory / f'blocked-{len(files)+1:03}.json'
    put_once(path, value)


def requires_scan(root, block):
    if not block or block['value']['error_code'] != 'cloud_refresh_failed':
        return False
    day = block['value']['day'].replace('-', '')
    directory = Path(root) / ('five-stores-' + day) / 'refresh-diagnostics'
    if not directory.exists():
        return False
    paths = sorted(directory.glob('attempt-*.json'))
    if not paths or len(paths) > 14:
        return False
    value = read(paths[-1])
    return value.get('phase') == 'supplier' and value.get('error_code') == 'yyb_reauthorization_required'


def selections(day):
    paths = sorted(Path(day).glob('recovery-*.json'))
    if (len(paths) > 14
            or any(p.name != f'recovery-{i+1:03}.json' for i, p in enumerate(paths))):
        raise ValueError('recovery_sequence_invalid')
    return paths


def selected(day, plan):
    day = Path(day)
    paths = selections(day)
    if not paths:
        return day / 'controller-state', campaign.task_path(day,
            campaign.validate_plan(plan)[0][0], plan.get('cadence'))
    previous = None
    parent_ledger = 'controller-state'
    for index, path in enumerate(paths, 1):
        value = read(path)
        fields = {'schema_version', 'index', 'plan_sha256', 'parent_selection_sha256',
                  'parent_checkpoint_sha256', 'parent_ledger', 'parent_checkpoint',
                  'ledger', 'task', 'receipt_sha256', 'block_sha256',
                  'inherited_attempts', 'inherited_revision'}
        if (set(value) != fields or value.get('schema_version') != 1 or value.get('index') != index
                or value.get('plan_sha256') != hashlib.sha256(encoded(plan)).hexdigest()
                or value.get('parent_selection_sha256') != previous
                or value.get('parent_ledger') != parent_ledger
                or type(value.get('parent_checkpoint')) is not str
                or not re.fullmatch(r'checkpoint-\d{3}\.json', value['parent_checkpoint'])
                or value.get('ledger') != f'controller-state-reauth-{index:03}'
                or value.get('task') != f'collection-{plan["plan_id"][-8:]}-reauth-{index:03}.json'):
            raise ValueError('recovery_selection_invalid')
        for field in ('receipt_sha256', 'block_sha256', 'parent_checkpoint_sha256'):
            if type(value[field]) is not str or not re.fullmatch('[0-9a-f]{64}', value[field]):
                raise ValueError('recovery_selection_invalid')
        parent = day / parent_ledger / value['parent_checkpoint']
        if (hashlib.sha256(_read_private_file(parent)).hexdigest() != value['parent_checkpoint_sha256']
                or parent != sorted((day / parent_ledger).glob('checkpoint-*.json'))[-1]):
            raise ValueError('recovery_parent_changed')
        old, seed = read(parent), read(day / value['ledger'] / 'checkpoint-000.json')
        expected = {**old, 'pending': False, 'halted': False, 'error_code': None}
        if (seed != expected or value['inherited_attempts'] != old['attempts']
                or value['inherited_revision'] != old['highest_revision']):
            raise ValueError('recovery_seed_changed')
        parent_ledger = value['ledger']
        previous = hashlib.sha256(_read_private_file(path)).hexdigest()
    return day / value['ledger'], day / value['task']


def resume_not_before(root):
    """A confirmation after closing can safely select tomorrow's finite plan."""
    block = latest_block(root)
    if block is None or active_block(root) is not None:
        return None
    clearance = read(Path(root) / 'recovery-clearances' / (block['sha256'] + '.json'))
    day = (Path(root) / clearance['selection_file']).parent
    plan = read(day / 'campaign-plan.json')
    selected(day, plan)
    return campaign.validate_plan(plan)[0][0]


def task_usage(day, plan):
    """Include every terminal, pending and active task in the same day budget."""
    paths = [campaign.task_path(day, campaign.validate_plan(plan)[0][0], plan.get('cadence'))]
    for selection in selections(day):
        paths.append(Path(day) / read(selection)['task'])
    used, states = 0, {}
    for path in paths:
        if not os.path.lexists(path):
            continue
        status = campaign.legacy.task_status(path)
        if (status['store_ids'] != plan['store_ids'] or status['api_profile'] != 'miniapp_gateway'
                or status['interval_seconds'] != 600
                or status.get('cadence_policy', {}).get('id') != plan.get('cadence')
                or status['target_rounds'] > campaign.validate_plan(plan)[0][2]):
            raise ValueError('recovery_task_scope_invalid')
        used += status['completed_slots'] + int(status['pending_attempt'])
        states[path] = status
    if used > plan['ordinary_query_limit']:
        raise ValueError('recovery_budget_exhausted')
    return used, states


def window_budget(day, plan, start):
    _, task = selected(day, plan)
    used, states = task_usage(day, plan)
    remaining = plan['ordinary_query_limit'] - used
    previous = states.get(task)
    if previous:
        if previous['target_slots'] - previous['completed_slots'] - int(previous['pending_attempt']) > remaining:
            raise ValueError('recovery_budget_exhausted')
        return task, previous['target_rounds'], True, previous
    return task, min(campaign.validate_plan(plan)[0][2], remaining // len(plan['store_ids'])), False, None


def signed_receipt(payload, key):
    return {'payload': payload, 'signature': hmac.new(key, encoded(payload), hashlib.sha256).hexdigest()}


def validate_receipt(value, block, key, now):
    if type(value) is not dict or set(value) != {'payload', 'signature'}:
        raise ValueError('recovery_receipt_invalid')
    payload = value['payload']
    keys = {'schema_version', 'receipt_id', 'account_ref', 'block_sha256', 'confirmed_at'}
    if (type(payload) is not dict or set(payload) != keys or payload['schema_version'] != 1
            or payload['account_ref'] != '1' or payload['block_sha256'] != block['sha256']
            or type(payload['receipt_id']) is not str or not re.fullmatch('[0-9a-f]{32}', payload['receipt_id'])
            or type(value['signature']) is not str
            or not hmac.compare_digest(value['signature'], hmac.new(key, encoded(payload), hashlib.sha256).hexdigest())):
        raise ValueError('recovery_receipt_invalid')
    at = datetime.fromisoformat(payload['confirmed_at'])
    stopped_at = datetime.fromisoformat(block['value']['at'])
    if at.utcoffset() is None or not stopped_at <= at <= now or (now-at).total_seconds() > 1800:
        raise ValueError('recovery_receipt_expired')
    return payload


def prepare_recovery(root, day, plan, value, key, now):
    """Select one new epoch after one fresh owner confirmation; never retry it."""
    root, day = Path(root), Path(day)
    block = active_block(root)
    if block is None:
        return False
    payload = validate_receipt(value, block, key, now)
    receipt_sha = hashlib.sha256(encoded(value)).hexdigest()
    paths = selections(day)
    matching = [p for p in paths if read(p).get('receipt_sha256') == receipt_sha]
    if matching:
        selection_path = matching[0]
    else:
        selected_ledger, _ = selected(day, plan)
        selected_ledger.mkdir(mode=0o700, exist_ok=True)
        try:
            parent = campaign.CampaignLedger(selected_ledger, plan)
        except campaign.TrialError as error:
            if str(error) not in {'cloud_refresh_outcome_unknown', 'cloud_trial_halted'}:
                raise
            # Ledger validates all fields before reporting a terminal state.
            # Inspect that validated instance without resetting its history.
            parent = campaign.CampaignLedger.__new__(campaign.CampaignLedger)
            parent.checkpoint_limit = 2 * plan['renewal_limit'] + len(plan['windows']) + 2
            try:
                campaign.legacy.Ledger.__init__(parent, selected_ledger, plan)
            except campaign.TrialError as terminal:
                if str(terminal) not in {'cloud_refresh_outcome_unknown', 'cloud_trial_halted'}:
                    raise
        if parent.value['attempts'] >= plan['renewal_limit'] or parent.value['finished_windows']:
            raise ValueError('recovery_budget_exhausted')
        index = len(paths) + 1
        if index > 14:
            raise ValueError('recovery_epoch_limit')
        parent_path = sorted(selected_ledger.glob('checkpoint-*.json'))[-1]
        parent_body = _read_private_file(parent_path)
        ledger_dir = f'controller-state-reauth-{index:03}'
        successor = day / ledger_dir
        successor.mkdir(mode=0o700, exist_ok=True)
        seed = {**parent.value, 'pending': False, 'halted': False, 'error_code': None}
        put_once(successor / 'checkpoint-000.json', seed)
        selection_path = day / f'recovery-{index:03}.json'
        manifest = {'schema_version': 1, 'index': index,
            'plan_sha256': hashlib.sha256(encoded(plan)).hexdigest(),
            'parent_selection_sha256': hashlib.sha256(_read_private_file(paths[-1])).hexdigest() if paths else None,
            'parent_checkpoint_sha256': hashlib.sha256(parent_body).hexdigest(),
            'parent_ledger': selected_ledger.name, 'parent_checkpoint': parent_path.name,
            'ledger': ledger_dir, 'task': f'collection-{plan["plan_id"][-8:]}-reauth-{index:03}.json',
            'receipt_sha256': receipt_sha, 'block_sha256': block['sha256'],
            'inherited_attempts': parent.value['attempts'], 'inherited_revision': parent.value['highest_revision']}
        if _read_private_file(parent_path) != parent_body:
            raise ValueError('recovery_parent_changed')
        put_once(selection_path, manifest)
    clearances = root / 'recovery-clearances'
    clearances.mkdir(mode=0o700, exist_ok=True)
    put_once(clearances / (block['sha256'] + '.json'), {
        'schema_version': 1, 'block_sha256': block['sha256'], 'receipt_sha256': receipt_sha,
        'selection_file': str(selection_path.relative_to(root)),
        'selection_sha256': hashlib.sha256(_read_private_file(selection_path)).hexdigest()})
    return True
