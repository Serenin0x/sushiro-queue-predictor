"""One closed-hours handover, retaining old files and bounded task budgets.

Run by the authorized system operator. Public gateway exposure must already
be authorized. Archive checks run as the original file owner. No origin GETs,
credentials, deletion, new firewall rules or edits to old task checkpoints.
"""
import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

from sushiwait.businesshours import BusinessHours, read_hours
from sushiwait.dailyfleet import read_catalog
from sushiwait.remotetasks import _at, _now
from zoneinfo import ZoneInfo


def validate(meta):
    keys={'schema_version','release','root','user','not_before','cutover_at','old_units',
        'old_collector_units','new_units','port','public_port','legacy_exports_root',
        'legacy_through_date','legacy_store_ids'}
    if type(meta) is not dict or set(meta)!=keys or meta['schema_version']!=1:
        raise ValueError('cutover_configuration_invalid')
    for key in ('release','root','legacy_exports_root'):
        if (type(meta[key]) is not str or not Path(meta[key]).is_absolute()
                or len(meta[key])>400 or '..' in Path(meta[key]).parts or '%' in meta[key]):
            raise ValueError('cutover_path_invalid')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',meta['user']):raise ValueError('cutover_user_invalid')
    for key in ('old_units','old_collector_units','new_units'):
        v=meta[key]
        if (type(v) is not list or not 1<=len(v)<=20 or len(set(v))!=len(v)
                or any(type(s) is not str or not re.fullmatch(r'sushiwait-[a-z0-9-]{1,70}\.service',s) for s in v)):
            raise ValueError('cutover_unit_scope_invalid')
    if (len(meta['new_units'])!=3 or set(meta['new_units'])&set(meta['old_units'])
            or set(meta['old_collector_units'])-set(meta['old_units'])
            or type(meta['legacy_store_ids']) is not list or not 1<=len(meta['legacy_store_ids'])<=16
            or len(meta['old_collector_units'])!=len(meta['legacy_store_ids'])
            or len(set(meta['legacy_store_ids']))!=len(meta['legacy_store_ids'])
            or any(not re.fullmatch(r'[1-9][0-9]{0,9}',s) for s in meta['legacy_store_ids'])):
        raise ValueError('cutover_store_scope_invalid')
    activation,cutover=_at(meta['not_before']),_at(meta['cutover_at'])
    if (date.fromisoformat(meta['legacy_through_date']).isoformat()!=meta['legacy_through_date']
            or (activation-cutover).total_seconds()<300
            or meta['legacy_through_date']>=activation.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()):
        raise ValueError('cutover_date_scope_invalid')
    for key in ('port','public_port'):
        if type(meta[key]) is not int or not 1024<=meta[key]<=65535:raise ValueError('cutover_port_invalid')
    return meta


def verify_archives(meta, runner):
    # POSIX private-file checks execute under the owner, not the root service.
    code=("import json,sys; from sushiwait.dailyarchive import read_legacy_day; "
        "v=[read_legacy_day(sys.argv[1],s,sys.argv[2]) for s in json.loads(sys.argv[3])]; "
        "assert all(x['summary'] is not None and not x['graph_truncated'] for x in v); "
        "print(json.dumps({'verified_archives':len(v),'origin_requests':0}))")
    command=['runuser','-u',meta['user'],'--',str(Path(meta['release'])/'.venv/bin/python'),'-I','-c',code,
        meta['legacy_exports_root'],meta['legacy_through_date'],json.dumps(meta['legacy_store_ids'])]
    result=runner(command,check=True,capture_output=True,text=True,timeout=30)
    value=json.loads(result.stdout)
    if value!={'verified_archives':len(meta['legacy_store_ids']),'origin_requests':0}:
        raise ValueError('cutover_archives_not_verified')


def read_local(port,path):
    opener=build_opener(ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}'+path,timeout=3) as response:
        raw=response.read(8*1024*1024+1)
        if response.status!=200 or len(raw)>8*1024*1024:raise ValueError('cutover_read_failed')
        return json.loads(raw)


