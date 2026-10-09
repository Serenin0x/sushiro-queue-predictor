"""Export committed daily projections after 22:05; never opens an active DB."""
import argparse
from datetime import date, datetime, time as day_time, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import urllib.request
from zoneinfo import ZoneInfo

from sushiwait.monitorhub import read_config

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError('archive_redirect_not_allowed')


def read_private_archive(path):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_ARCHIVE_BYTES):
        raise ValueError('private_archive_required')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_ARCHIVE_BYTES + 1)
        after = os.fstat(stream.fileno())
    named = path.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                             info.st_mode, info.st_uid)
    if (len(raw) > MAX_ARCHIVE_BYTES or len(raw) != before.st_size
            or any(identity(info) != identity(before) for info in (opened, after, named))):
        raise ValueError('archive_changed_during_read')
    return raw


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        raise ValueError('private_archive_directory_required')


def immutable_json(path, value):
    if path.exists() or path.is_symlink():
        read_private_archive(path)
        return 'already_saved'
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError('daily_projection_too_large')
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.archive-', delete=False) as out:
        temporary = Path(out.name)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    try:
        os.link(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink()
    return 'saved'


def export_day(root, config, local_date):
    if date.fromisoformat(local_date).isoformat() != local_date:
        raise ValueError('canonical_date_required')
    output = root / 'daily-exports'
    private_directory(output)
    output = output / local_date
    private_directory(output)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    rows = []
    for worker in config['workers']:
        for store, name in worker['stores'].items():
            try:
                url = worker['endpoint'] + '/api/v1/stores/' + store + '/days/' + local_date
                with opener.open(url, timeout=10) as response:
                    raw = response.read(2097153)
                if len(raw) > 2097152:
                    raise ValueError('daily_projection_too_large')
                value = json.loads(raw)
                if (value['requested_store_id'] != store or value['local_date'] != local_date
                        or value['network_performed_by_read'] is not False):
                    raise ValueError('daily_projection_scope_mismatch')
                path = output / ('store-' + store + '.json')
                state = immutable_json(path, value)
                saved_raw = read_private_archive(path)
                saved = json.loads(saved_raw)
                summary = saved.get('summary')
                points = saved['points']
                complete = not saved['graph_truncated'] and len(points) == (summary['observations'] if summary else 0)
                rows.append({'store_id': store, 'name': name, 'state': state,
                    'points': len(points), 'complete_observed_projection': complete,
                    'sha256': hashlib.sha256(saved_raw).hexdigest(),
                    'daily_quality': summary})
            except Exception as exc:
                rows.append({'store_id': store, 'name': name, 'error_type': type(exc).__name__})
    manifest = {'local_date': local_date, 'exported_at': datetime.now(timezone.utc).isoformat(),
        'stores': rows, 'official_requests_added_by_export': 0,
        'active_database_opened': False, 'independent_backup': False,
        'full_day_source_quality_verified': False}
    immutable_json(output / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--date', action='append', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(args.root).absolute()
    private_directory(root)
    config = read_config(root / 'hub.json')
    deadline = datetime.fromisoformat(config['deadline_at'].replace('Z', '+00:00'))
    days = sorted(set(args.date))
    if len(days) != len(args.date) or not 1 <= len(days) <= 2:
        raise ValueError('one_or_two_explicit_dates_required')
    schedule = []
    for value in days:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError('canonical_date_required')
        target = datetime.combine(day, day_time(22, 5), ZoneInfo('Asia/Shanghai'))
        if target >= deadline:
            raise ValueError('export_outside_original_trial')
        schedule.append((value, target))
    for value, target in schedule:
        while True:
            remaining = (min(target, deadline) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))
        if datetime.now(timezone.utc) >= deadline:
            return
        proof = export_day(root, config, value)
        print(json.dumps({'date': value, 'stores': len(proof['stores']),
            'errors': [row['store_id'] for row in proof['stores'] if 'error_type' in row],
            'official_requests_added': 0}), flush=True)


if __name__ == '__main__':
    main()
