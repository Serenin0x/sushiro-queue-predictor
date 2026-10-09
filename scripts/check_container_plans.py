"""Actual Linux containers and hot plan publication; synthetic source, no official requests."""
import argparse
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
import uuid


def check():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--docker', default='docker')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,199}', args.image):
        raise ValueError('invalid_image_reference')
    fixture = Path(__file__).with_name('container_plan_fixture_service.py').resolve()
    prefix = 'sushiwait-plan-smoke-' + uuid.uuid4().hex[:12]
    state_volume, plan_volume = prefix + '-state', prefix + '-plans'
    fixture_image = prefix + ':fixture'
    names = []
    opener = build_opener(ProxyHandler({}))
    started = time.monotonic()

    def run(*arguments, body=None):
        result = subprocess.run([args.docker, *arguments], input=body,
                                capture_output=True, timeout=65)
        if result.returncode:
            raise RuntimeError('container_plan_command_failed: ' + result.stderr.decode()[-1200:])
        return result.stdout.decode().strip()

    def admin(action, name=None, document=None):
        command = ['python', '-I', '/opt/sushiwait/plan_admin.py', action, '--store-id', '900001']
        if name is None:
            arguments = ['run', '--rm', '-i', '--read-only', '--network=none', '--cap-drop=ALL',
                         '--security-opt=no-new-privileges', '--mount',
                         'source=' + plan_volume + ',target=/plan-updates',
                         '--entrypoint', 'python', args.image, *command[1:]]
        else:
            arguments = ['exec', '-i', name, *command]
        value = json.loads(run(*arguments, body=json.dumps(document).encode() if document is not None else None))
        assert value['ok'] and value['worker_application_verified'] is False
        assert value['network_performed'] is False and value['business_operation_performed'] is False
        assert value['credentials_accessed'] is False
        return value

    def start():
        name = prefix + '-' + str(len(names) + 1)
        names.append(name)
        run('run', '-d', '--name', name, '--init', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=256m',
            '--mount', 'source=' + state_volume + ',target=/state',
            '--mount', 'source=' + plan_volume + ',target=/plan-updates',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=1777',
            '-p', '127.0.0.1::8765', '--entrypoint', 'python', fixture_image, '-I', '/fixture.py')
        port = run('port', name, '8765/tcp').split(':')[-1]
        config = json.loads(run('inspect', name))[0]
        assert config['Config']['User'] == '10001:10001'
        assert config['HostConfig']['ReadonlyRootfs'] and config['HostConfig']['CapDrop'] == ['ALL']
        assert all(p['HostIp'] == '127.0.0.1' for p in config['HostConfig']['PortBindings']['8765/tcp'])
        return name, 'http://127.0.0.1:' + port

    def stop(name):
        run('stop', '--time', '45', name)
        assert json.loads(run('inspect', name))[0]['State']['Running'] is False

    def read(url, path):
        try:
            with opener.open(url + path, timeout=3) as response:
                return response.status, json.loads(response.read(32769))
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read(32769))

    def wait(url, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                status, value = read(url, '/api/v1/status')
                if status == 200 and predicate(value):
                    return value
                if status == 200 and value.get('service_state') == 'failed':
                    raise RuntimeError('container_plan_service_failed')
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(.2)
        raise RuntimeError('container_plan_service_wait_expired')

    def checkpoint(name, revision):
        # This CLI reads only the atomic task file, never the running database.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = json.loads(run('exec', name, 'sushiwait', 'remote-window-status',
                                   '--task-file', '/state/task.json'))
            if value.get('accepted_plan_revision') == revision:
                return value
            time.sleep(.2)
        raise RuntimeError('container_plan_application_wait_expired')

    def counter(name):
        return json.loads(run('exec', name, 'python', '-I', '-c',
                             "import pathlib; print((pathlib.Path('/state')/'synthetic-http-count.json').read_text())"))

    def terminal_state():
        # Called only after our writer has actually stopped; mount is read-only and network disabled.
        code = ("import pathlib,json,hashlib,os; p=pathlib.Path('/state'); "
                "print(json.dumps({'counter':json.loads((p/'synthetic-http-count.json').read_text()),"
                "'starts':json.loads((p/'synthetic-starts.json').read_text()),"
                "'schedule_starts':json.loads((p/'synthetic-schedule-starts.json').read_text()),"
                "'dispatch_delays':json.loads((p/'synthetic-dispatch-delays.json').read_text()),"
                "'task':json.loads((p/'task.json').read_text()),"
                "'hashes':{n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in "
                "['remote.sqlite3','task.json','plans.json']},"
                "'modes':{n:oct((p/n).stat().st_mode & 511) for n in "
                "['remote.sqlite3','task.json','plans.json']},"
                "'parent_mode':oct(p.stat().st_mode & 511),'uid':os.getuid()}))")
        return json.loads(run('run', '--rm', '--read-only', '--network=none', '--cap-drop=ALL',
                              '--security-opt=no-new-privileges', '--mount',
                              'source=' + state_volume + ',target=/state,readonly',
                              '--entrypoint', 'python', args.image, '-I', '-c', code))

    def publish(name, minutes, count):
        arrival = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        return admin('publish', name=name, document={'schema_version': 1, 'plans':
                     [{'store_id': '900001', 'desired_arrival_at': arrival} for _ in range(count)]})

    def until(origin, seconds):
        while True:
            remaining = seconds - (time.monotonic() - origin)
            if remaining <= 0:
                return
            time.sleep(min(.2, remaining))

    try:
        context = io.BytesIO()
        with tarfile.open(fileobj=context, mode='w') as archive:
            for name, body in [('Dockerfile', ('FROM ' + args.image + '\nCOPY fixture.py /fixture.py\n').encode()),
                               ('fixture.py', fixture.read_bytes())]:
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(body), 0o644
                archive.addfile(entry, io.BytesIO(body))
        built = subprocess.run([args.docker, 'build', '-t', fixture_image, '-'],
                               input=context.getvalue(), capture_output=True, timeout=65)
        if built.returncode:
            raise RuntimeError('container_plan_fixture_build_failed: ' + built.stderr.decode()[-1200:])
        run('volume', 'create', state_volume)
        run('volume', 'create', plan_volume)
        initial = admin('init')
        assert initial['revision'] == 1 and initial['committed'] is True
        assert admin('init')['committed'] is False and admin('status')['revision'] == 1
        first, url = start()
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 1)
        origin = time.monotonic()
        assert read(url, '/health')[0] == 200
        before_reads = counter(first)
        for _ in range(5):
            code, view = read(url, '/api/v1/stores/900001/queue')
            assert code == 200 and view['eta_available'] is False
            assert view['network_performed_by_read'] is False
        assert before_reads == counter(first) == 2
        until(origin, 10)
        assert publish(first, 20, 2)['revision'] == 2
        checkpoint(first, 2)
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 2, seconds=65)
        until(origin, 70)
        assert publish(first, 14, 1)['revision'] == 3
        checkpoint(first, 3)
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 3, seconds=40)
        until(origin, 100)
        assert admin('clear', name=first)['revision'] == 4
        checkpoint(first, 4)
        completed = wait(url, lambda v: v.get('service_state') == 'completed', seconds=30)
        assert read(url, '/health')[0] == 503 and completed['worker_alive'] is False
        stop(first)
        before = terminal_state()
        task = before['task']
        assert task['schema_version'] == 3 and task['state'] == 'completed'
        assert task['end_reason'] == 'deadline' and task['successful'] == 3
        assert task['plan_context']['revision'] == 4 and task['plan_context']['unobserved_revisions'] == 0
        assert task['config']['max_pairs'] == 6 and task['config']['duration_seconds'] == 121
        assert before['counter'] == 6 and len(before['starts']) == 3
        intervals = [later - earlier for earlier, later in zip(before['starts'], before['starts'][1:])]
        scheduled=[datetime.fromisoformat(s.replace('Z','+00:00')) for s in before['schedule_starts']]
        scheduled_intervals=[(b-a).total_seconds() for a,b in zip(scheduled,scheduled[1:])]
        delays=before['dispatch_delays']
        print(json.dumps({'container_plan_timing_diagnostic':True,
            'scheduled_start_intervals_seconds':scheduled_intervals,
            'transport_start_intervals_seconds':intervals,'dispatch_delays_seconds':delays}),flush=True)
        # The gate is before checkpoint persistence. Different fsync delays
        # can make consecutive transport entries slightly closer than 60/30s.
        # Check the original gate strictly and transport drift separately.
        assert len(scheduled)==len(delays)==3
        assert 59.99 <= scheduled_intervals[0] < 65 and 29.99 <= scheduled_intervals[1] < 35
        assert all(0<=delay<1 for delay in delays)
        assert 59<=intervals[0]<66 and 29<=intervals[1]<36
        assert admin('clear')['revision'] == 5
        second, url = start()
        terminal = wait(url, lambda v: v.get('service_state') == 'completed')
        assert read(url, '/health')[0] == 503 and terminal['worker_alive'] is False
        code, view = read(url, '/api/v1/stores/900001/queue')
        assert code == 200 and view['fields']['groupqueues']['state'] == 'saved_history'
        assert terminal['task']['accepted_plan_revision'] == 4
        stop(second)
        after = terminal_state()
        assert after['counter'] == 6 and after['hashes'] == before['hashes']
        assert after['task']['deadline_at'] == task['deadline_at']
        assert after['uid'] == 10001 and after['parent_mode'] == '0o700'
        assert set(after['modes'].values()) == {'0o600'}
        print(json.dumps({'ok': True, 'is_live': False, 'upstream_transport': 'synthetic',
            'official_http_requests': 0, 'synthetic_pairs': 3, 'synthetic_http_attempts': 6,
            'worker_accepted_revisions': [1, 2, 3, 4], 'terminal_feed_revision': 5,
            'terminal_task_revision': 4, 'same_store_plan_count': 2,
            'initial_reader_count': 5, 'initial_reader_added_upstream_requests': 0,
            'original_deadline_preserved': True, 'original_budget_preserved': True,
            'terminal_resume_unchanged': True, 'non_root_uid': 10001, 'read_only_root': True,
            'host_bind': '127.0.0.1', 'actual_start_intervals_seconds': [round(x, 3) for x in intervals],
            'saved_schedule_intervals_seconds':[round(x,3) for x in scheduled_intervals],
            'dispatch_delays_seconds':[round(x,4) for x in delays],
            'elapsed_seconds': round(time.monotonic() - started, 3)}))
    finally:
        for name in names:
            subprocess.run([args.docker, 'rm', '-f', name], capture_output=True, timeout=50)
        for volume in (state_volume, plan_volume):
            subprocess.run([args.docker, 'volume', 'rm', volume], capture_output=True, timeout=20)
        subprocess.run([args.docker, 'image', 'rm', fixture_image], capture_output=True, timeout=20)


if __name__ == '__main__':
    check()
