"""Bounded public-field packets; local export only, never upload or renew auth."""

from __future__ import annotations
import hashlib, json, re
import os
import secrets
import sqlite3
import stat
from uuid import UUID, NAMESPACE_URL, uuid5
from .capture import CaptureError, _open_parent, _check_parent
from .credentials import _private_file
from .storage import SnapshotStore
from sushiwait.monitoring import _time, _stamp
from sushiwait.observations import QUEUE_NAMES
from sushiwait.storage import _report_payload, _report_snapshot, _ERROR_CODES


class PacketError(ValueError):
    def __init__(self, code, *, committed=False):
        super().__init__(code)
        self.error_code = code
        self.committed = committed


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False).encode()


def display_field(field, kind):
    if type(field) is not dict or field.get('presence') not in {'missing','null','invalid','unknown','present'}:
        raise PacketError('packet_invalid_field')
    out={'presence':field['presence'],'value':None}
    if out['presence']!='present':
        return out
    value=field.get('value')
    if kind=='numbers':
        if (type(value) is not list or len(value)>64 or any(type(n) is not str
            or re.fullmatch(r'[A-Za-z]{0,8}[0-9]{1,10}(?:-[0-9]{1,6})?[A-Za-z]{0,8}',n) is None for n in value)):
            raise PacketError('packet_unrecognized_display_label')
        out['value']=list(value)
    elif kind=='status':
        if value not in {'OPEN','CLOSED'}:
            raise PacketError('packet_unrecognized_store_status')
        out['value']=value
    else:
        if type(value) is not int or not -(2**63)<=value<=2**63-1:
            raise PacketError('packet_invalid_field')
        out['value']=value
        out['unit']='unknown'
    return out


def public_record(row, *, as_of, store_ids, api_profile, data_origin):
    try:
        if (type(row) is not dict or set(row)!= {'id','run_id','store_id','api_profile','data_origin',
                'received_at','ok','payload_json'} or type(row['id']) is not int or not 1<=row['id']<=2**63-1
                or type(row['run_id']) is not str or len(row['run_id'])!=36
                or type(row['ok']) is not int or row['ok'] not in (0,1)
                or row['store_id'] not in store_ids or row['api_profile']!=api_profile
                or row['data_origin']!=data_origin):
            raise PacketError('packet_invalid_record')
        run=UUID(row['run_id'])
        if str(run)!=row['run_id'] or run.version!=4:
            raise PacketError('packet_invalid_record')
        record_at=_time(row['received_at'])
        if record_at>_time(as_of):
            raise PacketError('packet_future_record')
        raw=row['payload_json']
        if type(raw) is not str or len(raw.encode())>65536:
            raise PacketError('packet_record_too_large')
        body=_report_payload(raw)
        if body is None or any(body.get(k)!=row[k] for k in ('store_id','api_profile','data_origin')):
            raise PacketError('packet_invalid_record')
        identity=str(uuid5(NAMESPACE_URL, f"sushiwait:public-record:1:{run}:{row['id']}:{api_profile}:{data_origin}:{row['store_id']}"))
        out={'record_schema_version':1,'observation_id':identity,'store_id':row['store_id'],
             'api_profile':api_profile,'data_origin':data_origin,'recorded_at':_stamp(record_at),
             'ok':bool(row['ok']),'source_freshness':'unknown'}
        timing=body.get('timing')
        if type(timing) is not dict:
            raise PacketError('packet_invalid_timing')
        if row['ok']:
            clean=_report_snapshot(body,row['store_id'],data_origin,api_profile)
            if clean is None:
                raise PacketError('packet_invalid_record')
            fields=clean['normalized']
            identities=[fields[key] for key in ('id','storeId')]
            if (not any(field['presence']=='present' for field in identities) or
                any(field['presence'] not in {'missing','null'} and
                    (field['presence']!='present' or str(int(field['value']))!=row['store_id'])
                    for field in identities)):
                raise PacketError('packet_invalid_store_identity')
            out['display']={key:display_field(fields[key],'status' if key=='storeStatus' else 'integer')
                for key in ('storeStatus','groupQueuesCount','raw_wait','waitTimeCounter','waitTimeCap')}
            out['display']['groupQueues']={'presence':fields['groupQueues']['presence'],
                'groups':{key:display_field(fields['groupQueues']['groups'][key],'numbers') for key in QUEUE_NAMES}}
            phase='response'
        else:
            phase=body.get('failure_phase')
            if phase not in {'request','normalization','preflight'}:
                raise PacketError('packet_invalid_record')
            code=body.get('error_code')
            if code not in _ERROR_CODES and code!='unknown':
                raise PacketError('packet_unrecognized_error')
            status=body.get('http_status')
            if status is not None and (type(status) is not int or not 100<=status<=599):
                raise PacketError('packet_invalid_record')
            if phase=='preflight' and status is not None:
                raise PacketError('packet_invalid_record')
            out.update(failure_phase=phase,error_code=code,http_status=status)
        if phase=='preflight':
            checked=_time(timing.get('checked_at'))
            if checked!=record_at:
                raise PacketError('packet_invalid_timing')
            out['timing']={'checked_at':_stamp(checked),'semantics':'local_preflight_check'}
        else:
            started,received=_time(timing.get('request_started_at')),_time(timing.get('received_at'))
            elapsed=timing.get('elapsed_ms')
            if started>received or received!=record_at or type(elapsed) is not int or not 0<=elapsed<=2**63-1:
                raise PacketError('packet_invalid_timing')
            out['timing']={'request_started_at':_stamp(started),'received_at':_stamp(received),
                'elapsed_ms':elapsed,'semantics':'http_response_received'}
        digest=hashlib.sha256(encoded(out)).hexdigest()
        return {**out,'record_sha256':digest}
    except (ValueError,TypeError,KeyError,OverflowError) as error:
        if isinstance(error,PacketError):
            raise
        raise PacketError('packet_invalid_record') from None


