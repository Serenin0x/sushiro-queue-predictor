"""Finite cloud trial: private cold start, refresh and checkpointed collection.

Prepared deployment entry point, not enabled by importing this file. The owner
must approve the private source and supplier session first. No package reader,
account enumeration, fallback identity or unrestricted retry is provided.
"""
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

from sushiwait.auth import describe_authorization
from sushiwait.credentials import _read_private_file, read_credentials_file
from sushiwait.initialization import initialize_from_yyb, initialize_for_target_from_yyb
from sushiwait.materialsource import prepare_material_source
from sushiwait.packets import encoded, _write_packet
from sushiwait.sessionverification import prepare_bootstrap_verifier, prepare_verifier
from sushiwait.tasks import task_status

UTC = timezone.utc
CHINA = timezone(timedelta(hours=8))
STORES = {'3014', '3004', '2009'}


class TrialError(ValueError):
    pass


def utc_now():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat()


def validate_plan(value):
    keys = {'schema_version', 'app_id', 'source_version', 'package_sha256',
            'store_ids', 'windows', 'renewal_limit'}
    try:
        if (type(value) is not dict or set(value) != keys
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['app_id']) is not str or not re.fullmatch('wx[0-9a-f]{16}', value['app_id'])
                or type(value['source_version']) is not int or value['source_version'] != 181
                or type(value['package_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['package_sha256'])
                or type(value['renewal_limit']) is not int or not 1 <= value['renewal_limit'] <= 42
                or type(value['store_ids']) is not list or not 1 <= len(value['store_ids']) <= 3
                or len(set(value['store_ids'])) != len(value['store_ids'])
                or not all(type(s) is str and s in STORES for s in value['store_ids'])
                or type(value['windows']) is not list or not 1 <= len(value['windows']) <= 3):
            raise ValueError
        result = []
        for window in value['windows']:
            if type(window) is not dict or set(window) != {'start', 'end'}:
                raise ValueError
            start, end = (datetime.fromisoformat(window[k]) for k in ('start','end'))
            if (start.utcoffset() != timedelta(hours=8) or end.utcoffset() != timedelta(hours=8)
                    or start.date() != end.date()
                    or not '2026-10-12' <= start.date().isoformat() <= '2026-10-14'
                    or (start.hour,start.minute,start.second,start.microsecond) != (11,0,0,0)
                    or (end.hour,end.minute,end.second,end.microsecond) != (22,0,0,0)
                    or result and start <= result[-1][1]):
                raise ValueError
            result.append((start.astimezone(UTC),end.astimezone(UTC),44))
        return result
    except (ValueError,TypeError,KeyError,OverflowError):
        raise TrialError('cloud_plan_invalid') from None


def _decode(body):
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result:
                raise TrialError('cloud_json_invalid')
            result[key] = value
        return result
    return json.loads(body,object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(TrialError('cloud_json_invalid')))


class Ledger:
    """Immutable private checkpoints; attempt reservation survives restarts."""
    def __init__(self, root, plan):
        self.root = Path(root)
        self.digest = hashlib.sha256(encoded(plan)).hexdigest()
        self.limit = plan['renewal_limit']
        files = sorted(self.root.glob('checkpoint-*.json'))
        if len(files) > 128 or any(p.name != f'checkpoint-{i:03}.json' for i,p in enumerate(files)):
            raise TrialError('cloud_ledger_invalid')
        self.index = len(files)
        self.value = _decode(_read_private_file(files[-1])) if files else {
            'schema_version':1,'plan_sha256':self.digest,'attempts':0,'pending':False,
            'halted':False,'error_code':None,'highest_revision':0,'context_sha256':None,
            'finished_windows':[]}
        v = self.value
        if (type(v) is not dict or set(v) != {'schema_version','plan_sha256','attempts','pending',
                'halted','error_code','highest_revision','context_sha256','finished_windows'}
                or type(v['schema_version']) is not int or v['schema_version'] != 1
                or v['plan_sha256'] != self.digest
                or type(v['attempts']) is not int or not 0 <= v['attempts'] <= self.limit
                or type(v['pending']) is not bool or type(v['halted']) is not bool
                or type(v['highest_revision']) is not int or v['highest_revision'] < 0
                or v['context_sha256'] is not None and (type(v['context_sha256']) is not str
                    or not re.fullmatch('[0-9a-f]{64}',v['context_sha256']))
                or (v['highest_revision'] == 0) != (v['context_sha256'] is None)
                or type(v['finished_windows']) is not list
                or any(type(i) is not int or not 0 <= i < len(plan['windows']) for i in v['finished_windows'])
                or v['finished_windows'] != sorted(set(v['finished_windows']))
                or v['error_code'] not in {None,'cloud_refresh_failed','cloud_reader_failed','cloud_trial_failed'}):
            raise TrialError('cloud_ledger_invalid')
        if v['pending']:
            raise TrialError('cloud_refresh_outcome_unknown')
        if v['halted']:
            raise TrialError('cloud_trial_halted')
        if not files:
            self.save()

    def save(self):
        if self.index >= 128:
            raise TrialError('cloud_checkpoint_limit')
        result = _write_packet(encoded(self.value),self.root/f'checkpoint-{self.index:03}.json')
        if result['durability_confirmed'] is not True:
            raise TrialError('cloud_checkpoint_unconfirmed')
        self.index += 1

    def reserve(self):
        if self.value['pending'] or self.value['attempts'] >= self.limit:
            raise TrialError('cloud_refresh_limit')
        self.value['attempts'] += 1
        self.value['pending'] = True
        self.save()  # Before supplier/init/query, not after a crash-prone call.

    def check_context(self, revision, digest):
        if revision != self.value['highest_revision'] or digest != self.value['context_sha256']:
            raise TrialError('cloud_context_changed')

    def accepted(self, revision, digest):
        if not self.value['pending'] or revision <= self.value['highest_revision']:
            raise TrialError('cloud_revision_invalid')
        self.value.update(pending=False,highest_revision=revision,context_sha256=digest)
        self.save()

    def finish(self,index):
        self.value['finished_windows'] = sorted(set(self.value['finished_windows']) | {index})
        self.save()

    def halt(self, code):
        self.value.update(halted=True,error_code=code)
        self.save()


class Controller:
    def __init__(self, root, plan, source, ledger):
        self.root,self.plan,self.source,self.ledger = Path(root),plan,source,ledger
        self.credentials = self.root/'query-context.json'

    def context(self):
        before = _read_private_file(self.credentials)
        value = read_credentials_file(self.credentials,api_profile='miniapp_gateway')
        after = _read_private_file(self.credentials)
        if before != after:
            raise TrialError('cloud_context_changed')
        digest = hashlib.sha256(before).hexdigest()
        if value.revision is None:
            raise TrialError('cloud_context_invalid')
        return value,digest

    def ensure_refresh(self,start,end, *, now=None):
        now = utc_now() if now is None else now
        if not start <= now < end:
            return False  # No supplier/init/query even if the context expired.
        bootstrap = not os.path.lexists(self.credentials)
        if bootstrap:
            if self.ledger.value['highest_revision']:
                raise TrialError('cloud_context_missing')
            previous = None
        else:
            previous,digest = self.context()
            self.ledger.check_context(previous.revision,digest)
            auth = describe_authorization(previous.authorization,now=now)
            remaining = auth.get('remaining_seconds')
            if type(remaining) not in (int,float):
                raise TrialError('cloud_auth_timing_unknown')
            if remaining > 90:
                return False
        if (end-now).total_seconds() < 100:
            return False
        self.ledger.reserve()
        result = self.perform(start,end,previous)
        if not all(result.get(k) is True for k in ('ok','query_accepted','committed','durability_confirmed')):
            self.ledger.halt('cloud_refresh_failed')
            raise TrialError('cloud_refresh_failed')
        current,digest = self.context()
        self.ledger.accepted(current.revision,digest)
        print(json.dumps({'event':'cloud_context_published','revision':current.revision,
            'attempt':self.ledger.value['attempts'],'first_publication':bootstrap,
            'query_accepted':True,'durability_confirmed':True,'at':stamp(utc_now())}),flush=True)
        return True

    def perform(self,start,end,previous):
        if previous is None:
            accept = prepare_bootstrap_verifier(credentials_file=self.credentials,
                expected_app_id=self.plan['app_id'],expected_app_client=self.source.app_client,
                store_id=self.plan['store_ids'][0],timeout=15)
        else:
            accept = prepare_verifier(credentials_file=self.credentials,
                expected_revision=previous.revision,store_id=self.plan['store_ids'][0],timeout=15)
        def prepare(code,second):
            if not start <= utc_now() < end-timedelta(seconds=46):
                raise TrialError('cloud_business_window_closed')
            return self.source(code,second)
        def consume(token,headers):
            if not start <= utc_now() < end-timedelta(seconds=16):
                raise TrialError('cloud_business_window_closed')
            headers = dict(headers);headers['Content-Type'] = self.source.query_content_type
            return accept(token,headers)
        options = dict(supplier_timeout=45,initialize_timeout=30,expected_app_id=self.plan['app_id'])
        if previous is None:
            return initialize_for_target_from_yyb(prepare,consume,
                settings_file=self.root/'bootstrap-settings.json',**options)
        return initialize_from_yyb(prepare,consume,reference_context_file=self.credentials,
            settings_file=self.root/'renewal-settings.json',**options)


def collect_arguments(root,store_ids,rounds,task,resume,start,end):
    args = [sys.executable,'-I','-u',str(root/'official_business_collect.py'),
            stamp(start),stamp(end),'collect','--api-profile','miniapp_gateway']
    for store in store_ids:
        args += ['--store-id',store]
    args += ['--interval','900','--samples',str(rounds),'--wait-for-credentials','600',
        '--credentials-file',str(root/'query-context.json'),'--db',str(root/'observations.sqlite'),
        '--task-file',str(task)]
    if resume:
        args.append('--resume-task')
    return args


def main():
    if len(sys.argv) != 2:
        print('{"ok":false,"error_code":"cloud_root_required"}');return 2
    root = Path(sys.argv[1])
    os.umask(0o077)
    lock = child = ledger = None
    stopped = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stopped.set())
    try:
        if not root.is_absolute():
            raise TrialError('cloud_plan_invalid')
        plan = _decode(_read_private_file(root/'trial-plan.json'))
        windows = validate_plan(plan)
        lock = os.open(root/'controller.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state = root/'controller-state';state.mkdir(mode=0o700,exist_ok=True)
        ledger = Ledger(state,plan)
        source = prepare_material_source(bundle_file=root/'initialization-material.json',
            expected_app_id=plan['app_id'],expected_source_version=plan['source_version'],
            expected_package_sha256=plan['package_sha256'])
        controller = Controller(root,plan,source,ledger)
        for index,(start,end,rounds) in enumerate(windows):
            if index in ledger.value['finished_windows']:
                continue
            while utc_now() < start and not stopped.is_set():
                stopped.wait(min(10,max(0,(start-utc_now()).total_seconds())))
            if stopped.is_set():
                return 0
            if utc_now() >= end:
                ledger.finish(index)
                print(json.dumps({'event':'cloud_window_missed','index':index,'backfill':False}),flush=True)
                continue
            task = root/f'collection-{start.astimezone(CHINA).date()}.json'
            resume = os.path.lexists(task)
            if resume:
                previous = task_status(task)
                if previous['failed_slots']:
                    raise TrialError('cloud_reader_failed')
                if previous['state'] == 'completed':
                    ledger.finish(index);continue
                if previous['stop_reason'] not in {None,'auth_expiring','auth_declared_expired'}:
                    raise TrialError('cloud_reader_failed')
            controller.ensure_refresh(start,end)
            # -I deliberately uses the installed rc82 wheel, not this checkout.
            child = subprocess.Popen(collect_arguments(root,plan['store_ids'],rounds,task,resume,start,end),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            print(json.dumps({'event':'cloud_reader_started','pid':child.pid,'window':index,
                'resume':resume,'interval_seconds':900,'ordinary_query_limit':rounds*len(plan['store_ids'])}),flush=True)
            while utc_now() < end and not stopped.is_set():
                if child.poll() is not None:
                    if child.returncode != 0:
                        raise TrialError('cloud_reader_failed')
                    break
                controller.ensure_refresh(start,end)
                stopped.wait(1)
            if child.poll() is None:
                child.terminate();child.wait(timeout=20)
            if stopped.is_set():
                return 0  # Task checkpoint remains; restart waits a full period.
            status = task_status(task)
            if status['failed_slots']:
                raise TrialError('cloud_reader_failed')
            ledger.finish(index)
            print(json.dumps({'event':'cloud_window_finished','window':index,
                **{k:status[k] for k in ('state','completed_slots','successful_slots','uncertain_slots')},
                'backfill':False}),flush=True)
            child = None
        print('{"event":"cloud_finite_trial_finished"}',flush=True);return 0
    except Exception as error:
        allowed = {'cloud_plan_invalid','cloud_json_invalid','cloud_ledger_invalid',
            'cloud_refresh_outcome_unknown','cloud_trial_halted','cloud_checkpoint_limit',
            'cloud_checkpoint_unconfirmed','cloud_refresh_limit','cloud_context_changed',
            'cloud_revision_invalid','cloud_context_invalid','cloud_context_missing',
            'cloud_auth_timing_unknown','cloud_refresh_failed','cloud_reader_failed',
            'cloud_business_window_closed'}
        code = str(error) if isinstance(error,TrialError) and str(error) in allowed else 'cloud_trial_failed'
        if ledger is not None:
            try:
                ledger.halt('cloud_reader_failed' if code=='cloud_reader_failed' else 'cloud_trial_failed')
            except Exception:
                pass  # Preserve any pending immutable checkpoint; no retry.
        print(json.dumps({'ok':False,'event':'cloud_trial_stopped','error_code':code,
                          'automatic_retry':False}),flush=True);return 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=5)
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
