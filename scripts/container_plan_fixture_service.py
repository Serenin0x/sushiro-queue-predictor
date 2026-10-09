"""Container integration fixture: real clocks, synthetic transport, no official requests."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path
import time

from sushiwait.remote import QUEUE_NAMES, RemoteClient
from sushiwait.remoteservice import serve_local
from sushiwait.remotewindow import RemoteWindowService

assert os.getuid() == 10001
counter = Path('/state/synthetic-http-count.json')
starts = Path('/state/synthetic-starts.json')
schedule_starts = Path('/state/synthetic-schedule-starts.json')
dispatch_delays = Path('/state/synthetic-dispatch-delays.json')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output)
    os.replace(temporary, path)


class Response:
    headers = {}
    def __init__(self, request): self.request = request
    def geturl(self): return self.request.full_url
    def getcode(self): return 200
    def close(self): pass
    def read(self, size):
        value = ({name: ['900001'] for name in QUEUE_NAMES}
                 if 'groupqueues?' in self.request.full_url else 1)
        return json.dumps(value).encode()[:size]


class SyntheticOpener:
    def open(self, request, *, timeout):
        assert request.full_url.startswith('https://crm-cn-prd.sushiro.com.cn/api/1.1/remote/')
        if 'groupqueues?' in request.full_url:
            at=time.monotonic()
            pending=json.loads(Path('/state/task.json').read_text())['pending']['started_at']
            delay=(datetime.now(timezone.utc)-datetime.fromisoformat(pending.replace('Z','+00:00'))).total_seconds()
            saved=json.loads(schedule_starts.read_text()) if schedule_starts.exists() else []
            save(schedule_starts,saved+[pending])
            saved=json.loads(dispatch_delays.read_text()) if dispatch_delays.exists() else []
            save(dispatch_delays,saved+[delay])
            values = json.loads(starts.read_text()) if starts.exists() else []
            save(starts, values + [at])
        count = json.loads(counter.read_text()) if counter.exists() else 0
        save(counter, count + 1)
        return Response(request)


service = RemoteWindowService(db='/state/remote.sqlite3', task_file='/state/task.json',
    plan_file='/state/plans.json', plan_updates_file='/plan-updates/current.json',
    store_ids=['900001'], base_interval=300, duration_seconds=121, max_pairs=6,
    resume_if_present=True, client_factory=lambda: RemoteClient(opener=SyntheticOpener()))
serve_local(service, listen_host='0.0.0.0')
