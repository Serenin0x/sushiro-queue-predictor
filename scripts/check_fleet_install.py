"""Installed fleet startup and optional full-day memory probe; no origin I/O."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

from sushiwait.businesshours import read_hours
from sushiwait.dailyfleet import DailyFleetService, read_catalog
from sushiwait.remote import QUEUE_NAMES, RemoteClient


def check(args):
    catalog=read_catalog(args.catalog_file)
    hours=read_hours(args.business_hours_file)
    now=datetime.now(timezone.utc)
    calls=[]
    class Response:
        headers={}
        def __init__(self,r):self.r=r
        def getcode(self):return 200
        def geturl(self):return self.r.full_url
        def close(self):pass
        def read(self,size):return json.dumps({q:['100','101','102'] for q in QUEUE_NAMES}
            if 'groupqueues?' in self.r.full_url else 1).encode()[:size]
    class Transport:
        def open(self,r,timeout):calls.append(r.full_url);return Response(r)
    def forbidden_factory():raise AssertionError('client created before activation')
    tick=time.monotonic()
    with tempfile.TemporaryDirectory() as folder:
        service=DailyFleetService(root=Path(folder).resolve()/'fleet',catalog=catalog,
            business_hours=hours,not_before=(now+timedelta(days=1)).isoformat(),opener_factory=forbidden_factory)
        try:
            service.start();status=service.status()
            assert status['alive_store_workers']==len(catalog['stores']) and status['failed_store_ids']==[]
            assert status['origin_gate']['transport_admissions_this_process']==0
            startup=time.monotonic()-tick
            if args.points:
                template=RemoteClient(opener=Transport()).snapshot(catalog['stores'][0]['store_id'])
                day=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat()
                base=datetime.fromisoformat(day+'T10:30:00+08:00')
                for store,child in service.children.items():
                    child.active_day=day
                    for i in range(args.points):
                        value=deepcopy(template);value['requested_store_id']=store
                        stamp=base+timedelta(minutes=i)
                        for j,q in enumerate(value['queries'].values()):
                            q['started_at']=(stamp+timedelta(milliseconds=200*j)).isoformat()
                            q['received_at']=(stamp+timedelta(milliseconds=200*j+100)).isoformat()
                            q['elapsed_ms']=100
                        value['queries']['groupqueues']['payload']['queues']={q:[str(100+i),str(101+i),str(102+i)] for q in QUEUE_NAMES}
                        child.daily_view.publish(value,key=i,run='synthetic-capacity-probe')
                        child.view.publish(value)
                index=service.daily_batch_index(day[:7])
                assert len(index['days'][day])==len(service.names)
                assert all(r['observations']==args.points for r in index['days'][day].values())
            assert service.gate.status()['transport_admissions_this_process']==0
        finally:service.shutdown()
        assert service.lock_fd is None and not any(c.thread.is_alive() for c in service.children.values())
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bytes_rss=rss if sys.platform=='darwin' else rss*1024
    result={'installed_fleet_ok':True,'configured_store_count':len(catalog['stores']),
        'synthetic_points_per_store':args.points,'synthetic_projection_pairs':args.points*len(catalog['stores']),
        'synthetic_template_http_calls':len(calls),'official_http_requests':0,
        'fleet_transport_admissions':0,'startup_seconds':round(startup,3),
        'elapsed_seconds':round(time.monotonic()-tick,3),'peak_rss_mib':round(bytes_rss/1024**2,2),
        'all_workers_stopped':True,'current_mainland_completeness_verified':False,'eta_available':False}
    print(json.dumps(result));return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog-file',required=True)
    p.add_argument('--business-hours-file',required=True);p.add_argument('--points',type=int,default=0)
    args=p.parse_args()
    if not 0<=args.points<=1500:p.error('points must be bounded by the daily cap')
    check(args)