def build_packet(rows, *, as_of, store_ids, api_profile, data_origin):
    if (type(rows) is not list or len(rows)>1000 or type(store_ids) is not list
        or not 1<=len(store_ids)<=3
        or any(type(s) is not str or re.fullmatch('[1-9][0-9]{0,11}',s) is None for s in store_ids)
        or len(set(store_ids))!=len(store_ids) or type(api_profile) is not str or type(data_origin) is not str
        or api_profile not in {'legacy','miniapp_gateway'} or data_origin not in {'live','fixture','synthetic'}):
        raise PacketError('packet_invalid_scope')
    try:
        at=_stamp(_time(as_of))
        records=[public_record(r,as_of=at,store_ids=store_ids,api_profile=api_profile,data_origin=data_origin) for r in rows]
    except ValueError as error:
        if isinstance(error,PacketError):raise
        raise PacketError('packet_invalid_scope') from None
    ids=[r['observation_id'] for r in records]
    if len(set(ids))!=len(ids):
        raise PacketError('packet_duplicate_record')
    out={'packet_schema_version':1,'as_of':at,'store_ids':sorted(store_ids),
        'api_profile':api_profile,'data_origin':data_origin,'records':records,
        'source_freshness':'unknown','server_received':False,'credentials_included':False,
        'personal_ticket_included':False,'eta_available':False,'verified_training_labels':0}
    if len(encoded(out))>4*1024*1024:
        raise PacketError('packet_too_large')
    return out

def _file_identity(info):
    # Appends by a concurrent collector may change size/timestamps. Replacement,
    # ownership, mode and link count may not change during a selected read.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink)


def select_packet(database, *, as_of, store_ids, api_profile, data_origin,
                  after_id=0, limit=1000):
    """Select one ascending scoped page in a consistent read-only DB2 transaction.

    The returned local cursor belongs to this exact database and scope. It is
    not a server receipt. Future/invalid selected rows fail the entire page.
    """
    # Validate all input before opening any path, including an empty database.
    build_packet([], as_of=as_of, store_ids=store_ids,
                 api_profile=api_profile, data_origin=data_origin)
    if (type(after_id) is not int or not 0 <= after_id <= 2**63-1
            or type(limit) is not int or not 1 <= limit <= 1000):
        raise PacketError('packet_invalid_bounds')
    parent_fd = file_fd = None
    try:
        parent_fd, name = _open_parent(database, private=True)
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=parent_fd)
        before = os.fstat(file_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not _private_file(before) or not _private_file(named)
                or _file_identity(before) != _file_identity(named)):
            raise PacketError('packet_database_unsafe')
        with SnapshotStore(database, read_only=True) as source:
            source.db.execute('PRAGMA query_only=ON')
            source.db.execute('PRAGMA trusted_schema=OFF')
            kind = source.db.execute("SELECT type FROM sqlite_master WHERE name='samples'").fetchone()
            if source._schema_version != 2 or kind != ('table',):
                raise PacketError('packet_database_schema')
            source.db.execute('BEGIN')
            try:
                names = ('id','run_id','store_id','api_profile','data_origin','received_at','ok','payload_json')
                # Do not materialize an unexpectedly large payload/run/timestamp.
                rows = source.db.execute(
                    "SELECT id,CASE WHEN length(run_id)=36 THEN run_id END,store_id,api_profile,"
                    "data_origin,CASE WHEN length(received_at)<=32 THEN received_at END,ok,"
                    "CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END "
                    "FROM samples WHERE id>? AND store_id IN (" + ','.join('?' for _ in store_ids) +
                    ") AND api_profile=? AND data_origin=? ORDER BY id LIMIT ?",
                    [after_id, *store_ids, api_profile, data_origin, limit+1]).fetchall()
                page = [dict(zip(names,row)) for row in rows[:limit]]
                packet = build_packet(page, as_of=as_of, store_ids=store_ids,
                                      api_profile=api_profile, data_origin=data_origin)
                _check_parent(database, parent_fd, private=True)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (not _private_file(current) or _file_identity(before) != _file_identity(current)
                        or _file_identity(before) != _file_identity(os.fstat(file_fd))):
                    raise PacketError('packet_database_changed')
                local = {'after_id':after_id, 'next_after_id':page[-1]['id'] if page else after_id,
                         'has_more':len(rows)>limit, 'record_count':len(page)}
            finally:
                source.db.rollback()
        return packet, local
    except PacketError:
        raise
    except CaptureError:
        raise PacketError('packet_database_unsafe') from None
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        raise PacketError('packet_database_error') from None
    finally:
        for descriptor in (file_fd,parent_fd):
            if descriptor is not None:
                os.close(descriptor)


