"""Installed public CLI / persistent files with a synthetic source and clock.

No official requests, credentials, or synthetic observations claimed as real.
The same check runs in a network-disabled, unprivileged Linux container.
"""
from contextlib import redirect_stdout
from datetime import datetime,timedelta,timezone
import io,json,os
from pathlib import Path
import sys,tempfile
from unittest.mock import patch

from sushiwait.businesshours import read_hours
from sushiwait.cli import main
from sushiwait.dailyview import DailyView
from sushiwait.remote import QUEUE_NAMES, RemoteClient, RemoteStore
from sushiwait.remotecampaign import remote_campaign_status

class Clock:
    base=datetime(2026,10,9,13,59,59,tzinfo=timezone.utc)
    seconds=0
    def wall(self):return self.base+timedelta(seconds=self.seconds)
    def mono(self):return self.seconds
    def sleep(self,seconds):
        if 2<=self.seconds<45001:self.seconds=45001 # next natural Saturday 10:30
        else:self.seconds+=seconds

class Response:
    headers={}
    def __init__(self,request):self.request=request
    def geturl(self):return self.request.full_url
    def getcode(self):return 200
    def close(self):pass
    def read(self,size):return json.dumps({n:['900001','900002','900003','900004'] for n in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 7).encode()[:size]
class Opener:
    def __init__(self,clock):self.clock=clock;self.calls=[]
    def open(self,request,*,timeout):
        self.calls.append(request.full_url)
        if len(self.calls)==1:self.clock.seconds+=2
        return Response(request)

def check(rules_file):
    os.umask(0o077);rules=read_hours(rules_file);clock=Clock();opener=Opener(clock)
    with tempfile.TemporaryDirectory(prefix='sushiwait-installed-business-') as tmp:
        parent=Path(tmp).resolve();parent.chmod(0o700);root=parent/'campaign';root.mkdir(mode=0o700)
        plans=parent/'plans.json';plans.write_text('{"schema_version":1,"plans":[]}');plans.chmod(0o600)
        args=['remote-campaign-collect','--root',str(root),'--plan-file',str(plans),
            '--business-hours-file',str(rules_file),'--store-id','900001','--base-interval','60',
            '--duration','90000','--window-duration','86400','--max-pairs','2','--resume-if-present']
        def client():return RemoteClient(opener=opener)
        with patch('sushiwait.cli._utc_clock',clock.wall),patch('sushiwait.remoteintake._clock',clock.wall),patch('sushiwait.remote._utc',lambda:clock.wall().isoformat()),\
                patch('sushiwait.remotecampaign.RemoteClient',side_effect=client):
            # The factory is a function default captured at definition time.
            from sushiwait.remotecampaign import RemoteCampaign
            original=RemoteCampaign.collect
            def collect(self,**kw):kw['client_factory']=client;return original(self,**kw)
            with patch.object(RemoteCampaign,'collect',collect),patch('sushiwait.cli.time.monotonic',clock.mono),patch('sushiwait.cli.time.sleep',clock.sleep),redirect_stdout(io.StringIO()):
                assert main(args)==0
                before=remote_campaign_status(root);count=len(opener.calls)
                assert main(args)==0
                after=remote_campaign_status(root)
        assert count==len(opener.calls)==3
        assert before['successful_pairs']==1 and before['scheduled_pause_slots']==1 and not before['failed_pairs']
        assert before['completed_pair_slots']==2 and before['recorded_http_attempts']==3
        assert after['deadline_at']==before['deadline_at'] and after['maximum_request_budget']==4
        view=DailyView(['900001'],hours=rules,base_interval=60)
        for path in sorted(root.glob('window-*/remote.sqlite3')):
            with RemoteStore(path,read_only=True) as db:view.restore(db)
        index=view.index('900001',now=clock.wall());assert len(index['days'])==2
        assert index['days'][0]['scheduled_pause_slots']==1 and index['days'][1]['successful_pairs']==1
        detail=view.detail('900001','2026-10-10',now=clock.wall())
        assert detail['points'][0]['count_raw']==7 and not detail['network_performed_by_read']
        assert detail['points'][0]['queues']['storeQueue']==['900001','900002','900003']
    from importlib.resources import files
    for name in ('statistics.html','statistics.css','statistics.js'):
        assert len(files('sushiwait').joinpath('web',name).read_bytes())>100
    print(json.dumps({'installed_business_hours_ok':True,'actual_installed_cli':True,
        'synthetic_http_attempts':3,'official_http_requests':0,'credentials_accessed':0,
        'closing_boundary_second_get_blocked':True,'next_day_resumed':True,
        'original_budget_and_deadline_preserved':True,'daily_index_dates':2,
        'statistics_assets_installed':True,'verified_training_labels':0,'eta_available':False}))

if __name__=='__main__':check(Path(sys.argv[1]).resolve())
