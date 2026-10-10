"""Private, explicit closed-day data backups; never starts or resumes a writer.

The zip includes the full anonymous databases, not just the graph's three
labels. Restored checkpoints retain original paths and are evidence only.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from uuid import UUID, uuid4
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED, BadZipFile
from zoneinfo import ZoneInfo

from .capture import _open_parent, _check_parent
from .dailyarchive import read_day, read_legacy_day, selected_date
from .dailycontroller import read_daily_archive
from .remote import SOURCE, RemoteStore, MAX_RECORD, _id, _time, validate_record
from .remoteintake import read_receipt
from .remotecampaign import _decode_campaign, _summary as _window_summary
from .remotewindow import _decode_window
from .remotetasks import _json, _now, _EMPTY, _chain

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 2 * 1024**3
MAX_MANIFEST = 2 * 1024 * 1024
MAX_MEMBERS = 8192
CHUNK = 128 * 1024
ZONE = ZoneInfo('Asia/Shanghai')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_MEMBER = re.compile(r'(?:store-([1-9][0-9]{0,18})/(20[0-9]{2}-[0-9]{2}-[0-9]{2})/'
    r'(?:projection\.json|result\.json|quality\.json|campaign/campaign\.json|'
    r'campaign/window-[0-9]{2}/(?:task\.json|remote\.sqlite3))|'
    r'legacy-exports/(20[0-9]{2}-[0-9]{2}-[0-9]{2})/(?:manifest\.json|store-([1-9][0-9]{0,18})\.json))\Z')


class BackupError(ValueError):
    pass


def _clock(now):
    now = datetime.now(timezone.utc) if now is None else now
    _now(now)
    return now.astimezone(timezone.utc)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


@contextmanager
def _file(path, maximum=MAX_FILE, minimum=1):
    """Fixed descriptor, no symlinks, no shared hard links, stable whole read."""
    parent, name = _open_parent(Path(path), private=True)
    fd = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not minimum <= before.st_size <= maximum):
            raise BackupError('backup_unsafe_file')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if _identity(os.fstat(fd)) != _identity(before):
            raise BackupError('backup_source_changed')
        yield fd, before.st_size
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(named) != _identity(before) or _identity(os.fstat(fd)) != _identity(before):
            raise BackupError('backup_source_changed')
        _check_parent(Path(path), parent, private=True)
    finally:
        if fd is not None: os.close(fd)
        os.close(parent)


@contextmanager
def _writer_stopped(path):
    """Take the existing writer lock without creating, altering or deleting it."""
    import fcntl
    with _file(Path(str(path)+'.lock'), maximum=16384, minimum=0) as (fd, _):
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('backup_source_writer_active') from None
        try: yield
        finally: fcntl.flock(fd, fcntl.LOCK_UN)


def _scope(root, stores, day, kind):
    root = Path(os.path.abspath(root))
    selected_date(day)
    if (kind not in ('daily', 'legacy') or type(stores) is not list
            or not 1 <= len(stores) <= (256 if kind == 'daily' else 16)
            or len(set(stores)) != len(stores)):
        raise BackupError('backup_invalid_scope')
    for store in stores: _id(store)
    return root, sorted(stores, key=int)


def _closed(projection, now):
    summary = projection['summary']
    if (summary is None or not summary['declared_intervals']
            or max(_time(span[1]) for span in summary['declared_intervals']) > now
            or _time(projection['generated_at']) > now):
        raise BackupError('backup_day_not_closed')


def _window(path, campaign, entry):
    body = read_daily_archive(path)
    task = _decode_window(body)
    c = campaign['config']
    expected = {'db': str(Path(c['root'])/entry['name']/'remote.sqlite3'),
        'plan_file': c['plan_file'], 'plan_digest': c['plan_digest'],
        'store_ids': c['store_ids'], 'base_interval': c['base_interval'],
        'duration_seconds': entry['duration_seconds'], 'max_pairs': entry['max_pairs']}
    if c['plan_updates_file'] is not None: expected['plan_updates_file'] = c['plan_updates_file']
    if 'business_hours' in c: expected['business_hours'] = c['business_hours']
    if (task['config'] != expected or task['created_at'] != entry['created_at']
            or task['updated_at'] != entry['updated_at'] or task['starts'] != entry['starts']
            or _window_summary(task) != entry['summary'] or task['pending'] is not None
            or task['initial_id'] != 0):
        raise BackupError('backup_checkpoint_conflict')
    return task, hashlib.sha256(body).hexdigest()


def _raw_counts(path, store, day, task):
    with _file(path) as (fd, _):
        counts = _database_counts(path, store, day, task)
        digest = hashlib.sha256()
        while chunk := os.read(fd, CHUNK): digest.update(chunk)
    return *counts, digest.hexdigest()


def _database_counts(path, store, day, task):
    total = selected = attempts = successes = pauses = 0
    digest = _EMPTY; previous = None
    with RemoteStore(path, read_only=True) as db:
        if db.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise BackupError('backup_database_invalid')
        rows = db.db.execute('SELECT id,run_id,store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
            'THEN payload_json END FROM remote_samples ORDER BY id LIMIT 1502', (MAX_RECORD,))
        for identifier, run, stored_store, ok, text in rows:
            total += 1
            if total > 1500 or identifier != total:
                raise BackupError('backup_database_invalid')
            if str(UUID(run)) != run: raise BackupError('backup_database_invalid')
            stored = _json(text)
            record = validate_record(stored)
            read_receipt(stored, record, run)
            expected = dict(record)
            if 'local_intake' in stored: expected['local_intake'] = stored['local_intake']
            if stored != expected: raise BackupError('backup_database_invalid')
            if record['requested_store_id'] != store or stored_store != store or ok != int(record['ok']):
                raise BackupError('backup_database_scope_mismatch')
            started = _time(record['queries']['groupqueues']['started_at'])
            received = [_time(q['received_at']) for q in record['queries'].values() if q['received_at'] is not None]
            if (not _time(task['created_at']) <= started < _time(task['deadline_at'])
                    or started > _time(task['updated_at'])
                    or any(at > _time(task['updated_at']) for at in received)
                    or previous is not None and started < previous):
                raise BackupError('backup_checkpoint_conflict')
            previous = started
            digest = _chain(digest, (identifier, run, stored_store, ok, text))
            attempts += sum(q['attempted'] for q in record['queries'].values())
            successes += int(record['ok'])
            pauses += int('business_hours' in task['config'] and any(
                q['error_code'] == 'business_window_closed' for q in record['queries'].values()))
            selected += int(started.astimezone(ZONE).date().isoformat() == day)
        if (total != task['successful']+task['failed']+task.get('scheduled_pauses', 0)
                or successes != task['successful'] or pauses != task.get('scheduled_pauses', 0)
                or digest != task['records_digest'] or attempts != task['recorded_http_attempts']):
            raise BackupError('backup_checkpoint_conflict')
    return total, selected


def _append(archive, path, name, entries):
    if len(entries) >= MAX_MEMBERS-1:
        raise BackupError('backup_too_many_files')
    digest = hashlib.sha256()
    with _file(path) as (fd, size):
        if sum(e['size_bytes'] for e in entries) + size > MAX_TOTAL:
            raise BackupError('backup_too_large')
        with archive.open(name, 'w', force_zip64=True) as target:
            count = 0
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk: break
                count += len(chunk); digest.update(chunk); target.write(chunk)
                if count > size: raise BackupError('backup_source_changed')
            if count != size: raise BackupError('backup_source_changed')
    entry = {'path': name, 'size_bytes': size, 'sha256': digest.hexdigest()}
    entries.append(entry)
    return entry


def _summary(manifest, *, restored=False):
    return {'ok': True, 'backup_schema_version': 1, 'local_date': manifest['local_date'],
        'stores': len(manifest['store_ids']), 'files': len(manifest['files']),
        'raw_records': sum(r['raw_records'] for r in manifest['rows']),
        'selected_day_records': sum(r['selected_day_records'] for r in manifest['rows']),
        'full_raw_databases_included': True, 'original_times_preserved': True,
        'network_performed': False, 'official_requests_added': 0,
        'independent_machine_verified': False, 'restored_readable': restored,
        'collector_started_or_resumed': False, 'actual_call_verified': False, 'eta_available': False}


def create_backup(*, root, store_ids, day, output, source_kind='daily', now=None):
    """One explicit date; all selected stores must be coherent, no partial zip."""
    now = _clock(now); root, stores = _scope(root, store_ids, day, source_kind)
    output = Path(os.path.abspath(output))
    if output == root or root in output.parents or output in root.parents:
        raise BackupError('backup_destination_overlaps_source')
    parent, name = _open_parent(output, private=True)
    temporary = '.backup-'+uuid4().hex
    published = False
    try:
        if os.path.lexists(output): raise BackupError('backup_destination_exists')
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        entries = []; rows = []
        with os.fdopen(descriptor, 'w+b') as stream:
            with ZipFile(stream, 'w', compression=ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                if source_kind == 'legacy':
                    _append(archive, root/'daily-exports'/day/'manifest.json',
                        f'legacy-exports/{day}/manifest.json', entries)
                for store in stores:
                    folder = root/('store-'+store)/day
                    prefix = f'store-{store}/{day}'
                    campaign = folder/'campaign' if source_kind == 'daily' else root/('store-'+store)/'campaign'
                    with ExitStack() as locks:
                        locks.enter_context(_writer_stopped(campaign/'campaign.json'))
                        checkpoint = _decode_campaign(read_daily_archive(campaign/'campaign.json'))
                        if checkpoint['config']['store_ids'] != [store] or any(w['summary']['pending'] for w in checkpoint['windows']):
                            raise BackupError('backup_source_not_committed')
                        if source_kind == 'daily' and checkpoint['state'] != 'completed':
                            raise BackupError('backup_day_not_closed')
                        projection = (read_day(root/('store-'+store), store, day) if source_kind == 'daily'
                            else read_legacy_day(root/'daily-exports', store, day))
                        _closed(projection, now)
                        expected = projection['summary']['observations']
                        if source_kind == 'daily':
                            for base in ('projection.json', 'result.json'):
                                _append(archive, folder/base, prefix+'/'+base, entries)
                            if (folder/'quality.json').exists():
                                _append(archive, folder/'quality.json', prefix+'/quality.json', entries)
                        else:
                            _append(archive, root/'daily-exports'/day/('store-'+store+'.json'),
                                f'legacy-exports/{day}/store-{store}.json', entries)
                        _append(archive, campaign/'campaign.json', prefix+'/campaign/campaign.json', entries)
                        total = selected = 0
                        for window in checkpoint['windows']:
                            sub = campaign/window['name']; target = prefix+'/campaign/'+window['name']
                            locks.enter_context(_writer_stopped(sub/'task.json'))
                            task, task_digest = _window(sub/'task.json', checkpoint, window)
                            task_entry = _append(archive, sub/'task.json', target+'/task.json', entries)
                            n, n_day, raw_digest = _raw_counts(sub/'remote.sqlite3', store, day, task)
                            data_entry = _append(archive, sub/'remote.sqlite3', target+'/remote.sqlite3', entries)
                            if task_entry['sha256'] != task_digest or data_entry['sha256'] != raw_digest:
                                raise BackupError('backup_source_changed')
                            if window['checkpoint_digest'] is not None and (task_entry['sha256'] != window['checkpoint_digest']
                                    or data_entry['sha256'] != window['database_digest']):
                                raise BackupError('backup_archive_digest_mismatch')
                            total += n; selected += n_day
                        if selected != expected or source_kind == 'daily' and total != selected:
                            raise BackupError('backup_projection_database_mismatch')
                        rows.append({'store_id': store, 'raw_records': total,
                            'selected_day_records': selected, 'observed_points': projection['returned_graph_points']})
                manifest = {'backup_schema_version': 1, 'source': SOURCE, 'source_kind': source_kind,
                    'local_date': day, 'store_ids': stores, 'created_at': _now(now), 'files': entries, 'rows': rows,
                    'full_raw_databases_included': True, 'atomic_multi_store_snapshot': False,
                    'network_performed': False, 'official_requests_added': 0,
                    'independent_machine_verified': False, 'collector_resume_allowed': False}
                raw = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
                if len(raw) > MAX_MANIFEST: raise BackupError('backup_manifest_too_large')
                archive.writestr('manifest.json', raw)
            stream.flush(); os.fsync(stream.fileno())
        _check_parent(output, parent, private=True)
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        published = True
        os.unlink(temporary, dir_fd=parent); os.fsync(parent)
        return _summary(manifest)
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, BadZipFile, sqlite3.Error):
        if published: raise BackupError('backup_saved_durability_unconfirmed') from None
        raise BackupError('backup_create_failed') from None
    finally:
        try: os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError: pass
        os.close(parent)


def _manifest(archive):
    info = archive.infolist()
    names = [item.filename for item in info]
    if not 1 < len(names) <= MAX_MEMBERS or len(set(names)) != len(names) or names.count('manifest.json') != 1:
        raise BackupError('backup_invalid_members')
    for item in info:
        mode = item.external_attr >> 16
        if (item.is_dir() or item.flag_bits & 1 or item.compress_type not in (ZIP_DEFLATED, ZIP_STORED)
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or not 1 <= item.file_size <= (MAX_MANIFEST if item.filename == 'manifest.json' else MAX_FILE)):
            raise BackupError('backup_invalid_members')
    if sum(i.file_size for i in info) > MAX_TOTAL+MAX_MANIFEST:
        raise BackupError('backup_too_large')
    value = _json(archive.read('manifest.json'))
    if (type(value) is not dict or set(value) != {'backup_schema_version','source','source_kind',
            'local_date','store_ids','created_at','files','rows','full_raw_databases_included',
            'atomic_multi_store_snapshot','network_performed','official_requests_added',
            'independent_machine_verified','collector_resume_allowed'}
            or type(value['backup_schema_version']) is not int or value['backup_schema_version'] != 1
            or value['source'] != SOURCE or value['full_raw_databases_included'] is not True
            or value['atomic_multi_store_snapshot'] is not False or value['network_performed'] is not False
            or type(value['official_requests_added']) is not int or value['official_requests_added'] != 0
            or value['independent_machine_verified'] is not False or value['collector_resume_allowed'] is not False):
        raise BackupError('backup_invalid_manifest')
    _, stores = _scope('/', value['store_ids'], value['local_date'], value['source_kind'])
    if stores != value['store_ids']: raise BackupError('backup_invalid_manifest')
    _time(value['created_at'])
    files = value['files']
    if type(files) is not list or len(files) != len(names)-1:
        raise BackupError('backup_invalid_manifest')
    seen = set()
    for row in files:
        if (type(row) is not dict or set(row) != {'path','size_bytes','sha256'}
                or type(row['path']) is not str or not (match := _MEMBER.fullmatch(row['path']))
                or row['path'] in seen or type(row['size_bytes']) is not int
                or not 1 <= row['size_bytes'] <= MAX_FILE or type(row['sha256']) is not str
                or not _DIGEST.fullmatch(row['sha256'])):
            raise BackupError('backup_invalid_manifest')
        if (match[1] or match[4]) not in (None, *stores) or (match[2] or match[3]) != value['local_date']:
            raise BackupError('backup_invalid_manifest')
        if row['path'] not in names or archive.getinfo(row['path']).file_size != row['size_bytes']:
            raise BackupError('backup_invalid_manifest')
        seen.add(row['path'])
    if seen != set(names)-{'manifest.json'}:
        raise BackupError('backup_invalid_manifest')
    if type(value['rows']) is not list or [r.get('store_id') for r in value['rows'] if type(r) is dict] != stores:
        raise BackupError('backup_invalid_manifest')
    for row in value['rows']:
        if (set(row) != {'store_id','raw_records','selected_day_records','observed_points'}
                or any(type(row[k]) is not int or not 0 <= row[k] <= 1500 for k in ('raw_records','selected_day_records','observed_points'))):
            raise BackupError('backup_invalid_manifest')
    return value


def _validate_restored(root, manifest):
    day = manifest['local_date']; files = {e['path'] for e in manifest['files']}
    required = set()
    for row in manifest['rows']:
        store = row['store_id']; prefix = f'store-{store}/{day}'
        folder = root/prefix; campaign = folder/'campaign'
        checkpoint = _decode_campaign(read_daily_archive(campaign/'campaign.json'))
        required.add(prefix+'/campaign/campaign.json')
        if checkpoint['config']['store_ids'] != [store] or any(w['summary']['pending'] for w in checkpoint['windows']):
            raise BackupError('backup_invalid_manifest')
        if manifest['source_kind'] == 'daily':
            projection = read_day(root/('store-'+store), store, day)
            required.update(prefix+'/'+n for n in ('projection.json','result.json'))
            if prefix+'/quality.json' in files: required.add(prefix+'/quality.json')
            if checkpoint['state'] != 'completed': raise BackupError('backup_invalid_manifest')
        else:
            projection = read_legacy_day(root/'legacy-exports', store, day)
            required.update((f'legacy-exports/{day}/manifest.json', f'legacy-exports/{day}/store-{store}.json'))
        _closed(projection, _time(manifest['created_at']))
        total = selected = 0
        for window in checkpoint['windows']:
            sub = campaign/window['name']; entry_prefix = prefix+'/campaign/'+window['name']
            required.update(entry_prefix+'/'+n for n in ('task.json','remote.sqlite3'))
            task, _ = _window(sub/'task.json', checkpoint, window)
            n, n_day, _ = _raw_counts(sub/'remote.sqlite3', store, day, task)
            total += n; selected += n_day
            if window['checkpoint_digest'] is not None:
                for base, key in (('task.json','checkpoint_digest'),('remote.sqlite3','database_digest')):
                    if hashlib.sha256((sub/base).read_bytes()).hexdigest() != window[key]:
                        raise BackupError('backup_archive_digest_mismatch')
        if (total != row['raw_records'] or selected != row['selected_day_records']
                or selected != projection['summary']['observations']
                or row['observed_points'] != projection['returned_graph_points']
                or manifest['source_kind'] == 'daily' and total != selected):
            raise BackupError('backup_projection_database_mismatch')
    if required != files: raise BackupError('backup_invalid_members')


def restore_backup(*, bundle, destination=None):
    """Verify every byte and reread raw DBs; destination must not exist.

    Without destination this is a temporary recovery drill, not a retained copy.
    No writer locks or controller checkpoint are restored, and no source query
    can be resumed using this evidence tree.
    """
    bundle = Path(os.path.abspath(bundle))
    destination = Path(os.path.abspath(destination)) if destination is not None else None
    parent_path = destination.parent if destination is not None else bundle.parent
    parent, name = _open_parent(parent_path/(destination.name if destination else 'backup-check-sentinel'), private=True)
    temporary = None; reserved = None; published = False
    try:
        if destination is not None and os.path.lexists(destination):
            raise BackupError('backup_destination_exists')
        with _file(bundle, maximum=MAX_TOTAL+MAX_MANIFEST) as (fd, _):
            with os.fdopen(os.dup(fd), 'rb') as stream, ZipFile(stream) as archive:
                manifest = _manifest(archive)
                if shutil.disk_usage(parent_path).free < sum(e['size_bytes'] for e in manifest['files']) + 16*1024**2:
                    raise BackupError('backup_insufficient_space')
                temporary = Path(tempfile.mkdtemp(prefix='.backup-restore-', dir=parent_path))
                for row in manifest['files']:
                    target = temporary/row['path']; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    # pathlib's parents use the process umask: fix newly-owned ancestors only.
                    ancestor = target.parent
                    while ancestor != temporary:
                        ancestor.chmod(0o700); ancestor = ancestor.parent
                    count = 0; digest = hashlib.sha256()
                    out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with archive.open(row['path']) as source, os.fdopen(out_fd, 'wb') as out:
                        while True:
                            chunk = source.read(CHUNK)
                            if not chunk: break
                            count += len(chunk)
                            if count > row['size_bytes']: raise BackupError('backup_invalid_members')
                            digest.update(chunk); out.write(chunk)
                        out.flush(); os.fsync(out.fileno())
                    if count != row['size_bytes'] or digest.hexdigest() != row['sha256']:
                        raise BackupError('backup_digest_mismatch')
                _validate_restored(temporary, manifest)
                for directory, _, _ in os.walk(temporary, topdown=False):
                    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
        _check_parent(parent_path/name, parent, private=True)
        if destination is not None:
            os.mkdir(name, 0o700, dir_fd=parent)
            reserved = _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
            if _identity(destination.lstat()) != reserved: raise BackupError('backup_destination_changed')
            os.replace(temporary, destination); temporary = None; published = True
            os.fsync(parent)
        return _summary(manifest, restored=True)
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, BadZipFile, RuntimeError, sqlite3.Error):
        if published: raise BackupError('backup_restored_durability_unconfirmed') from None
        raise BackupError('backup_restore_failed') from None
    finally:
        if temporary is not None: shutil.rmtree(temporary)
        if reserved is not None and not published:
            try:
                if _identity(destination.lstat()) == reserved: os.rmdir(name, dir_fd=parent)
            except FileNotFoundError: pass
        os.close(parent)
