"""One finite cadence switch, after the original cloud reader crosses expiry.

This program makes no supplier or upstream calls. It preserves the immutable
plan, task histories and renewal ledger; failed proof leaves the old reader.
Run baseline before the first expiry, then switch once within the fixed window.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sushiwait.auth import describe_authorization
from sushiwait.credentials import _read_private_file, read_credentials_file
from sushiwait.packets import _write_packet, encoded
from sushiwait.tasks import task_status

ROOT = Path('/home/ubuntu/sushiwait-official-cloud-e0462-20261012')
UNIT = 'sushiwait-official-cloud-trial.service'
OLD_SHA = '94ebc6c2bfee224cb02572c221bb065f442a33d30f9c8a48d6012d791bee64f5'
NEW_SHA = '650c2b1e9eb4c99db7da17df0930401cdaddddd8891a9c0c16f1ee2fd5a397eb'
STORES = ('3014','3004','2009')
START = datetime(2026,10,12,7,0,tzinfo=timezone.utc)
END = datetime(2026,10,12,7,5,tzinfo=timezone.utc)


def command(args):
    result = subprocess.run(args,capture_output=True,timeout=130)
    if result.returncode:
        raise ValueError('cadence_service_failed')
    return result.stdout.decode()


def process(pid):
    # Only the already selected project parent and its one selected child.
    fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
    if fields[0] in {'Z','X'}:
        raise ValueError('cadence_process_missing')
    return {'pid':pid,'start_ticks':int(fields[19]),'parent_pid':int(fields[1])}


def processes():
    pid = int(command(['systemctl','show',UNIT,'--property=MainPID','--value']).strip())
    parent = process(pid)
    children = (Path('/proc')/str(pid)/'task'/str(pid)/'children').read_text().split()
    if len(children) != 1:
        raise ValueError('cadence_reader_missing')
    reader = process(int(children[0]))
    args = (Path('/proc')/str(reader['pid'])/'cmdline').read_bytes().split(b'\0')
    if len(args)<4 or args[3] != str(ROOT/'official_business_collect.py').encode():
        raise ValueError('cadence_reader_mismatch')
    return {'parent':parent,'reader':reader}


def snapshot():
    credentials = read_credentials_file(ROOT/'query-context.json',api_profile='miniapp_gateway')
    auth = describe_authorization(credentials.authorization)
    status = task_status(ROOT/'collection-2026-10-12.json')
    with sqlite3.connect((ROOT/'observations.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=1')
        groups = db.execute("SELECT store_id,max(received_at) FROM samples WHERE ok=1 "
            "AND api_profile='miniapp_gateway' AND data_origin='live' GROUP BY store_id").fetchall()
    return {'at':datetime.now(timezone.utc).isoformat(),'processes':processes(),
            'revision':credentials.revision,'expiry':auth['declared_expires_at'],
            'remaining_seconds':auth['remaining_seconds'],'task':status,
            'latest_success':dict(groups)}


def prove(baseline,current, *, now):
    expiry = datetime.fromisoformat(baseline['expiry'].replace('Z','+00:00'))
    if not START<=now<END or not now>expiry:
        raise ValueError('cadence_proof_timing')
    if current['processes'] != baseline['processes']:
        raise ValueError('cadence_original_reader_changed')
    task = current['task']
    if (baseline['revision'] != 1 or current['revision'] <= baseline['revision']
            or current['remaining_seconds'] <= 90
            or task['credential_revision'] != current['revision']
            or task['interval_seconds'] != 900 or task['pending_attempt']
            or task['failed_slots'] or task['uncertain_slots']
            or task['completed_slots'] <= baseline['task']['completed_slots']
            or any(datetime.fromisoformat(current['latest_success'].get(s,'').replace('Z','+00:00'))
                   <= expiry for s in STORES)):
        raise ValueError('cadence_expiry_proof_incomplete')
    return {'original_reader_crossed_declared_expiry':True,
        'reader_pid':current['processes']['reader']['pid'],
        'previous_declared_expiry':baseline['expiry'],'accepted_revision':current['revision'],
        'latest_success':current['latest_success'],'at':now.isoformat()}


def publish(value,name):
    result = _write_packet(encoded(value),ROOT/name)
    if not all(result[k] is True for k in ('committed','durability_confirmed')):
        raise ValueError('cadence_publication_unconfirmed')


def main():
    os.umask(0o077)
    if len(sys.argv)!=2 or sys.argv[1] not in {'baseline','switch'}:
        return 2
    try:
        if sys.argv[1]=='baseline':
            current=snapshot()
            if current['revision']!=1 or current['remaining_seconds']<=120:
                raise ValueError('cadence_baseline_too_late')
            publish(current,'cadence-baseline.json')
            print(json.dumps({'baseline_saved':True,'processes':current['processes'],
                'expiry':current['expiry'],'completed_slots':current['task']['completed_slots']}),flush=True)
            return 0
        baseline=json.loads(_read_private_file(ROOT/'cadence-baseline.json'))
        proof=prove(baseline,snapshot(),now=datetime.now(timezone.utc))
        target=ROOT/'official_cloud_controller.py'
        pending=ROOT/'official_cloud_controller-600.py.pending'
        if (hashlib.sha256(target.read_bytes()).hexdigest()!=OLD_SHA
                or hashlib.sha256(pending.read_bytes()).hexdigest()!=NEW_SHA
                or (ROOT/'polling-policy.json').exists()):
            raise ValueError('cadence_program_changed')
        # .pending is deliberately not inferred as executable Python by suffix.
        from importlib.machinery import SourceFileLoader
        spec=importlib.util.spec_from_loader('cadence_candidate',SourceFileLoader('cadence_candidate',str(pending)))
        candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)
        plan=candidate._decode(_read_private_file(ROOT/'trial-plan.json'))
        candidate.validate_plan(plan)
        ledger=candidate.Ledger(ROOT/'controller-state',plan)
        current,digest=candidate.Controller(ROOT,plan,None,ledger).context()
        ledger.check_context(current.revision,digest)
        used,_=candidate.query_usage(ROOT,plan['store_ids'])
        if used>=396:
            raise ValueError('cadence_budget_exhausted')
        publish(proof,'cadence-expiry-proof.json')
        # All proof and program checks happen before stopping the old reader.
        command(['sudo','-n','systemctl','stop',UNIT])
        if int(command(['systemctl','show',UNIT,'--property=MainPID','--value']).strip()) != 0:
            raise ValueError('cadence_reader_not_stopped')
        final=task_status(ROOT/'collection-2026-10-12.json')
        if final['pending_attempt'] or final['failed_slots'] or final['uncertain_slots']:
            raise ValueError('cadence_stop_uncertain')
        candidate.Ledger(ROOT/'controller-state',plan)
        # Backup is exclusive and durable; histories and context are untouched.
        publish_bytes=target.read_bytes()
        backup=_write_packet(publish_bytes,ROOT/'official_cloud_controller-900.py.backup')
        if not backup['durability_confirmed']:
            raise ValueError('cadence_backup_unconfirmed')
        publish({'schema_version':1,'interval_seconds':600,'ordinary_query_limit':396},'polling-policy.json')
        os.replace(pending,target)
        directory=os.open(ROOT,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        command(['sudo','-n','systemctl','start',UNIT])
        publish({'at':datetime.now(timezone.utc).isoformat(),'interval_seconds':600,
            'ordinary_query_limit':396,'spent_before_switch':used,'expiry_proof_saved':True},'cadence-switch-receipt.json')
        print('{"cadence_switched":true,"interval_seconds":600,"expiry_proof_saved":true}',flush=True)
        return 0
    except Exception as error:
        allowed={'cadence_service_failed','cadence_process_missing','cadence_reader_missing',
            'cadence_reader_mismatch','cadence_proof_timing','cadence_original_reader_changed',
            'cadence_expiry_proof_incomplete','cadence_publication_unconfirmed','cadence_baseline_too_late',
            'cadence_program_changed','cadence_budget_exhausted','cadence_reader_not_stopped',
            'cadence_stop_uncertain','cadence_backup_unconfirmed'}
        code=str(error) if str(error) in allowed else 'cadence_switch_failed'
        print(json.dumps({'cadence_switched':False,'error_code':code,'automatic_retry':False}),flush=True)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
