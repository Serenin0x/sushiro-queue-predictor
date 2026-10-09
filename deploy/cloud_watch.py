"""Bounded local health observer. Never queries the restaurant or restarts it."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from sushiwait.monitorhub import MonitorHub, read_config
from sushiwait.credentials import _read_private_file


def instant(value):return datetime.fromisoformat(value.replace('Z','+00:00'))


def snapshot(hub,now):
    if now>=instant(hub.config['deadline_at']):
        return {'checked_at':now.isoformat(),'phase':'deadline_reached','stores':[],
                'official_requests_added_by_observer':0}
    stores=[]
    for store in hub.names:
        try:
            v=hub.status(store);t=v['task'];window=v.get('business_windows',{}).get(store,{})
            age=max(0,(now-instant(t['updated_at'])).total_seconds())
            open_age=(now-instant(window['window_start_at'])).total_seconds() if window.get('is_open_window') else 0
            stale=age>180 and open_age>180 and v['service_state']=='running'
            stores.append({'store_id':store,'state':v['service_state'],'worker_alive':v['worker_alive'],
                'collection_phase':v.get('collection_phase'),'successful_pairs':t['successful_pairs'],
                'failed_pairs':t['failed_pairs'],'recorded_http_attempts':t['recorded_http_attempts'],
                'updated_at':t['updated_at'],'deadline_at':t['deadline_at'],
                'maximum_pair_budget':t['maximum_pair_budget'],
                'maximum_request_budget':t['maximum_request_budget'],
                'observation_late':stale,'needs_attention':stale or v['service_state'] not in {'running','completed'}
                    or v['service_state']=='running' and not v['worker_alive'],
                'error_code':v.get('error_code')})
        except Exception:
            stores.append({'store_id':store,'state':'unavailable','needs_attention':True})
    return {'checked_at':now.isoformat(),'phase':'observing','stores':stores,
            'official_requests_added_by_observer':0,'source_freshness':'unknown'}


def save(root,value):
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=root,prefix='.progress-',delete=False) as out:
        path=Path(out.name);json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
    os.replace(path,root/'progress.json')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
    os.umask(0o077);root=Path(args.root).absolute();info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.geteuid():
        raise ValueError('private_observer_directory_required')
    output=root/'observer';info=output.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.geteuid():
        raise ValueError('private_observer_output_required')
    raw=json.loads(_read_private_file(root/'hub.json'))
    now=datetime.now(timezone.utc)
    if now>=instant(raw['deadline_at']):
        save(output,{'checked_at':now.isoformat(),'phase':'deadline_reached','stores':[],
                     'official_requests_added_by_observer':0});return
    hub=MonitorHub(read_config(root/'hub.json'));prior=None;deadline=instant(hub.config['deadline_at'])
    while True:
        now=datetime.now(timezone.utc);value=snapshot(hub,now);save(output,value)
        attention=[s['store_id'] for s in value['stores'] if s['needs_attention']]
        signature=(value['phase'],tuple(attention))
        if signature!=prior:
            print(json.dumps({'observer_phase':value['phase'],'attention_store_ids':attention,
                             'official_requests_added':0}),flush=True);prior=signature
        if now>=deadline:return
        time.sleep(min(60,max(0,(deadline-datetime.now(timezone.utc)).total_seconds())))


if __name__=='__main__':main()
