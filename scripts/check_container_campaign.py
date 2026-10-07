"""Actual campaign rollover and container restart; synthetic upstream, real clocks."""
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
    fixture = Path(__file__).with_name('container_campaign_fixture_service.py').resolve()
    prefix = 'sushiwait-campaign-smoke-' + uuid.uuid4().hex[:12]
    volumes = [prefix + '-state', prefix + '-plans']
    fixture_image = prefix + ':fixture'
    names = []
    opener = build_opener(ProxyHandler({}))
    started = time.monotonic()

    def run(*arguments, body=None):
        result = subprocess.run([args.docker, *arguments], input=body, capture_output=True, timeout=65)
        if result.returncode:
            raise RuntimeError('container_campaign_command_failed: ' + result.stderr.decode()[-1200:])
        return result.stdout.decode().strip()

    def admin(action, name=None, document=None):
        command = ['python', '-I', '/opt/sushiwait/plan_admin.py', action, '--store-id', '900001']
        arguments = (['exec', '-i', name, *command] if name else
            ['run', '--rm', '-i', '--read-only', '--network=none', '--cap-drop=ALL',
             '--security-opt=no-new-privileges', '--mount', 'source=' + volumes[1] + ',target=/plan-updates',
             '--entrypoint', 'python', args.image, *command[1:]])
        value = json.loads(run(*arguments, body=json.dumps(document).encode() if document else None))
        assert value['ok'] and value['worker_application_verified'] is False
        assert value['network_performed'] is False and value['credentials_accessed'] is False
        return value

    def start():
        name = prefix + '-' + str(len(names) + 1)
        names.append(name)
        run('run', '-d', '--name', name, '--init', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=256m',
            '--mount', 'source=' + volumes[0] + ',target=/state',
            '--mount', 'source=' + volumes[1] + ',target=/plan-updates',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=1777',
            '-p', '127.0.0.1::8765', '--entrypoint', 'python', fixture_image, '-I', '/fixture.py')
        port = run('port', name, '8765/tcp').split(':')[-1]
        config = json.loads(run('inspect', name))[0]
        assert config['Config']['User'] == '10001:10001' and config['HostConfig']['ReadonlyRootfs']
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
                    raise RuntimeError('container_campaign_service_failed')
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(.2)
        raise RuntimeError('container_campaign_wait_expired')

    def counter(name):
        return json.loads(run('exec', name, 'python', '-I', '-c',
                             "import pathlib; print((pathlib.Path('/state')/'synthetic-http-count.json').read_text())"))

    def terminal_state():
        # Our writer must have stopped. This helper has no network, only a read-only volume.
        code = ("import pathlib,json,hashlib,os; p=pathlib.Path('/state'); r=p/'campaign'; "
            "print(json.dumps({'counter':json.loads((p/'synthetic-http-count.json').read_text()),"
            "'starts':json.loads((p/'synthetic-starts.json').read_text()),"
            "'campaign':json.loads((r/'campaign.json').read_text()),"
            "'hashes':{str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() "
            "for f in list(r.rglob('*'))+[p/'plans.json'] if f.is_file()},"
            "'modes':{str(f.relative_to(p)):oct(f.stat().st_mode & 511) "
            "for f in list(r.rglob('*'))+[r] },'uid':os.getuid()}))")
        return json.loads(run('run', '--rm', '--read-only', '--network=none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--mount', 'source=' + volumes[0] + ',target=/state,readonly',
            '--entrypoint', 'python', args.image, '-I', '-c', code))

    def publish(name, minutes, count):
        arrival = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        return admin('publish', name, {'schema_version': 1, 'plans':
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
            raise RuntimeError('container_campaign_fixture_build_failed: ' + built.stderr.decode()[-1200:])
        for volume in volumes:
            run('volume', 'create', volume)
        assert admin('init')['revision'] == 1
        first, url = start()
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 1)
        origin = time.monotonic()
        assert read(url, '/health')[0] == 200
        before_reads = counter(first)
        for _ in range(5):
            status, view = read(url, '/api/v1/stores/900001/queue')
            assert status == 200 and view['eta_available'] is False
        assert before_reads == counter(first) == 2
        assert publish(first, 20, 2)['revision'] == 2
        original = read(url, '/api/v1/status')[1]['task']
        until(origin, 5)
        stop(first)
        second, url = start()
        resumed = wait(url, lambda v: v.get('service_state') == 'running')
        assert resumed['task']['deadline_at'] == original['deadline_at']
        assert resumed['task']['maximum_pair_budget'] == 8
        assert counter(second) == 2
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 2, seconds=75)
        assert publish(second, 14, 1)['revision'] == 3
        wait(url, lambda v: v.get('task', {}).get('successful_pairs') == 3, seconds=40)
        completed = wait(url, lambda v: v.get('service_state') == 'completed', seconds=65)
        assert read(url, '/health')[0] == 503 and completed['worker_alive'] is False
        assert completed['task']['windows_created'] == completed['task']['windows_archived'] == 3
        assert completed['task']['accepted_plan_revision'] == 3
        stop(second)
        before = terminal_state()
        campaign = before['campaign']
        assert campaign['state'] == 'completed' and campaign['end_reason'] == 'deadline'
        assert campaign['deadline_at'] == original['deadline_at']
        assert campaign['config']['duration_seconds'] == 141 and campaign['config']['max_pairs'] == 8
        assert before['counter'] == 8 and len(before['starts']) == 4
        intervals = [b - a for a, b in zip(before['starts'], before['starts'][1:])]
        assert 64 <= intervals[0] < 85, f'campaign_restart_interval_out_of_range: {intervals!r}'
        assert all(29.99 <= interval < 35 for interval in intervals[1:]), f'campaign_poll_intervals_out_of_range: {intervals!r}'
        assert all(e['checkpoint_digest'] and e['database_digest'] for e in campaign['windows'])
        assert admin('clear')['revision'] == 4
        third, url = start()
        terminal = wait(url, lambda v: v.get('service_state') == 'completed')
        assert terminal['task']['accepted_plan_revision'] == 3 and terminal['worker_alive'] is False
        assert read(url, '/health')[0] == 503
        status, view = read(url, '/api/v1/stores/900001/queue')
        assert status == 200 and view['fields']['groupqueues']['state'] == 'saved_history'
        stop(third)
        after = terminal_state()
        assert after['counter'] == 8 and after['hashes'] == before['hashes']
        assert after['uid'] == 10001
        assert all(mode == ('0o600' if '.' in Path(name).name else '0o700') for name, mode in after['modes'].items())
        print(json.dumps({'ok': True, 'is_live': False, 'upstream_transport': 'synthetic',
            'official_http_requests': 0, 'synthetic_pairs': 4, 'synthetic_http_attempts': 8,
            'actual_containers_created': 3, 'windows_archived': 3,
            'initial_reader_count': 5, 'initial_reader_added_upstream_requests': 0,
            'campaign_deadline_preserved': True, 'campaign_budget_preserved': True,
            'terminal_resume_unchanged': True, 'terminal_feed_revision': 4, 'accepted_revision': 3,
            'actual_start_intervals_seconds': [round(interval, 3) for interval in intervals],
            'non_root_uid': 10001, 'host_bind': '127.0.0.1',
            'elapsed_seconds': round(time.monotonic() - started, 3)}))
    finally:
        for name in names:
            subprocess.run([args.docker, 'rm', '-f', name], capture_output=True, timeout=50)
        for volume in volumes:
            subprocess.run([args.docker, 'volume', 'rm', volume], capture_output=True, timeout=20)
        subprocess.run([args.docker, 'image', 'rm', fixture_image], capture_output=True, timeout=20)


if __name__ == '__main__':
    check()