def _write_packet(body, destination, *, before_publish=None):
    """Publish a complete new 0600 file without replacing an existing name.

    A hard link provides atomic no-overwrite publication; the temporary link is
    removed immediately. No source cursor is persisted or advanced here.
    """
    parent_fd = file_fd = None
    temporary = None
    committed = False
    try:
        import fcntl
        parent_fd, name = _open_parent(destination, private=True)
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise PacketError('packet_output_exists')
        os.fsync(parent_fd)
        if before_publish is not None:
            before_publish(parent_fd)
        temporary = '.sushiwait-packet-' + secrets.token_hex(16) + '.tmp'
        file_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=parent_fd)
        initial = os.fstat(file_fd)
        if not _private_file(initial) or stat.S_IMODE(initial.st_mode) != 0o600:
            raise PacketError('packet_output_unsafe')
        offset = 0
        while offset < len(body):
            written = os.write(file_fd, body[offset:])
            if written <= 0:
                raise PacketError('packet_output_error')
            offset += written
        os.fsync(file_fd)
        os.lseek(file_fd, 0, os.SEEK_SET)
        pieces, remaining = [], len(body)+1
        while remaining:
            piece = os.read(file_fd, remaining)
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        named = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        final = os.fstat(file_fd)
        if (not _private_file(named) or _file_identity(final) != _file_identity(named)
                or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or final.st_size != len(body) or b''.join(pieces) != body):
            raise PacketError('packet_output_changed')
        _check_parent(destination, parent_fd, private=True)
        try:
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                    follow_symlinks=False)
        except FileExistsError:
            raise PacketError('packet_output_exists') from None
        committed = True
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = None
        _check_parent(destination, parent_fd, private=True)
        # If fsync fails after publication, never call this an unwritten file or
        # roll it back. The caller must inspect the explicit committed status.
        try:
            os.fsync(parent_fd)
        except OSError:
            return {'committed':True, 'durability_confirmed':False}
        return {'committed':True, 'durability_confirmed':True}
    except PacketError:
        raise
    except (OSError, CaptureError, ImportError, ValueError, TypeError):
        raise PacketError('packet_output_uncertain' if committed else 'packet_output_error',
                          committed=committed) from None
    finally:
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        for descriptor in (file_fd,parent_fd):
            if descriptor is not None:
                os.close(descriptor)


def export_packet(database, destination, **scope):
    """Export only allowed public fields; never send a packet or access auth."""
    try:
        conflict = os.path.abspath(database) == os.path.abspath(destination)
    except (TypeError, ValueError, OSError):
        raise PacketError('packet_invalid_path') from None
    if conflict:
        raise PacketError('packet_path_conflict')
    packet, local = select_packet(database, **scope)
    body = encoded(packet)
    result = _write_packet(body, destination)
    return {**local, **result, 'packet_schema_version':1,
            'packet_bytes':len(body), 'packet_sha256':hashlib.sha256(body).hexdigest(),
            'network_performed':False, 'server_received':False,
            'credentials_accessed':False, 'personal_ticket_included':False,
            'source_freshness':'unknown', 'eta_available':False,
            'verified_training_labels':0, 'output_requires_private_handling':True}
