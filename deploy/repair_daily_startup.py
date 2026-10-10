"""Operator-only recovery of a verified zero-attempt, unpublished first window.

Stop the fleet before use. Original bytes are retained in a private incident
directory. No HTTP, budget reset, catch-up, or recovery of attempted tasks.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from sushiwait.dailycontroller import _decode, _private_dir, read_daily_archive
from sushiwait.remotecampaign import _decode_campaign
from sushiwait.remotetasks import _at, _now
from sushiwait.remotewindow import _decode_window


def private(path, directory=False):
    info=path.lstat()
    if (info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=(0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink!=1):
        raise ValueError('startup_recovery_unsafe_path')


def lock(path, stack):
    private(path)
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
    stack.callback(os.close,fd)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)


def write_new(path, body):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(body);stream.flush();os.fsync(stream.fileno())


def sync(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def repair(root, store_ids, incident, *, apply=False, now=None):
    root,incident=Path(root).absolute(),Path(incident).absolute()
    now=now or datetime.now(timezone.utc)
    if not store_ids or len(set(store_ids))!=len(store_ids) or len(store_ids)>256:
        raise ValueError('startup_recovery_scope')
    private(root,True)
    if incident.is_relative_to(root) or incident.exists() or incident.is_symlink():
        raise ValueError('startup_recovery_new_external_incident_required')
    prepared=[]
    with ExitStack() as stack:
        lock(root/'fleet.lock',stack)
        for store in store_ids:
            if not store.isdecimal() or str(int(store))!=store:
                raise ValueError('startup_recovery_store_id')
            folder=root/('store-'+store);private(folder,True)
            lock(folder/'controller.json.lock',stack)
            raw=read_daily_archive(folder/'controller.json');v=_decode(raw)
            day=v['current']
            # Descriptor exhaustion can also prevent persisting the halt.
            startup_state=(v['state']=='halted' and v['error_code']=='daily_controller_storage_or_input_error'
                or v['state']=='active' and v['error_code'] is None)
            if (v['config']['store_id']!=store or not startup_state
                    or day is None or day['phase']!='collecting'
                    or not _at(v['updated_at'])<=now<_at(day['deadline_at'])
                    or now.astimezone(_at(day['created_at']).tzinfo).date()!=_at(day['created_at']).date()):
                raise ValueError('startup_recovery_not_zero_attempt_startup')
            campaign=folder/day['local_date']/'campaign'
            private(campaign.parent,True);private(campaign,True)
            lock(campaign/'campaign.json.lock',stack)
            cr=read_daily_archive(campaign/'campaign.json');cv=_decode_campaign(cr)
            if (cv['state']!='active' or cv['windows'] or cv['config']['store_ids']!=[store]
                    or cv['config']['root']!=str(campaign) or now>=_at(cv['deadline_at'])
                    or cv['config']['max_pairs']!=day['maximum_pair_budget']):
                raise ValueError('startup_recovery_published_or_expired_campaign')
            names={p.name for p in campaign.iterdir()}
            if names-{'campaign.json','campaign.json.lock','window-01'}:
                raise ValueError('startup_recovery_foreign_campaign_files')
            orphan=campaign/'window-01';inventory={}
            if orphan.exists() or orphan.is_symlink():
                private(orphan,True)
                if {p.name for p in orphan.iterdir()}-{'task.json.lock','task.json','remote.sqlite3'}:
                    raise ValueError('startup_recovery_foreign_window_files')
                for p in orphan.iterdir():
                    private(p)
                    if p.stat().st_size>(1024*1024 if p.name=='remote.sqlite3' else 16*1024):
                        raise ValueError('startup_recovery_file_too_large')
                    inventory[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
                    if p.name.endswith('.lock'):
                        if p.stat().st_size:raise ValueError('startup_recovery_lock_not_empty')
                        lock(p,stack)
                task=orphan/'task.json'
                if task.exists():
                    t=_decode_window(read_daily_archive(task))
                    if (t['state']!='ready' or any(t[k] for k in ('cursor','successful','failed','uncertain','recorded_http_attempts'))
                            or t['pending'] is not None or t['last_attempt_at'] is not None or t['starts']
                            or t['config']['store_ids']!=[store]):
                        raise ValueError('startup_recovery_attempted_task')
                db=orphan/'remote.sqlite3'
                if db.exists():
                    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=0) as connection:
                        objects=connection.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                        if objects not in ([],[('table','remote_samples')]):
                            raise ValueError('startup_recovery_foreign_database')
                        if objects and connection.execute('SELECT count(*) FROM remote_samples').fetchone()[0]:
                            raise ValueError('startup_recovery_database_contains_observations')
            after=deepcopy(v);after.update(state='active',error_code=None,updated_at=_now(now))
            body=json.dumps(after,sort_keys=True,allow_nan=False).encode();_decode(body)
            prepared.append((store,folder,campaign,orphan,raw,cr,body,inventory))
        report={'schema_version':1,'recovery':'verified_unpublished_zero_attempt_startup',
            'store_ids':store_ids,'applied':False,'official_requests_added':0,
            'original_deadlines_and_budgets_preserved':True,'catch_up_requests':0,
            'recovered_at':_now(now),'stores':[]}
        if not apply:return report
        _private_dir(incident.parent);incident.mkdir(mode=0o700)
        # Retain every original checkpoint before changing any controller.
        for store,folder,campaign,orphan,raw,cr,body,inventory in prepared:
            dest=incident/('store-'+store);dest.mkdir(mode=0o700)
            write_new(dest/'controller.before.json',raw);write_new(dest/'campaign.before.json',cr)
            write_new(dest/'controller.after.json',body)
            report['stores'].append({'store_id':store,'controller_before_sha256':hashlib.sha256(raw).hexdigest(),
                'campaign_before_sha256':hashlib.sha256(cr).hexdigest(),'unpublished_window_files':inventory})
        write_new(incident/'plan.json',json.dumps(report,sort_keys=True).encode());sync(incident)
        for store,folder,campaign,orphan,raw,cr,body,inventory in prepared:
            if orphan.exists():os.rename(orphan,incident/('store-'+store)/'unpublished-window-01')
            sync(campaign)
            temporary=folder/'controller.startup-recovery.tmp'
            write_new(temporary,body);os.replace(temporary,folder/'controller.json');sync(folder)
            sync(incident/('store-'+store))
        report['applied']=True
        write_new(incident/'result.json',json.dumps(report,sort_keys=True).encode());sync(incident)
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True);parser.add_argument('--store-id',action='append',required=True)
    parser.add_argument('--incident',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(repair(args.root,args.store_id,args.incident,apply=args.apply),sort_keys=True))
