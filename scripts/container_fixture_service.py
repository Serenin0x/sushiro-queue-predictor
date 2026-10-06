"""ONLY for container smoke checks: synthetic transport, no official network requests."""
import json,os
from pathlib import Path
from sushiwait.remote import RemoteClient,QUEUE_NAMES
from sushiwait.remotewindow import RemoteWindowService
from sushiwait.remoteservice import serve_local

assert os.getuid()==10001
counter=Path('/state/synthetic-http-count.json')

class Response:
    headers={}
    def __init__(self,request):self.request=request
    def geturl(self):return self.request.full_url
    def getcode(self):return 200
    def close(self):pass
    def read(self,size):
        value={name:['900001'] for name in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 1
        return json.dumps(value).encode()[:size]

class SyntheticOpener:
    def open(self,request,*,timeout):
        assert request.full_url.startswith('https://crm-cn-prd.sushiro.com.cn/api/1.1/remote/')
        value=json.loads(counter.read_text()) if counter.exists() else 0
        temporary=counter.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as output:json.dump(value+1,output)
        os.replace(temporary,counter)
        return Response(request)

service=RemoteWindowService(db='/state/remote.sqlite3',task_file='/state/task.json',
    plan_file='/state/plans.json',store_ids=['900001'],base_interval=60,duration_seconds=240,
    max_pairs=2,resume_if_present=True,client_factory=lambda:RemoteClient(opener=SyntheticOpener()))
serve_local(service,listen_host='0.0.0.0')
