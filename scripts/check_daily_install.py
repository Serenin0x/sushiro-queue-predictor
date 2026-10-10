"""Installed daily lifecycle with two synthetic days and offline archive reads."""
import argparse
import asyncio
from datetime import datetime,timedelta,timezone
import json
import hashlib
import shutil
from pathlib import Path
import tempfile
from unittest.mock import patch

from sushiwait.businesshours import read_hours
from sushiwait.dailycontroller import DailyController,daily_config,daily_controller_status
from sushiwait.dailyquality import daily_quality_report
from sushiwait.dailyarchive import read_day
from sushiwait.daybackup import create_backup, restore_backup
from sushiwait.dailyservice import DailyCollectorService
from sushiwait.remote import RemoteClient,QUEUE_NAMES
from sushiwait.remoteservice import RemoteASGI


def check(source_root):
    hours=read_hours(source_root/'config/default-business-hours.json')
    hours['weekday_intervals']={str(i):[['11:00','11:02']] for i in range(1,8)}
    hours['known_statutory_holiday_intervals']=[['11:00','11:02']]
    base=datetime(2026,10,9,3,tzinfo=timezone.utc);seconds=[0];calls=[]
    wall=lambda:base+timedelta(seconds=seconds[0])
    def sleep(delay):seconds[0]+=delay
    class Response:
        headers={}
        def __init__(self,request):self.request=request
        def geturl(self):return self.request.full_url
        def getcode(self):return 200
        def close(self):pass
        def read(self,size):
            return json.dumps({k:['12','13','14'] for k in QUEUE_NAMES}
                if 'groupqueues?' in self.request.full_url else 0).encode()[:size]
    class Transport:
        def open(self,request,timeout):calls.append(seconds[0]);return Response(request)
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder).resolve()/'daily'
        cfg=daily_config(root,'900001',hours,not_before=base.isoformat())
        with patch('socket.socket',side_effect=AssertionError('synthetic only')), \
             patch('sushiwait.remote._utc',side_effect=lambda:wall().isoformat()), \
             patch('sushiwait.remoteintake._clock',side_effect=wall):
            for day in range(2):
                seconds[0]=day*86400
                with DailyController(config=cfg,now=wall()) as controller:
                    controller.tick(wall_clock=wall,monotonic_clock=lambda:seconds[0],sleep=sleep,
                        emit=lambda _:None,client_factory=lambda:RemoteClient(opener=Transport()))
            assert len(calls)==8
            assert daily_controller_status(root)['last_finished_date']=='2026-10-10'
            for day in ['2026-10-09', '2026-10-10']:
                projection = json.loads((root/day/'projection.json').read_bytes())
                quality = json.loads((root/day/'quality.json').read_bytes())
                assert quality == daily_quality_report(projection, store_id='900001', day=day,
                    as_of=projection['generated_at'])
                assert quality['quality_state'] == 'declared_day_observed'
                assert quality['projection_content_sha256'] == hashlib.sha256((root/day/'projection.json').read_bytes()).hexdigest()
        service=DailyCollectorService(root=root,store_id='900001',business_hours=hours,
            not_before=base.isoformat(),wall_clock=wall)
        before=len(calls)
        for path in ['/api/v1/months/2026-10','/api/v1/stores/900001/days/2026-10-09']:
            messages=[]
            async def receive():return {'type':'http.request','body':b''}
            async def send(value):messages.append(value)
            asyncio.run(RemoteASGI(service)({'type':'http','method':'GET','path':path,
                'query_string':b''},receive,send))
            assert messages[0]['status']==200
            assert json.loads(messages[1]['body'])['network_performed_by_read'] is False
        assert len(calls)==before and service.thread is None
        # A finite-trial projection is read in place, alongside a disjoint new
        # day. No migration, forged receipt time or duplicate collector.
        exports=root.parent/'old-exports';exports.mkdir(mode=0o700)
        day=exports/'2026-10-09';day.mkdir(mode=0o700)
        raw=(root/'2026-10-09/projection.json').read_bytes();old=json.loads(raw)
        projection=day/'store-900001.json';projection.write_bytes(raw);projection.chmod(0o600)
        manifest=day/'manifest.json'
        manifest.write_text(json.dumps({'local_date':'2026-10-09','exported_at':'2026-10-09T14:05:00Z',
            'stores':[{'store_id':'900001','name':'合成旧归档','state':'saved','points':len(old['points']),
                'complete_observed_projection':True,'sha256':hashlib.sha256(raw).hexdigest(),
                'daily_quality':old['summary']}], 'official_requests_added_by_export':0,
            'active_database_opened':False,'independent_backup':False,'full_day_source_quality_verified':False}))
        manifest.chmod(0o600)
        newroot=root.parent/'reader';newroot.mkdir(mode=0o700)
        newday=newroot/'2026-10-10';newday.mkdir(mode=0o700)
        for name in ['projection.json','result.json']:
            (newday/name).write_bytes((root/'2026-10-10'/name).read_bytes());(newday/name).chmod(0o600)
        reader=DailyCollectorService(root=newroot,store_id='900001',business_hours=hours,
            not_before=(base+timedelta(days=1)).isoformat(),wall_clock=wall,
            legacy_exports_root=exports,legacy_through_date='2026-10-09')
        with patch('socket.socket',side_effect=AssertionError('offline archive only')), \
             patch('sqlite3.connect',side_effect=AssertionError('reader database opened')):
            index=reader.daily_index('900001','2026-10');detail=reader.daily_detail('900001','2026-10-09')
            assert len(index['days'])==2 and not index['unavailable_archive_dates']
            assert detail['points']==old['points'] and detail['generated_at']==old['generated_at']
            assert detail['archive_lineage']['exported_at']=='2026-10-09T14:05:00Z'
            assert detail['archive_lineage']['full_day_source_quality_verified'] is False
        assert projection.read_bytes()==raw and len(calls)==before and reader.thread is None
        fleet=root.parent/'backup-source';fleet.mkdir(mode=0o700)
        shutil.copytree(root,fleet/'store-900001')
        sources={p:p.read_bytes() for p in fleet.rglob('*') if p.is_file()}
        bundle=root.parent/'closed-day.zip';destination=root.parent/'restored-day'
        with patch('socket.socket',side_effect=AssertionError('backup opened network')), \
             patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('backup started source')):
            created=create_backup(root=fleet,store_ids=['900001'],day='2026-10-09',output=bundle,now=wall())
            checked=restore_backup(bundle=bundle)
            recovered=restore_backup(bundle=bundle,destination=destination)
            assert created['raw_records']==2 and checked['restored_readable'] and recovered['restored_readable']
            assert not recovered['collector_started_or_resumed'] and not recovered['independent_machine_verified']
            assert read_day(fleet/'store-900001','900001','2026-10-09') == read_day(destination/'store-900001','900001','2026-10-09')
        assert {p:p.read_bytes() for p in sources}==sources and not list(destination.rglob('*.lock'))
    print(json.dumps({'installed_daily_lifecycle_ok':True,'synthetic_days':2,'synthetic_http_attempts':8,
        'archive_reads_add_origin_requests':0,'legacy_export_read_in_place':True,
        'legacy_receipt_times_preserved':True,'disjoint_old_and_new_dates':2,
        'private_roots_preserved':True,'daily_receipt_quality_archived':True,
        'installed_full_raw_backup_recovery_ok':True,'backup_source_files_unchanged':True,
        'backup_resumes_collector':False,'independent_machine_backup_verified':False,'eta_available':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,required=True)
    check(parser.parse_args().source_root)
