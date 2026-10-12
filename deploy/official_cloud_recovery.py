"""One controlled cloud process restart; never request codes or query stores.

Run only in the fixed owner-approved trial. Keep the session volume, context,
ledger and collection task. A later natural renewal and saved source responses
must prove recovery; starting a service alone is not that proof.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import types

from sushiwait.auth import describe_authorization
from sushiwait.credentials import _read_private_file
from sushiwait.packets import _write_packet, encoded
from sushiwait.tasks import task_status

ROOT = Path('/home/ubuntu/sushiwait-official-cloud-e0462-20261012')
TRIAL = 'sushiwait-official-cloud-trial.service'
SUPPLIER = 'sushiwait-official-cloud-supplier.service'
SYNC = 'sushiwait-official-cloud-public-sync.service'
CONTROLLER_SHA = '650c2b1e9eb4c99db7da17df0930401cdaddddd8891a9c0c16f1ee2fd5a397eb'
START = datetime(2026, 10, 12, 7, 43, tzinfo=timezone.utc)
END = datetime(2026, 10, 12, 7, 46, tzinfo=timezone.utc)
TASK = 'collection-2026-10-12-600.json'


def command(args):
    result = subprocess.run(args, capture_output=True, timeout=130)
    if result.returncode:
        raise ValueError('recovery_service_failed')
    return result.stdout.decode()


def unit(name):
    text = command(['systemctl', 'show', name,
                    '--property=MainPID,ActiveState,SubState,Result'])
    values = dict(line.split('=', 1) for line in text.splitlines())
    return {'pid': int(values['MainPID']), 'active': values['ActiveState'],
            'sub': values['SubState'], 'result': values['Result']}


def container():
    args = ['sudo', '-n', 'docker', 'compose', '--env-file', str(ROOT/'supplier.env'),
            '--project-name', 'sushiwait-official-e0462', '-f',
            str(ROOT/'yyb_cloud_trial.compose.yaml'), 'ps', '--all', '--quiet']
    ids = command(args).split()
    if len(ids) != 1 or len(ids[0]) != 64 or any(c not in '0123456789abcdef' for c in ids[0]):
        raise ValueError('recovery_container_mismatch')
    # Explicit selected metadata, never environment, logs or account DB contents.
    value = json.loads(command(['sudo', '-n', 'docker', 'inspect', '--format',
        '{"id":{{json .Id}},"pid":{{json .State.Pid}},'
        '"started":{{json .State.StartedAt}},"running":{{json .State.Running}}}', ids[0]]))
    return value


def load_controller():
    path = ROOT/'official_cloud_controller.py'
    # This is our public source program, not a 16 KiB credential document.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                or not 0 < info.st_size <= 65536):
            raise ValueError('recovery_program_changed')
        body = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(body) != info.st_size or hashlib.sha256(body).hexdigest() != CONTROLLER_SHA:
        raise ValueError('recovery_program_changed')
    module = types.ModuleType('recovery_controller')
    module.__file__ = str(path)
    exec(compile(body, str(path), 'exec'), module.__dict__)
    return module


def snapshot(module):
    plan = module._decode(_read_private_file(ROOT/'trial-plan.json'))
    module.validate_plan(plan)
    if plan['store_ids'] != ['3014', '3004', '2009']:
        raise ValueError('recovery_plan_changed')
    # Existing checkpoints only; this inspection must not initialize a new ledger.
    if not (ROOT/'controller-state'/'checkpoint-000.json').exists():
        raise ValueError('recovery_ledger_missing')
    ledger = module.Ledger(ROOT/'controller-state', plan)
    context, digest = module.Controller(ROOT, plan, None, ledger).context()
    ledger.check_context(context.revision, digest)
    used, _ = module.query_usage(ROOT, plan['store_ids'])
    auth = describe_authorization(context.authorization)
    with sqlite3.connect((ROOT/'observations.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=1')
        latest = dict(db.execute("SELECT store_id,max(received_at) FROM samples WHERE ok=1 "
            "AND api_profile='miniapp_gateway' AND data_origin='live' GROUP BY store_id"))
    return {'at': datetime.now(timezone.utc).isoformat(),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'units': {name: unit(name) for name in (TRIAL, SUPPLIER, SYNC)},
            'container': container(), 'task': task_status(ROOT/TASK),
            'ledger_sha256': hashlib.sha256(encoded(ledger.value)).hexdigest(),
            'context_sha256': digest, 'revision': context.revision,
            'expiry': auth['declared_expires_at'], 'remaining_seconds': auth['remaining_seconds'],
            'ordinary_used': used, 'renewal_attempts': ledger.value['attempts'],
            'policy': module.polling_policy(ROOT), 'latest_success': latest}


def preflight(state, proof, *, now):
    task = state['task']
    units = state['units']
    if not START <= now < END:
        raise ValueError('recovery_window_closed')
    if (proof.get('original_reader_crossed_declared_expiry') is not True
            or type(proof.get('accepted_revision')) is not int
            or proof['accepted_revision'] < 2):
        raise ValueError('recovery_expiry_proof_missing')
    if (state['remaining_seconds'] <= 630 or state['revision'] < proof['accepted_revision']
            or state['ordinary_used'] >= 396
            or state['policy'] != {'interval_seconds': 600, 'ordinary_query_limit': 396}
            or task['interval_seconds'] != 600 or task['credential_revision'] != state['revision']
            or task['pending_attempt'] or task['failed_slots'] or task['uncertain_slots']
            or task['completed_slots'] < 6 or task['state'] not in {'ready', 'running'}
            or task['stop_reason'] is not None):
        raise ValueError('recovery_checkpoint_unsafe')
    for name in (TRIAL, SYNC):
        if (units[name]['active'] != 'active' or units[name]['sub'] != 'running'
                or units[name]['pid'] <= 0 or units[name]['result'] != 'success'):
            raise ValueError('recovery_process_not_running')
    if (units[SUPPLIER]['active'] != 'active' or units[SUPPLIER]['sub'] != 'exited'
            or units[SUPPLIER]['result'] != 'success' or not state['container']['running']
            or state['container']['pid'] <= 0):
        raise ValueError('recovery_supplier_not_running')


def unchanged(before, after):
    for key in ('ledger_sha256', 'context_sha256', 'revision', 'ordinary_used', 'policy',
                'renewal_attempts'):
        if before[key] != after[key]:
            raise ValueError('recovery_checkpoint_changed')
    for key in ('completed_slots', 'target_slots', 'target_rounds', 'failed_slots',
                'uncertain_slots', 'pending_attempt', 'credential_revision'):
        if before['task'][key] != after['task'][key]:
            raise ValueError('recovery_checkpoint_changed')
    if before['units'][SYNC] != after['units'][SYNC]:
        raise ValueError('recovery_sync_changed')


def publish(value, name):
    result = _write_packet(encoded(value), ROOT/name)
    if not all(result[k] is True for k in ('committed', 'durability_confirmed')):
        raise ValueError('recovery_publication_unconfirmed')


def stop_reader(before):
    if unit(TRIAL) != before['units'][TRIAL]:
        raise ValueError('recovery_reader_changed')
    # Signal only the selected same-owner controller. Its existing handler
    # first sets the stop event, then closes its child; no simultaneous group
    # kill that could race a child failure into the persistent halt ledger.
    os.kill(before['units'][TRIAL]['pid'], signal.SIGTERM)
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        if unit(TRIAL)['pid'] == 0:
            return
        time.sleep(0.25)
    raise ValueError('recovery_reader_not_stopped')


def restart(module, before):
    # Baseline is exclusive. A repeated call cannot stop a recovered writer.
    publish(before, 'recovery-before.json')
    stop_reader(before)
    stopped = snapshot(module)
    unchanged(before, stopped)
    if stopped['units'][TRIAL]['pid'] != 0:
        raise ValueError('recovery_reader_not_stopped')
    command(['sudo', '-n', 'systemctl', 'stop', SUPPLIER])
    cold = snapshot(module)
    unchanged(before, cold)
    if (cold['units'][SUPPLIER]['active'] != 'inactive'
            or cold['container']['running'] or cold['container']['pid'] != 0):
        raise ValueError('recovery_supplier_not_stopped')
    publish(cold, 'recovery-cold.json')
    command(['sudo', '-n', 'systemctl', 'start', SUPPLIER])
    source = container()
    if (not source['running'] or source['pid'] <= 0
            or source['pid'] == before['container']['pid']
            or source['started'] == before['container']['started']
            or source['id'] != before['container']['id']):
        raise ValueError('recovery_supplier_restart_unproven')
    command(['sudo', '-n', 'systemctl', 'start', TRIAL])
    after = snapshot(module)
    unchanged(before, after)
    if (after['units'][TRIAL]['active'] != 'active'
            or after['units'][TRIAL]['sub'] != 'running'
            or after['units'][TRIAL]['pid'] <= 0
            or after['units'][TRIAL]['pid'] == before['units'][TRIAL]['pid']):
        raise ValueError('recovery_reader_restart_unproven')
    publish(after, 'recovery-after.json')
    publish({'at': after['at'], 'before_pid': before['units'][TRIAL]['pid'],
             'after_pid': after['units'][TRIAL]['pid'], 'supplier_process_changed': True,
             'checkpoints_preserved': True, 'source_queries_added_by_probe': 0,
             'natural_renewal_and_new_records_still_required': True}, 'recovery-started.json')


def prove_recovery(before, cold, after, current):
    """Require a persisted supplier to issue new context after its process died."""
    if (cold['container']['running'] or cold['container']['pid'] != 0
            or cold['units'][TRIAL]['pid'] != 0
            or after['container']['pid'] == before['container']['pid']
            or after['container']['started'] == before['container']['started']
            or after['units'][TRIAL]['pid'] == before['units'][TRIAL]['pid']
            or current['container'] != after['container']
            or current['units'][TRIAL] != after['units'][TRIAL]):
        raise ValueError('recovery_cold_process_unproven')
    unchanged(before, cold)
    unchanged(before, after)
    expiry = datetime.fromisoformat(before['expiry'].replace('Z', '+00:00'))
    task = current['task']
    if (datetime.fromisoformat(current['at']) <= expiry
            or current['revision'] <= before['revision']
            or current['renewal_attempts'] <= before['renewal_attempts']
            or current['context_sha256'] == before['context_sha256']
            or current['remaining_seconds'] <= 90
            or current['policy'] != before['policy']
            or current['ordinary_used'] <= before['ordinary_used']
            or task['target_slots'] != before['task']['target_slots']
            or task['target_rounds'] != before['task']['target_rounds']
            or task['credential_revision'] != current['revision']
            or task['completed_slots'] <= before['task']['completed_slots']
            or task['pending_attempt'] or task['failed_slots'] or task['uncertain_slots']):
        raise ValueError('recovery_natural_renewal_unproven')
    for store in ('3014', '3004', '2009'):
        at = current['latest_success'].get(store)
        if at is None or datetime.fromisoformat(at.replace('Z', '+00:00')) <= expiry:
            raise ValueError('recovery_new_source_records_missing')
    return {'at': current['at'], 'cloud_process_cold_recovery_proven': True,
            'accepted_revision': current['revision'], 'renewal_attempts': current['renewal_attempts'],
            'previous_declared_expiry': before['expiry'],
            'latest_success': current['latest_success'], 'checkpoint_budget_preserved': True,
            'mac_actual_offline_proven': False, 'host_reboot_proven': False}


def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in {'restart', 'audit'}:
        return 2
    try:
        module = load_controller()
        state = snapshot(module)
        if sys.argv[1] == 'audit':
            saved = [json.loads(_read_private_file(ROOT/name)) for name in
                     ('recovery-before.json', 'recovery-cold.json', 'recovery-after.json')]
            proof = prove_recovery(*saved, state)
            publish(proof, 'recovery-proof.json')
            print(json.dumps(proof), flush=True)
            return 0
        proof = json.loads(_read_private_file(ROOT/'cadence-expiry-proof.json'))
        preflight(state, proof, now=datetime.now(timezone.utc))
        restart(module, state)
        print('{"cloud_processes_restarted":true,"checkpoints_preserved":true,'
              '"renewal_and_source_recovery_proven":false}', flush=True)
        return 0
    except Exception as error:
        allowed = {'recovery_service_failed', 'recovery_container_mismatch',
            'recovery_program_changed', 'recovery_plan_changed', 'recovery_ledger_missing',
            'recovery_window_closed', 'recovery_expiry_proof_missing',
            'recovery_checkpoint_unsafe', 'recovery_process_not_running',
            'recovery_supplier_not_running', 'recovery_checkpoint_changed',
            'recovery_reader_changed',
            'recovery_sync_changed', 'recovery_publication_unconfirmed',
            'recovery_reader_not_stopped', 'recovery_supplier_not_stopped',
            'recovery_supplier_restart_unproven', 'recovery_reader_restart_unproven',
            'recovery_cold_process_unproven', 'recovery_natural_renewal_unproven',
            'recovery_new_source_records_missing'}
        code = str(error) if str(error) in allowed else 'recovery_probe_failed'
        print(json.dumps({'cloud_processes_restarted': False, 'error_code': code,
                          'automatic_retry': False}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
