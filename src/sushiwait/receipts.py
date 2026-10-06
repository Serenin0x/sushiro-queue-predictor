"""Strict local packet validation and append-only archive, without a server."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID

from .capture import CaptureError, _open_parent, _check_parent
from .credentials import _identity, _private_file
from .monitoring import _time, _stamp
from .observations import QUEUE_NAMES
from .packets import PacketError, build_packet, display_field, encoded
from .storage import _ERROR_CODES, _unique_object


MAX_PACKET_BYTES = 4 * 1024 * 1024
_PRESENCES = {'present','missing','null','invalid','unknown'}
_PACKET_KEYS = {'packet_schema_version','as_of','store_ids','api_profile','data_origin',
    'records','source_freshness','server_received','credentials_included',
    'personal_ticket_included','eta_available','verified_training_labels'}
_RECORD_KEYS = {'record_schema_version','observation_id','store_id','api_profile',
    'data_origin','recorded_at','ok','source_freshness','timing','record_sha256'}
_SCALARS = ('storeStatus','groupQueuesCount','raw_wait','waitTimeCounter','waitTimeCap')
_CREATE = ("CREATE TABLE archive_records(observation_id TEXT PRIMARY KEY NOT NULL,"
    "record_sha256 TEXT NOT NULL,store_id TEXT NOT NULL,api_profile TEXT NOT NULL,"
    "data_origin TEXT NOT NULL,recorded_at TEXT NOT NULL,ok INTEGER NOT NULL,"
    "archived_at TEXT NOT NULL,record_json TEXT NOT NULL)")


class ReceiptError(ValueError):
    def __init__(self, error_code, *, commit_status='not_started'):
        super().__init__(error_code)
        self.error_code = error_code
        self.commit_status = commit_status


def _canonical_time(value):
    moment = _time(value)
    if _stamp(moment) != value:
        raise ReceiptError('packet_noncanonical_time')
    return moment


def _reject_constant(_):
    raise ValueError('nonfinite_packet')


def _validate_record(record, packet):
    if type(record) is not dict or type(record.get('ok')) is not bool:
        raise ReceiptError('packet_invalid_record')
    success = record['ok']
    expected = _RECORD_KEYS | ({'display'} if success else {'failure_phase','error_code','http_status'})
    if (set(record) != expected or type(record['record_schema_version']) is not int
            or record['record_schema_version'] != 1
            or record['store_id'] not in packet['store_ids']
            or record['api_profile'] != packet['api_profile']
            or record['data_origin'] != packet['data_origin']
            or record['source_freshness'] != 'unknown'):
        raise ReceiptError('packet_invalid_record')
    identity = record['observation_id']
    if type(identity) is not str or len(identity) != 36:
        raise ReceiptError('packet_invalid_observation_id')
    parsed = UUID(identity)
    if str(parsed) != identity or parsed.version != 5:
        raise ReceiptError('packet_invalid_observation_id')
    recorded = _canonical_time(record['recorded_at'])
    if recorded > _canonical_time(packet['as_of']):
        raise ReceiptError('packet_future_record')
    if success:
        display = record['display']
        if type(display) is not dict or set(display) != {*_SCALARS,'groupQueues'}:
            raise ReceiptError('packet_invalid_display')
        for key in _SCALARS:
            kind = 'status' if key == 'storeStatus' else 'integer'
            if display_field(display[key],kind) != display[key]:
                raise ReceiptError('packet_invalid_display')
        groups = display['groupQueues']
        if (type(groups) is not dict or set(groups) != {'presence','groups'}
                or groups['presence'] not in _PRESENCES or type(groups['groups']) is not dict
                or set(groups['groups']) != set(QUEUE_NAMES)):
            raise ReceiptError('packet_invalid_display')
        for field in groups['groups'].values():
            if (display_field(field,'numbers') != field
                    or groups['presence'] != 'present' and field['presence'] == 'present'):
                raise ReceiptError('packet_invalid_display')
        phase = 'response'
    else:
        phase = record['failure_phase']
        if phase not in {'preflight','request','normalization'} or record['error_code'] not in {*_ERROR_CODES,'unknown'}:
            raise ReceiptError('packet_invalid_failure')
        status = record['http_status']
        if (status is not None and (type(status) is not int or not 100 <= status <= 599)
                or phase == 'preflight' and status is not None):
            raise ReceiptError('packet_invalid_failure')
    timing = record['timing']
    if type(timing) is not dict:
        raise ReceiptError('packet_invalid_timing')
    if phase == 'preflight':
        if (set(timing) != {'checked_at','semantics'}
                or timing['semantics'] != 'local_preflight_check'
                or _canonical_time(timing['checked_at']) != recorded):
            raise ReceiptError('packet_invalid_timing')
    elif (set(timing) != {'request_started_at','received_at','elapsed_ms','semantics'}
            or timing['semantics'] != 'http_response_received'
            or _canonical_time(timing['request_started_at']) > recorded
            or _canonical_time(timing['received_at']) != recorded
            or type(timing['elapsed_ms']) is not int or not 0 <= timing['elapsed_ms'] <= 2**63-1):
        raise ReceiptError('packet_invalid_timing')
    digest = record['record_sha256']
    if (type(digest) is not str or re.fullmatch('[0-9a-f]{64}',digest) is None
            or digest != hashlib.sha256(encoded({k:v for k,v in record.items() if k != 'record_sha256'})).hexdigest()):
        raise ReceiptError('packet_checksum_mismatch')


def validate_packet(packet, *, as_of):
    """Validate structure/checksums, never authenticate provenance or freshness."""
    try:
        if type(packet) is not dict or set(packet) != _PACKET_KEYS:
            raise ReceiptError('packet_invalid_format')
        if len(encoded(packet)) > MAX_PACKET_BYTES:
            raise ReceiptError('packet_too_large')
        scope = build_packet([],as_of=packet['as_of'],store_ids=packet['store_ids'],
                             api_profile=packet['api_profile'],data_origin=packet['data_origin'])
        if (type(packet['packet_schema_version']) is not int or packet['packet_schema_version'] != 1
                or packet['store_ids'] != scope['store_ids'] or packet['as_of'] != scope['as_of']
                or _canonical_time(packet['as_of']) > _time(as_of)
                or packet['source_freshness'] != 'unknown'
                or any(packet[key] is not False for key in ('server_received','credentials_included',
                    'personal_ticket_included','eta_available'))
                or type(packet['verified_training_labels']) is not int or packet['verified_training_labels'] != 0
                or type(packet['records']) is not list or len(packet['records']) > 1000):
            raise ReceiptError('packet_invalid_format')
        seen = set()
        for record in packet['records']:
            _validate_record(record,packet)
            if record['observation_id'] in seen:
                raise ReceiptError('packet_duplicate_record')
            seen.add(record['observation_id'])
        return json.loads(encoded(packet))
    except ReceiptError:
        raise
    except (PacketError, ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise ReceiptError('packet_invalid_format') from None


def read_packet(path, *, as_of):
    parent_fd = file_fd = None
    try:
        parent_fd,name = _open_parent(path,private=True)
        file_fd = os.open(name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not _private_file(before):
            raise ReceiptError('packet_input_unsafe')
        if before.st_size > MAX_PACKET_BYTES:
            raise ReceiptError('packet_too_large')
        chunks,remaining = [],MAX_PACKET_BYTES+1
        while remaining:
            chunk = os.read(file_fd,remaining)
            if not chunk:
                break
            chunks.append(chunk);remaining -= len(chunk)
        body = b''.join(chunks)
        _check_parent(path,parent_fd,private=True)
        named = os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        if (not _private_file(named) or _identity(before) != _identity(named)
                or _identity(before) != _identity(os.fstat(file_fd)) or len(body) != before.st_size):
            raise ReceiptError('packet_input_changed')
        value = json.loads(body.decode('utf8'),object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        return validate_packet(value,as_of=as_of)
    except ReceiptError:
        raise
    except (CaptureError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ReceiptError('packet_input_invalid') from None
    finally:
        for descriptor in (file_fd,parent_fd):
            if descriptor is not None:
                os.close(descriptor)


def packet_summary(packet):
    return {'record_count':len(packet['records']),
            'successful_records':sum(record['ok'] for record in packet['records']),
            'failed_records':sum(not record['ok'] for record in packet['records']),
            'packet_sha256':hashlib.sha256(encoded(packet)).hexdigest(),
            'structure_validated':True, 'source_claims_verified':False,
            'source_freshness':'unknown', 'server_received':False,
            'network_performed':False, 'credentials_accessed':False,
            'eta_available':False, 'verified_training_labels':0}


class PacketArchive:
    """One private local database, advisory directory lock, transactional batches."""
    def __init__(self,path):
        self.parent_fd = self.db = None
        try:
            import fcntl
            self.path = Path(os.path.abspath(path))
            self.parent_fd,self.name = _open_parent(self.path,private=True)
            fcntl.flock(self.parent_fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.open(self.name,os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,0o600,dir_fd=self.parent_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ReceiptError('archive_database_unsafe')
                self.identity = (info.st_dev,info.st_ino)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=0)
            self.db.execute('PRAGMA trusted_schema=OFF')
            self._guard()
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            objects = self.db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and not objects:
                self.db.execute('BEGIN IMMEDIATE');self.db.execute(_CREATE)
                self.db.execute('PRAGMA user_version=1');self.db.commit()
            elif version != 1 or len(objects) != 1 or objects[0][:2] != ('table','archive_records') or ' '.join(objects[0][2].split()) != ' '.join(_CREATE.split()):
                raise ReceiptError('archive_database_schema')
            if self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ReceiptError('archive_database_schema')
        except (KeyboardInterrupt,SystemExit):
            self.close();raise
        except Exception as error:
            self.close()
            if isinstance(error,ReceiptError):raise
            raise ReceiptError('archive_database_unavailable') from None

    def _guard(self):
        _check_parent(self.path,self.parent_fd,private=True)
        info = os.stat(self.name,dir_fd=self.parent_fd,follow_symlinks=False)
        if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600 or (info.st_dev,info.st_ino) != self.identity:
            raise ReceiptError('archive_database_changed')

    def append(self,packet,*,as_of):
        # Validate again at the database boundary; direct library callers cannot
        # bypass field checks or store arbitrary JSON in the archive.
        packet = validate_packet(packet,as_of=as_of)
        records = packet['records'];inserted = duplicates = 0;commit_status = 'not_started'
        try:
            self._guard();self.db.execute('BEGIN IMMEDIATE')
            for record in records:
                body = encoded(record).decode('utf8')
                previous = self.db.execute('SELECT record_sha256,record_json FROM archive_records WHERE observation_id=?',
                                           [record['observation_id']]).fetchone()
                if previous is not None:
                    if previous != (record['record_sha256'],body):
                        raise ReceiptError('archive_observation_conflict')
                    duplicates += 1
                else:
                    self.db.execute('INSERT INTO archive_records VALUES(?,?,?,?,?,?,?,?,?)',
                        [record['observation_id'],record['record_sha256'],record['store_id'],
                         record['api_profile'],record['data_origin'],record['recorded_at'],
                         int(record['ok']),_stamp(_time(as_of)),body])
                    inserted += 1
            self._guard()
            commit_status = 'unknown'
            self.db.commit()
            commit_status = 'committed'
            self._guard()
            os.fsync(self.parent_fd)
            return {**packet_summary(packet),'inserted_records':inserted,'duplicate_records':duplicates,
                    'archive_schema_version':1,'local_archive_commit_status':'committed',
                    'durability_confirmed':True}
        except ReceiptError as error:
            self.db.rollback()
            if commit_status != 'not_started':
                raise ReceiptError(error.error_code,commit_status=commit_status) from None
            raise
        except (OSError,CaptureError,sqlite3.Error):
            self.db.rollback()
            raise ReceiptError('archive_commit_unconfirmed' if commit_status != 'not_started'
                               else 'archive_database_error',commit_status=commit_status) from None

    def close(self):
        if self.db is not None:
            self.db.close();self.db = None
        if self.parent_fd is not None:
            os.close(self.parent_fd);self.parent_fd = None

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()


def archive_packet(path,database,*,now:datetime):
    try:
        if os.path.abspath(path) == os.path.abspath(database):
            raise ReceiptError('archive_path_conflict')
        at = _stamp(_time(now.isoformat()))
    except ReceiptError:
        raise
    except (OSError,ValueError,TypeError,AttributeError):
        raise ReceiptError('archive_invalid_input') from None
    packet = read_packet(path,as_of=at)
    with PacketArchive(database) as archive:
        return archive.append(packet,as_of=at)
