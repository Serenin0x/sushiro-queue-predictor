"""Exercise the installed CLI with real private files and a synthetic 503."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.remote import QUEUE_NAMES, RemoteClient
from sushiwait.remotecampaign import RemoteCampaign, remote_campaign_status


def check():
    os.umask(0o077)
    class Clock:
        seconds=0
        def wall(self):return datetime(2026,10,9,3,tzinfo=timezone.utc)+timedelta(seconds=self.seconds)
        def mono(self):return self.seconds
        def sleep(self,seconds):self.seconds+=seconds
    clock=Clock();calls=[]
    class Response:
        headers={}
        def __init__(self,request,status):self.request,self.status=request,status
        def geturl(self):return self.request.full_url
        def getcode(self):return self.status
        def close(self):pass
        def read(self,size):return json.dumps({n:[] for n in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0).encode()[:size]
    class Opener:
        def open(self,request,*,timeout):
            calls.append((request.full_url,clock.seconds))
            return Response(request,503 if len(calls)==1 else 200)
    def client():return RemoteClient(opener=Opener())
    original=RemoteCampaign.collect
    def collect(self,**kw):kw['client_factory']=client;return original(self,**kw)
    with tempfile.TemporaryDirectory(prefix='sushiwait-installed-recovery-') as tmp:
        parent=Path(tmp).resolve();parent.chmod(0o700);root=parent/'campaign';root.mkdir(mode=0o700)
        plans=parent/'plans.json';plans.write_text('{"schema_version":1,"plans":[]}');plans.chmod(0o600)
        args=['remote-campaign-collect','--root',str(root),'--plan-file',str(plans),
            '--store-id','900001','--base-interval','60','--duration','150',
            '--window-duration','150','--max-pairs','3','--resume-if-present',
            '--transient-recovery-limit','1']
        with patch('sushiwait.cli._utc_clock',clock.wall),patch('sushiwait.remoteintake._clock',clock.wall),\
             patch('sushiwait.remote._utc',lambda:clock.wall().isoformat()),\
             patch.object(RemoteCampaign,'collect',collect),\
             patch('sushiwait.cli.time.monotonic',clock.mono),patch('sushiwait.cli.time.sleep',clock.sleep),\
             redirect_stdout(io.StringIO()):
            assert main(args)==1
            before=remote_campaign_status(root)
            archive={p:p.read_bytes() for p in (root/'window-01').iterdir() if p.is_file()}
            assert main(args)==1
            after=remote_campaign_status(root)
        assert len(calls)==5 and [at for url,at in calls if 'groupqueues?' in url]==[0,60,120]
        assert before['successful_pairs']==2 and before['failed_pairs']==1
        assert before['completed_pair_slots']==3 and before['recorded_http_attempts']==5
        assert before['transient_recoveries_used']==1 and before['windows_archived']==2
        assert before['deadline_at']==after['deadline_at'] and after['maximum_request_budget']==6
        assert {p:p.read_bytes() for p in archive}==archive
    print(json.dumps({'installed_campaign_recovery_ok':True,'actual_installed_cli':True,
        'synthetic_http_attempts':5,'official_http_requests':0,'failed_archive_unchanged':True,
        'original_budget_and_deadline_preserved':True,'failed_query_replayed':False,
        'terminal_resume_added_requests':0,'verified_training_labels':0,'eta_available':False}))


if __name__=='__main__':check()
