"""Bounded full snapshots and independent restoration of continuity archives.

Only a caller-selected public-field archive is opened. No account material,
source queries, upload, writer resume or full-business-day claim is included.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .capture import _open_parent, _check_parent
from .continuity import _uuid5, validate_continuity_bundle
from .continuityarchive import SCHEMA, singleton_bundle
from .continuityview import ContinuityStatisticsView
from .credentials import _private_file
from .monitoring import _stamp, _time
from .officialview import _json, read_config
from .packets import build_packet, encoded, _file_identity, _write_packet
from .receipts import _read_packet_value, validate_packet

MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 20000
MAX_BUNDLES = 1000
_MANIFEST_KEYS = {'backup_schema_version','archive_kind','as_of','archive_file',
    'archive_sha256','archive_bytes','store_ids','api_profile','data_origin','audit',
    'source_claims_verified','complete_business_day_claim','backup_kind'}
_AUDIT_COUNTS = {'records','writer_claims','linked_observations','partial_writer_claims','unclaimed_records','bundles'}
_AUDIT_FLAGS = {'all_original_body_hashes_checked','all_relation_hashes_checked',
                'bundle_membership_and_first_times_checked','acyclic_claim_graph_checked'}


class ContinuityBackupError(ValueError):
    def __init__(self, code, *, output_may_exist=False):
        self.error_code=code;self.output_may_exist=output_may_exist
        super().__init__(code)


def _deadline(seconds):
    if type(seconds) not in (int,float) or not 1 <= seconds <= 30:
        raise ContinuityBackupError('continuity_backup_invalid_deadline')
    return time.monotonic()+seconds


def _check(deadline):
    if time.monotonic() >= deadline:
        raise ContinuityBackupError('continuity_backup_deadline')


def _canonical(value, cutoff):
    moment=_time(value)
    if _stamp(moment)!=value or moment>cutoff:
        raise ContinuityBackupError('continuity_backup_invalid_time')
    return moment


@contextmanager
def _snapshot_file(path, *, deadline, maximum=MAX_DATABASE_BYTES):
    """Stable identity/permissions/mtime and whole bytes, not ctime alone.

    Real macOS snapshots changed only ctime during a read. Hash the complete
    fixed descriptor before and after use; this never admits changed content.
    """
    parent,name=_open_parent(path,private=True);fd=None
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        before=os.fstat(fd)
        if (not _private_file(before) or stat.S_IMODE(before.st_mode)!=0o600
                or not 0<before.st_size<=maximum):
            raise ContinuityBackupError('continuity_backup_unsafe_file')
        identity=(*_file_identity(before),before.st_size,before.st_mtime_ns)
        def guard():
            _check(deadline);_check_parent(path,parent,private=True)
            for current in (os.fstat(fd),os.stat(name,dir_fd=parent,follow_symlinks=False)):
                if (not _private_file(current)
                        or (*_file_identity(current),current.st_size,current.st_mtime_ns)!=identity):
                    raise ContinuityBackupError('continuity_backup_file_changed')
        def digest():
            result=hashlib.sha256();offset=0
            while offset<before.st_size:
                _check(deadline);part=os.pread(fd,min(131072,before.st_size-offset),offset)
                if not part:raise ContinuityBackupError('continuity_backup_file_changed')
                result.update(part);offset+=len(part)
            guard();return result.digest()
        guard();initial_digest=digest()
        yield fd,before.st_size
        guard()
        if digest()!=initial_digest:
            raise ContinuityBackupError('continuity_backup_file_changed')
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)


def _audit(path, packet, deadline):
    """Validate all three tables, including evidence membership and graph."""
    cutoff=_time(packet['as_of'])
    with _snapshot_file(path,deadline=deadline):
        with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=0)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
            db.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
            db.execute('BEGIN')
            objects=db.execute("SELECT type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if (db.execute('PRAGMA user_version').fetchone()[0]!=1
                    or any(row[0]!='table' for row in objects)
                    or sorted(' '.join(row[1].split()) for row in objects)!=sorted(' '.join(s.split()) for s in SCHEMA)
                    or db.execute('PRAGMA journal_mode').fetchone()[0]!='delete'
                    or db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]
                    or db.execute('PRAGMA foreign_key_check').fetchall()):
                raise ContinuityBackupError('continuity_backup_invalid_schema')
            counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                for table in ('archive_records','continuity_claims','continuity_bundles')}
            if (counts['archive_records']>MAX_RECORDS or counts['continuity_claims']>MAX_RECORDS
                    or counts['continuity_bundles']>MAX_BUNDLES):
                raise ContinuityBackupError('continuity_backup_capacity_exceeded')
            records={};archived={}
            rows=db.execute('SELECT observation_id,record_sha256,store_id,api_profile,data_origin,recorded_at,ok,'
                'archived_at,CASE WHEN length(CAST(record_json AS BLOB))<=65536 THEN record_json END '
                'FROM archive_records ORDER BY observation_id')
            for row in rows:
                _check(deadline);record=_json(row[8])
                validate_packet({**packet,'records':[record]},as_of=packet['as_of'])
                if (any(record[k]!=row[i] for i,k in enumerate(('observation_id','record_sha256',
                        'store_id','api_profile','data_origin','recorded_at')))
                        or type(row[6]) is not int or row[6]!=int(record['ok']) or row[8]!=encoded(record).decode()):
                    raise ContinuityBackupError('continuity_backup_record_mismatch')
                archived[row[0]]=_canonical(row[7],cutoff);records[row[0]]=record
            claims={};links={};partial=0
            for identity,alias,digest,raw,claimed,linked in db.execute('SELECT observation_id,writer_id,link_sha256,'
                    'CASE WHEN length(CAST(link_json AS BLOB))<=4096 THEN link_json END,claimed_at,linked_at '
                    'FROM continuity_claims ORDER BY observation_id'):
                _check(deadline);_uuid5(alias)
                claimed_time=_canonical(claimed,cutoff)
                if identity not in records or claimed_time<archived[identity]:
                    raise ContinuityBackupError('continuity_backup_claim_mismatch')
                claims[identity]=(alias,claimed,linked)
                if digest is None and raw is None and linked is None:
                    partial+=1;continue
                association=singleton_bundle(db,records[identity],packet=packet)
                if association is None:
                    raise ContinuityBackupError('continuity_backup_claim_mismatch')
                bundle,_=association;link=bundle['links'][0]
                if raw!=encoded(link).decode() or digest!=link['link_sha256']:
                    raise ContinuityBackupError('continuity_backup_link_mismatch')
                links[identity]=link
            first_claim={};first_link={}
            for digest,raw,received in db.execute('SELECT bundle_sha256,'
                    'CASE WHEN length(CAST(bundle_json AS BLOB))<=4194304 THEN bundle_json END,archived_at '
                    'FROM continuity_bundles ORDER BY bundle_sha256'):
                _check(deadline);moment=_canonical(received,cutoff)
                bundle=validate_continuity_bundle(_json(raw),as_of=received)
                if (not re.fullmatch('[0-9a-f]{64}',digest) or raw!=encoded(bundle).decode()
                        or hashlib.sha256(raw.encode()).hexdigest()!=digest
                        or any(bundle['packet'][k]!=packet[k] for k in ('store_ids','api_profile','data_origin'))):
                    raise ContinuityBackupError('continuity_backup_bundle_mismatch')
                members=[(r,l['writer_id']) for r,l in zip(bundle['packet']['records'],bundle['links'])]
                members += [(a['record'],a['writer_id']) for a in bundle['anchors']]
                for record,alias in members:
                    identity=record['observation_id']
                    if records.get(identity)!=record or identity not in claims or claims[identity][0]!=alias:
                        raise ContinuityBackupError('continuity_backup_bundle_membership')
                    first_claim[identity]=min(first_claim.get(identity,moment),moment)
                for link in bundle['links']:
                    identity=link['observation_id']
                    if links.get(identity)!=link:
                        raise ContinuityBackupError('continuity_backup_bundle_membership')
                    first_link[identity]=min(first_link.get(identity,moment),moment)
            if (set(first_claim)!=set(claims) or set(first_link)!=set(links)
                    or any(_time(claims[i][1])!=first_claim[i] for i in claims)
                    or any(_time(claims[i][2])!=first_link[i] for i in links)):
                raise ContinuityBackupError('continuity_backup_first_receipt_mismatch')
            # A per-page graph can be valid while contradictory pages make a
            # cycle. Audit the complete bounded snapshot, retaining unknown
            # anchor links as endpoints instead of inventing predecessors.
            finished=set()
            for identity in links:
                trail=set();current=identity
                while current in links and current not in finished:
                    _check(deadline)
                    if current in trail:
                        raise ContinuityBackupError('continuity_backup_graph_cycle')
                    trail.add(current);current=links[current]['predecessor_id']
                finished.update(trail)
            db.rollback();_check(deadline)
    return {'records':len(records),'writer_claims':len(claims),'linked_observations':len(links),
        'partial_writer_claims':partial,'unclaimed_records':len(records)-len(claims),
        'bundles':counts['continuity_bundles'],'all_original_body_hashes_checked':True,
        'all_relation_hashes_checked':True,'bundle_membership_and_first_times_checked':True,
        'acyclic_claim_graph_checked':True}


def _hash(path,deadline):
    digest=hashlib.sha256()
    with _snapshot_file(path,deadline=deadline) as (fd,size):
        while chunk:=os.read(fd,131072):_check(deadline);digest.update(chunk)
    return digest.hexdigest(),size


def _folder(path):
    parent,name=_open_parent(path,private=True)
    try:
        os.mkdir(name,0o700,dir_fd=parent)
        _check_parent(path,parent,private=True)
        os.fsync(parent)
    finally:os.close(parent)
    return Path(os.path.abspath(path))


def _sync(folder):
    parent,name=_open_parent(folder/'archive.sqlite3',private=True)
    fd=None
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
        if not _private_file(os.fstat(fd)):
            raise ContinuityBackupError('continuity_backup_unsafe_output')
        os.fsync(fd);os.fsync(parent);_check_parent(folder/'archive.sqlite3',parent,private=True)
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)


def _summary(manifest):
    return {'backup_schema_version':1,'archive_kind':manifest['archive_kind'],'as_of':manifest['as_of'],
        'archive_sha256':manifest['archive_sha256'],'archive_bytes':manifest['archive_bytes'],
        **manifest['audit'],'source_claims_verified':False,'complete_business_day_claim':False,
        'network_performed':False,'origin_queries_added':0,'query_credentials_accessed':False}


def _seal(folder,manifest,deadline):
    # Keep a stable database descriptor through manifest publication. The
    # independent reader verifies the copied bytes again on every restoration.
    with _snapshot_file(folder/'archive.sqlite3',deadline=deadline) as (fd,size):
        digest=hashlib.sha256()
        while chunk:=os.read(fd,131072):_check(deadline);digest.update(chunk)
        if (digest.hexdigest(),size)!=(manifest['archive_sha256'],manifest['archive_bytes']):
            raise ContinuityBackupError('continuity_backup_hash_mismatch')
        os.fsync(fd);_check(deadline)
        if _write_packet(encoded(manifest),folder/'manifest.json')['durability_confirmed'] is not True:
            raise ContinuityBackupError('continuity_backup_manifest_unconfirmed')


def snapshot_archive(config_file,output,*,clock=None,seconds=30):
    """Online SQLite snapshot; final manifest is published only after audit."""
    created=False
    try:
        deadline=_deadline(seconds);clock=clock or (lambda:datetime.now(timezone.utc))
        view=read_config(config_file)
        if not isinstance(view,ContinuityStatisticsView):
            raise ContinuityBackupError('continuity_backup_wrong_mode')
        created=True;folder=_folder(output);target=folder/'archive.sqlite3'
        parent,name=_open_parent(view.database,private=True);source_fd=None
        try:
            source_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            original=os.fstat(source_fd)
            if not _private_file(original) or not 0<original.st_size<=MAX_DATABASE_BYTES:
                raise ContinuityBackupError('continuity_backup_unsafe_source')
            identity=_file_identity(original)
            def guard():
                _check(deadline);_check_parent(view.database,parent,private=True)
                if (not _private_file(os.fstat(source_fd))
                        or _file_identity(os.fstat(source_fd))!=identity
                        or _file_identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=identity):
                    raise ContinuityBackupError('continuity_backup_source_replaced')
            guard()
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            with closing(sqlite3.connect(view.database.as_uri()+'?mode=ro',uri=True,timeout=0)) as src:
                src.execute('PRAGMA query_only=ON');src.execute('PRAGMA trusted_schema=OFF')
                with closing(sqlite3.connect(target.as_uri()+'?mode=rw',uri=True,timeout=0)) as dst:
                    src.backup(dst,pages=128,progress=lambda *_:guard(),sleep=.01)
            guard()
        finally:
            if source_fd is not None:os.close(source_fd)
            os.close(parent)
        at=_stamp(_time(clock().isoformat()))
        packet=build_packet([],as_of=at,store_ids=sorted(view.names),api_profile='miniapp_gateway',data_origin=view.data_origin)
        audit=_audit(target,packet,deadline);digest,size=_hash(target,deadline);_sync(folder)
        manifest={'backup_schema_version':1,'archive_kind':'public_records_with_reported_continuity',
            'as_of':at,'archive_file':'archive.sqlite3','archive_sha256':digest,'archive_bytes':size,
            'store_ids':packet['store_ids'],'api_profile':'miniapp_gateway','data_origin':packet['data_origin'],
            'audit':audit,'source_claims_verified':False,'complete_business_day_claim':False,
            'backup_kind':'sqlite_snapshot_not_offsite'}
        _check(deadline)
        _seal(folder,manifest,deadline)
        return {**_summary(manifest),'durability_confirmed':True,'backup_kind':manifest['backup_kind']}
    except ContinuityBackupError as error:
        raise ContinuityBackupError(error.error_code,output_may_exist=created) from None
    except Exception:
        raise ContinuityBackupError('continuity_backup_failed',output_may_exist=created) from None


def _verify(folder,*,as_of,deadline):
    folder=Path(os.path.abspath(folder));manifest=_read_packet_value(folder/'manifest.json')
    if (type(manifest) is not dict or set(manifest)!=_MANIFEST_KEYS
            or type(manifest['backup_schema_version']) is not int or manifest['backup_schema_version']!=1
            or manifest['archive_kind']!='public_records_with_reported_continuity'
            or manifest['archive_file']!='archive.sqlite3' or manifest['api_profile']!='miniapp_gateway'
            or manifest['backup_kind']!='sqlite_snapshot_not_offsite'
            or manifest['source_claims_verified'] is not False or manifest['complete_business_day_claim'] is not False
            or type(manifest['archive_bytes']) is not int or not 0<manifest['archive_bytes']<=MAX_DATABASE_BYTES
            or type(manifest['archive_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',manifest['archive_sha256'])):
        raise ContinuityBackupError('continuity_backup_manifest_invalid')
    audit=manifest['audit']
    if (type(audit) is not dict or set(audit)!=_AUDIT_COUNTS|_AUDIT_FLAGS
            or any(type(audit[k]) is not int or not 0<=audit[k]<=MAX_RECORDS for k in _AUDIT_COUNTS)
            or audit['bundles']>MAX_BUNDLES or any(audit[k] is not True for k in _AUDIT_FLAGS)):
        raise ContinuityBackupError('continuity_backup_manifest_invalid')
    _canonical(manifest['as_of'],_time(as_of))
    packet=build_packet([],as_of=manifest['as_of'],store_ids=manifest['store_ids'],
                        api_profile=manifest['api_profile'],data_origin=manifest['data_origin'])
    if packet['store_ids']!=manifest['store_ids']:
        raise ContinuityBackupError('continuity_backup_manifest_invalid')
    target=folder/'archive.sqlite3';digest,size=_hash(target,deadline)
    if (digest,size)!=(manifest['archive_sha256'],manifest['archive_bytes']):
        raise ContinuityBackupError('continuity_backup_hash_mismatch')
    if _audit(target,packet,deadline)!=manifest['audit']:
        raise ContinuityBackupError('continuity_backup_audit_mismatch')
    return folder,manifest


def verify_snapshot(folder,*,as_of,seconds=30):
    try:
        _,manifest=_verify(folder,as_of=as_of,deadline=_deadline(seconds))
        return {**_summary(manifest),'snapshot_validated':True,'historical_snapshot_only':True,
                'writer_state_verified':False,'offsite_location_verified':False}
    except ContinuityBackupError:raise
    except Exception:raise ContinuityBackupError('continuity_backup_verification_failed') from None


def restore_snapshot(source,output,*,as_of,seconds=30):
    created=False
    try:
        deadline=_deadline(seconds);folder,manifest=_verify(source,as_of=as_of,deadline=deadline)
        created=True;destination=_folder(output);target=destination/'archive.sqlite3'
        descriptor=None
        try:
            descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with _snapshot_file(folder/'archive.sqlite3',deadline=deadline) as (source_fd,_):
                while chunk:=os.read(source_fd,131072):
                    _check(deadline);offset=0
                    while offset<len(chunk):
                        written=os.write(descriptor,chunk[offset:])
                        if written<=0:raise ContinuityBackupError('continuity_backup_copy_failed')
                        offset+=written
            os.fsync(descriptor)
        finally:
            if descriptor is not None:os.close(descriptor)
        if _hash(target,deadline)!=(manifest['archive_sha256'],manifest['archive_bytes']):
            raise ContinuityBackupError('continuity_backup_hash_mismatch')
        packet=build_packet([],as_of=manifest['as_of'],store_ids=manifest['store_ids'],
                            api_profile=manifest['api_profile'],data_origin=manifest['data_origin'])
        if _audit(target,packet,deadline)!=manifest['audit']:
            raise ContinuityBackupError('continuity_backup_audit_mismatch')
        _sync(destination);_check(deadline)
        _seal(destination,manifest,deadline)
        return {**_summary(manifest),'restored':True,'durability_confirmed':True,
                'writer_started':False,'restored_archive_bytes_identical':True}
    except ContinuityBackupError as error:
        raise ContinuityBackupError(error.error_code,output_may_exist=created) from None
    except Exception:
        raise ContinuityBackupError('continuity_backup_restore_failed',output_may_exist=created) from None