def run(meta, *, now=None, runner=subprocess.run, archive_check=verify_archives,
        reader=read_local, sleep=time.sleep):
    validate(meta)
    target=Path(meta['root'])/'cutover-proof.json'
    if target.exists() or target.is_symlink():
        raise ValueError('cutover_already_recorded_requires_review')
    clock=datetime.now(timezone.utc) if now is None else now
    if clock<_at(meta['cutover_at']):raise ValueError('cutover_not_due')
    if clock>=_at(meta['not_before']):raise ValueError('cutover_late_requires_review')
    release=Path(meta['release']);catalog=read_catalog(release/'config/mainland-store-catalog.json')
    ids=[r['store_id'] for r in catalog['stores']]
    if set(meta['legacy_store_ids'])-set(ids):raise ValueError('cutover_catalog_scope_invalid')
    hours=BusinessHours(read_hours(release/'config/default-business-hours.json'))
    if any(hours.decision(s,clock)['is_open_window'] for s in ids):raise ValueError('cutover_requires_closed_hours')
    archive_check(meta,runner)
    previous={}
    for unit in meta['old_units']+meta['new_units']:
        state=runner(['systemctl','is-enabled',unit],capture_output=True,text=True,timeout=5).stdout.strip()
        if state not in ('enabled','disabled'):
            raise ValueError('cutover_unit_enable_state_unconfirmed')
        previous[unit]=state
    changed_enable_states=False
    try:
        runner(['systemctl','stop',*meta['old_units']],check=True,timeout=60)
        for unit in meta['old_collector_units']:
            state=runner(['systemctl','is-active',unit],capture_output=True,text=True,timeout=5)
            if state.stdout.strip() not in ('inactive','failed'):raise ValueError('cutover_old_writer_stop_unconfirmed')
        runner(['systemctl','start',*meta['new_units']],check=True,timeout=60)
        healthy=False
        for _ in range(20):
            try:
                status=reader(meta['port'],'/api/v1/status')
                index=reader(meta['public_port'],'/api/v1/days')
                healthy=(status.get('service_state')=='running' and status.get('worker_alive') is True
                    and status.get('store_ids')==ids and status.get('alive_store_workers')==len(ids)
                    and status.get('failed_store_ids')==[] and status.get('origin_gate',{}).get('origin_halted') is False
                    and index.get('configured_store_ids')==ids and index.get('unavailable_store_ids')==[]
                    and set(index.get('days',{}).get(meta['legacy_through_date'],{}))==set(meta['legacy_store_ids']))
                if healthy:break
            except (ValueError,OSError):pass
            sleep(.5)
        if not healthy:raise ValueError('cutover_new_readers_unconfirmed')
        changed_enable_states=True
        runner(['systemctl','enable',*meta['new_units']],check=True,timeout=30)
        runner(['systemctl','disable',*meta['old_units']],check=True,timeout=30)
    except BaseException:
        runner(['systemctl','stop',*meta['new_units']],check=False,timeout=60)
        for unit in meta['new_units']:
            state=runner(['systemctl','is-active',unit],capture_output=True,text=True,timeout=5)
            if state.stdout.strip() not in ('inactive','failed'):
                raise ValueError('cutover_rollback_stop_unconfirmed')
        if changed_enable_states:
            for state,command in [('enabled','enable'),('disabled','disable')]:
                units=[u for u,v in previous.items() if v==state]
                if units:runner(['systemctl',command,*units],check=False,timeout=30)
        # Original task identities/deadlines are unchanged on rollback.
        runner(['systemctl','start',*meta['old_units']],check=False,timeout=60)
        raise
    proof={'cutover_started_at':_now(clock),
        'cutover_completed_at':_now(datetime.now(timezone.utc) if now is None else clock),'configured_stores':len(ids),
        'old_store_writers_verified_stopped':len(meta['old_collector_units']),
        'verified_old_archives':len(meta['legacy_store_ids']),'old_files_preserved':True,
        'new_activation':meta['not_before'],'official_requests_added_by_handover':0,
        'previous_unit_enable_states':previous,
        'current_mainland_completeness_verified':False}
    # Private local operator evidence, separate from immutable store archives.
    with target.open('x') as stream:json.dump(proof,stream);stream.flush();os.fsync(stream.fileno())
    target.chmod(0o600)
    print(json.dumps(proof))
    return proof


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config-file',required=True)
    args=p.parse_args();run(json.loads(Path(args.config_file).read_text()))
