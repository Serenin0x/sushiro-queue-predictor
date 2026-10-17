"""Incremental, indexed public read model. Original archives remain read-only.

A durable rowid cursor indexes only newly accepted observations and bundles.
Dirty store/day partitions are rebuilt independently; web reads never scan raw
history. This is a derived view, not another source of official observations.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import zlib

from .capture import _open_parent, _check_parent
from .credentials import _private_file
from .packets import _file_identity, build_packet, encoded
from .monitoring import _stamp, _time
from .officialview import OfficialStatisticsView, OfficialViewError, _COLUMNS, _DAY, _MONTH, _json
from .continuity import validate_continuity_bundle
from .receipts import validate_packet, MAX_PACKET_BYTES
from .analytics import summarize_day, opening

POLICY='indexed_day_rollups_v1'
DAY_LIMIT=2*1024*1024
SCHEMA=(
 'CREATE TABLE sources(source_key TEXT PRIMARY KEY, scope_sha256 TEXT NOT NULL, identity_json TEXT, record_cursor INTEGER NOT NULL DEFAULT 0, bundle_cursor INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0, backlog INTEGER NOT NULL DEFAULT 0, error_code TEXT)',
 'CREATE TABLE pointers(source_key TEXT NOT NULL, observation_id TEXT NOT NULL, source_rowid INTEGER NOT NULL, store_id TEXT NOT NULL, day TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(source_key,observation_id), UNIQUE(source_key,source_rowid))',
 'CREATE INDEX pointers_day ON pointers(source_key,store_id,day,source_rowid)',
 'CREATE TABLE dirty(source_key TEXT NOT NULL,store_id TEXT NOT NULL,day TEXT NOT NULL,first_seen TEXT NOT NULL, PRIMARY KEY(source_key,store_id,day))',
 'CREATE INDEX dirty_age ON dirty(first_seen,source_key,store_id,day)',
 'CREATE TABLE snapshots(store_id TEXT NOT NULL,day TEXT NOT NULL,source_key TEXT NOT NULL,built_at TEXT NOT NULL,detail BLOB NOT NULL,detail_sha256 TEXT NOT NULL,summary TEXT NOT NULL,summary_sha256 TEXT NOT NULL,ordinary BLOB NOT NULL,ordinary_sha256 TEXT NOT NULL,reservation BLOB NOT NULL,reservation_sha256 TEXT NOT NULL, PRIMARY KEY(store_id,day))',
 'CREATE INDEX snapshots_day ON snapshots(day,store_id)',
)


def _schema(db):
    rows=db.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if db.execute('PRAGMA user_version').fetchone()[0]!=1 or sorted(' '.join(r[0].split()) for r in rows)!=sorted(' '.join(s.split()) for s in SCHEMA):
        raise ValueError('read_model_schema_invalid')


@contextmanager
def connection(path, *, write=False):
    path=Path(os.path.abspath(path));parent=fd=db=None
    try:
        parent,name=_open_parent(path,private=True)
        fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW if write else os.O_RDONLY|os.O_NOFOLLOW,0o600,dir_fd=parent)
        info=os.fstat(fd)
        if not _private_file(info):raise ValueError('read_model_file_unsafe')
        identity=_file_identity(info)
        db=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=.25)
        db.execute('PRAGMA trusted_schema=OFF')
        if write and db.execute('PRAGMA user_version').fetchone()[0]==0:
            if db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:raise ValueError('read_model_schema_invalid')
            with db:
                for sql in SCHEMA:db.execute(sql)
                db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        _schema(db)
        if write:
            if db.execute('PRAGMA journal_mode').fetchone()[0]!='wal':db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
        if not write:db.execute('PRAGMA query_only=ON')
        yield db
        _check_parent(path,parent,private=True)
        if _file_identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=identity or _file_identity(os.fstat(fd))!=identity:
            raise ValueError('read_model_database_changed')
    finally:
        if db is not None:db.close()
        for handle in (fd,parent):
            if handle is not None:os.close(handle)


@contextmanager
def source_connection(view, *, seconds=8):
    parent=fd=db=None
    try:
        parent,name=_open_parent(view.database,private=True)
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
        identity=_file_identity(os.fstat(fd));view._guard(parent,name,identity,fd)
        db=sqlite3.connect(view.database.as_uri()+'?mode=ro',uri=True,timeout=.05)
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
        deadline=time.monotonic()+seconds;db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        db.execute('BEGIN')
        actual=db.execute("SELECT type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if db.execute('PRAGMA user_version').fetchone()[0]!=1 or db.execute('PRAGMA journal_mode').fetchone()[0]!='delete' or any(t!='table' for t,_ in actual) or sorted(' '.join(s.split()) for _,s in actual)!=sorted(' '.join(s.split()) for s in view._schema):
            raise ValueError('read_model_source_schema')
        yield db,identity
        view._guard(parent,name,identity,fd)
        db.rollback()
    finally:
        if db is not None:db.close()
        for handle in (fd,parent):
            if handle is not None:os.close(handle)


def source_key(view):
    return hashlib.sha256(encoded({'path':str(view.database),'stores':view.names,'origin':view.data_origin,'hours':view.hours,'base_interval':view.base_interval,'schema':list(view._schema),'policy':POLICY})).hexdigest()


def _record(row, view, now):
    record=_json(row[-1])
    if type(record) is not dict or any(record.get(k)!=row[i] for i,k in enumerate(_COLUMNS[:6])) or type(row[6]) is not int or row[6] not in (0,1) or record.get('ok') is not bool(row[6]) or _stamp(_time(row[7]))!=row[7] or _time(row[7])>now:
        raise ValueError('read_model_source_record_mismatch')
    if record['store_id'] not in view.names or record['api_profile']!='miniapp_gateway' or record['data_origin']!=view.data_origin:
        raise ValueError('read_model_source_scope')
    return record


def _day(record):
    timing=record['timing'];at=timing.get('request_started_at') or timing.get('checked_at')
    return _time(at).astimezone(timezone(timedelta(hours=8))).date().isoformat()


def pack(value, limit=DAY_LIMIT):
    raw=encoded(value)
    if len(raw)>limit:raise ValueError('read_model_partition_too_large')
    return gzip.compress(raw,compresslevel=1,mtime=0),hashlib.sha256(raw).hexdigest()


def unpack(blob,digest,limit=DAY_LIMIT):
    if not isinstance(blob,bytes) or len(blob)>limit:raise ValueError('read_model_snapshot_invalid')
    dec=zlib.decompressobj(31);raw=dec.decompress(blob,limit+1)
    if len(raw)>limit or not dec.eof or dec.unused_data or dec.unconsumed_tail or hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError('read_model_snapshot_invalid')
    return _json(raw)


class RollupWriter:
    def __init__(self,path,views):
        self.path=Path(path);self.views={source_key(v):v for v in views}
        names=[s for v in views for s in v.names]
        if not self.views or len(names)!=len(set(names)) or len(names)>128:raise ValueError('read_model_scope_invalid')
        with connection(self.path,write=True) as db,db:
            old={r[0] for r in db.execute('SELECT source_key FROM sources')}
            if old-set(self.views):raise ValueError('read_model_scope_changed')
            for key in self.views:db.execute('INSERT OR IGNORE INTO sources(source_key,scope_sha256) VALUES(?,?)',(key,key))

    def _dirty(self,db,key,store,day,now):
        db.execute('INSERT OR IGNORE INTO dirty VALUES(?,?,?,?)',(key,store,day,_stamp(now)))

    def ingest(self,key,*,now=None,records=256,bundles=4):
        view=self.views[key]
        if not 1<=records<=256 or not 1<=bundles<=16:raise ValueError('read_model_batch_invalid')
        with connection(self.path,write=True) as db,source_connection(view) as (source,identity):
            now=now or datetime.now(timezone.utc)
            saved=db.execute('SELECT identity_json,record_cursor,bundle_cursor,ready FROM sources WHERE source_key=?',(key,)).fetchone()
            pin=encoded(list(identity)).decode();after,linked=saved[1:3]
            if saved[0] is not None and saved[0]!=pin:raise ValueError('read_model_source_replaced')
            high=source.execute('SELECT coalesce(max(rowid),0) FROM archive_records').fetchone()[0]
            has_bundles=len(view._schema)>1
            bundle_high=source.execute('SELECT coalesce(max(rowid),0) FROM continuity_bundles').fetchone()[0] if has_bundles else 0
            if high<after or bundle_high<linked:raise ValueError('read_model_source_rewound')
            sizes=source.execute('SELECT rowid,length(CAST(record_json AS BLOB)) FROM archive_records WHERE rowid>? ORDER BY rowid LIMIT ?',(after,records)).fetchall()
            ids=[];size=0
            for rowid,length in sizes:
                if not 1<=length<=MAX_PACKET_BYTES:raise ValueError('read_model_source_record_size')
                if ids and size+length>MAX_PACKET_BYTES//2:break
                ids.append(rowid);size+=length
            values=[]
            if ids:
                marks=','.join('?' for _ in ids)
                rows=source.execute(f"SELECT rowid,{','.join(_COLUMNS)} FROM archive_records WHERE rowid IN ({marks}) ORDER BY rowid",ids).fetchall()
                values=[(row[0],_record(row[1:],view,now)) for row in rows]
                packet=build_packet([],as_of=_stamp(now),store_ids=list(view.names),api_profile='miniapp_gateway',data_origin=view.data_origin)
                packet['records']=[r for _,r in values];validate_packet(packet,as_of=_stamp(now))
            # Fetch one bounded bundle at a time, never a whole historical log.
            changes=[];last_bundle=linked
            for _ in range(bundles):
                row=source.execute('SELECT rowid,bundle_sha256,CASE WHEN length(CAST(bundle_json AS BLOB))<=? THEN bundle_json END,archived_at FROM continuity_bundles WHERE rowid>? ORDER BY rowid LIMIT 1',(MAX_PACKET_BYTES,last_bundle)).fetchone() if has_bundles else None
                if row is None:break
                if row[2] is None or hashlib.sha256(row[2].encode()).hexdigest()!=row[1] or _stamp(_time(row[3]))!=row[3] or _time(row[3])>now:raise ValueError('read_model_source_bundle_invalid')
                safe=validate_continuity_bundle(_json(row[2]),as_of=_stamp(now))
                for record in safe['packet']['records']+[a['record'] for a in safe['anchors']]:
                    if record['store_id'] not in view.names or record['data_origin']!=view.data_origin or record['api_profile']!='miniapp_gateway':raise ValueError('read_model_source_scope')
                    changes.append((record['store_id'],_day(record)))
                last_bundle=row[0]
            with db:
                for rowid,record in values:
                    day=_day(record);item=(key,record['observation_id'],rowid,record['store_id'],day,record['record_sha256'])
                    db.execute('INSERT OR IGNORE INTO pointers VALUES(?,?,?,?,?,?)',item)
                    if db.execute('SELECT * FROM pointers WHERE source_key=? AND observation_id=?',item[:2]).fetchone()!=item:raise ValueError('read_model_pointer_conflict')
                    self._dirty(db,key,record['store_id'],day,now)
                for store,day in changes:self._dirty(db,key,store,day,now)
                after=ids[-1] if ids else after
                caught=after==high and last_bundle==bundle_high
                db.execute('UPDATE sources SET identity_json=?,record_cursor=?,bundle_cursor=?,ready=?,backlog=?,error_code=NULL WHERE source_key=?',(pin,after,last_bundle,int(caught or saved[3]),(high-after)+(bundle_high-last_bundle),key))
            return {'records':len(values),'bundles':last_bundle-linked,'caught_up':caught}

    def refresh(self,key,store,day,*,now=None):
        view=self.views[key]
        with connection(self.path,write=True) as db,source_connection(view) as (source,identity):
            now=now or datetime.now(timezone.utc)
            rows=db.execute('SELECT source_rowid,observation_id,record_sha256 FROM pointers WHERE source_key=? AND store_id=? AND day=? ORDER BY source_rowid LIMIT 1001',(key,store,day)).fetchall()
            if not rows:return False  # bundle predecessor can arrive before its pointer page
            if len(rows)>1000:raise ValueError('read_model_partition_too_large')
            pin=db.execute('SELECT identity_json FROM sources WHERE source_key=?',(key,)).fetchone()[0]
            if pin!=encoded(list(identity)).decode():raise ValueError('read_model_source_replaced')
            marks=','.join('?' for _ in rows)
            found=source.execute(f"SELECT rowid,{','.join(_COLUMNS)} FROM archive_records WHERE rowid IN ({marks}) ORDER BY rowid",[r[0] for r in rows]).fetchall()
            if len(found)!=len(rows):raise ValueError('read_model_source_record_missing')
            packet=build_packet([],as_of=_stamp(now),store_ids=[store],api_profile='miniapp_gateway',data_origin=view.data_origin)
            for pointer,row in zip(rows,found):
                record=_record(row[1:],view,now)
                if row[0]!=pointer[0] or record['observation_id']!=pointer[1] or record['record_sha256']!=pointer[2] or record['store_id']!=store or _day(record)!=day:raise ValueError('read_model_source_record_mismatch')
                packet['records'].append(record)
            detail=view._project(source,packet,store,now)['days'][(store,day)]
            detail.update(view._common(now),full_source_arrays_persisted=True,persistence_semantics='validated_selected_records_in_packet_archive')
            detail['summary'].update(selection_scope=detail['selection_scope'],full_source_arrays_persisted=True,persistence_semantics=detail['persistence_semantics'])
            summary=encoded(detail['summary']).decode();values=[pack(detail),pack(summarize_day(detail,'ordinary',now)),pack(summarize_day(detail,'reservation',now))]
            with db:
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(store,day,key,_stamp(now),values[0][0],values[0][1],summary,hashlib.sha256(summary.encode()).hexdigest(),values[1][0],values[1][1],values[2][0],values[2][1]))
                db.execute('DELETE FROM dirty WHERE source_key=? AND store_id=? AND day=?',(key,store,day))
            return True

    def step(self,*,max_days=32,now=None):
        began=time.monotonic();accepted=0;errors=[];deferred=0
        for key in self.views:
            try:
                result=self.ingest(key,now=now,bundles=16);accepted+=result['records']+result['bundles']
            except sqlite3.OperationalError as error:
                if getattr(error,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    deferred+=1;continue
                with connection(self.path,write=True) as db,db:db.execute("UPDATE sources SET ready=0,error_code='read_model_source_unavailable' WHERE source_key=?",(key,))
                errors.append(key)
            except Exception:
                with connection(self.path,write=True) as db,db:db.execute("UPDATE sources SET ready=0,error_code='read_model_source_unavailable' WHERE source_key=?",(key,))
                errors.append(key)
        with connection(self.path) as db:
            jobs=db.execute('SELECT d.source_key,d.store_id,d.day FROM dirty d JOIN sources s USING(source_key) WHERE s.ready=1 AND s.error_code IS NULL ORDER BY d.first_seen,d.source_key,d.store_id,d.day LIMIT ?',(max_days,)).fetchall()
        for key,store,day in jobs:
            try:self.refresh(key,store,day,now=now)
            except Exception:
                with connection(self.path,write=True) as db,db:db.execute("UPDATE sources SET error_code='read_model_partition_unavailable' WHERE source_key=?",(key,))
                errors.append(key)
        with connection(self.path) as db:
            pending=db.execute('SELECT count(*) FROM dirty').fetchone()[0]
            unready=db.execute('SELECT count(*) FROM sources WHERE ready=0 OR backlog>0').fetchone()[0]
            built=db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            persistent_errors=db.execute('SELECT count(*) FROM sources WHERE error_code IS NOT NULL').fetchone()[0]
            uninitialized=db.execute('SELECT count(*) FROM sources WHERE ready=0').fetchone()[0]
            oldest=db.execute('SELECT min(first_seen) FROM dirty').fetchone()[0]
        return {'policy':POLICY,'accepted':accepted,'pending_days':pending,'pending_sources':unready,'error_sources':persistent_errors,'snapshots':built,'uninitialized_sources':uninitialized,'oldest_pending_at':oldest,'deferred_sources':deferred,'seconds':round(time.monotonic()-began,3),'caught_up':not pending and not unready and not errors}


class ReadModelView:
    def __init__(self,path,views,*,status_file=None):
        self.path=Path(path);self.views=list(views);self.status_file=Path(status_file) if status_file else None
        self.names={s:n for v in views for s,n in v.names.items()};self.by_store={s:v for v in views for s in v.names}
        if len(self.names)!=sum(len(v.names) for v in views):raise ValueError('read_model_scope_invalid')
        with connection(self.path) as db:
            if {r[0] for r in db.execute('SELECT source_key FROM sources')}!={source_key(v) for v in views}:raise ValueError('read_model_scope_changed')

    def health(self,now=None):
        if self.status_file is None:return {'state':'running','data_available':True}
        from .credentials import _read_private_file
        now=now or datetime.now(timezone.utc)
        value=_json(_read_private_file(self.status_file,max_bytes=8192))
        age=(now-_time(value['at'])).total_seconds()
        if value.get('policy')!=POLICY or age < -60 or age>120:
            return {'state':'needs_attention','data_available':False}
        # A quarantined source or a capacity warning must not hide healthy
        # partitions. Individual source errors are checked by each day/month.
        available=value.get('data_available',value.get('state')=='running') is True
        return {'state':'running' if value.get('state')=='running' else 'needs_attention',
                'data_available':available,'storage_pressure':value.get('storage_pressure') is True}

    def allowed(self,path):return any(v.allowed(path) for v in self.views)

    def _day_value(self,db,store,day,now):
        row=db.execute('SELECT detail,detail_sha256,built_at,source_key FROM snapshots WHERE store_id=? AND day=?',(store,day)).fetchone()
        if row is None:return self.by_store[store]._empty_day(store,day,now)
        status=db.execute('SELECT ready,error_code FROM sources WHERE source_key=?',(row[3],)).fetchone()
        if status is None or not status[0] or status[1]:raise ValueError('read_model_source_unavailable')
        detail=unpack(row[0],row[1]);detail.update(self.by_store[store]._common(now),read_model_policy=POLICY,read_model_built_at=row[2])
        # Source freshness remains unknown; retain actual point timestamps.
        summary=detail['summary'];through=min(now,_time(day+'T00:00:00+08:00')+timedelta(days=1))
        import math
        elapsed=sum(math.ceil(max(0, (min(_time(b),through)-_time(a)).total_seconds())/self.by_store[store].base_interval) for a,b in summary['declared_intervals'] if through>_time(a))
        summary.update(expected_background_slots_so_far=elapsed,observed_slot_fraction_so_far=min(1,summary['observed_background_slots']/elapsed) if elapsed else None)
        return detail

    def _read(self,db,path,now):
        match=_DAY.fullmatch(path)
        if match:return self._day_value(db,match[1],match[2],now)
        month=_MONTH.fullmatch(path)[1];first=month+'-01';following=(datetime.fromisoformat(first).replace(day=28)+timedelta(days=4)).replace(day=1).date().isoformat()
        result={**self.views[0]._common(now),'read_model_policy':POLICY,'month':month,'configured_store_ids':list(self.names),'store_names':self.names,'unavailable_store_ids':[],'days':{},'timezone':'Asia/Shanghai','heatmap_semantics':'coverage_only_traffic_not_calibrated'}
        for key,ready,error in db.execute('SELECT source_key,ready,error_code FROM sources'):
            if not ready or error:result['unavailable_store_ids'] += [s for v in self.views if source_key(v)==key for s in v.names]
        for store,day,raw,digest in db.execute('SELECT store_id,day,summary,summary_sha256 FROM snapshots WHERE day>=? AND day<? ORDER BY day,store_id',(first,following)):
            if store in result['unavailable_store_ids']:continue
            if store not in self.names or hashlib.sha256(raw.encode()).hexdigest()!=digest:raise ValueError('read_model_snapshot_invalid')
            summary=_json(raw)
            result['days'].setdefault(day,{})[store]=summary
        return result

    def dispatch(self,path):
        if not self.allowed(path):return 404,b'Unknown saved route','text/plain'
        try:
            if not self.health()['data_available']:raise ValueError('read_model_publisher_unavailable')
            with connection(self.path) as db:
                db.execute('BEGIN');value=self._read(db,path,datetime.now(timezone.utc));body=encoded(value)
                if len(body)>(8*1024*1024 if _MONTH.fullmatch(path) else DAY_LIMIT):raise ValueError('read_model_response_too_large')
                return 200,body,'application/json'
        except Exception:return 503,b'{"error_code":"read_model_unavailable","source":"sapi_miniapp_gateway"}','application/json'

    @contextmanager
    def report_reader(self):
        if not self.health()['data_available']:raise ValueError('read_model_publisher_unavailable')
        with connection(self.path) as db:
            db.execute('BEGIN');view=self
            class Reader:
                names=view.names
                def dispatch(self,path):
                    if not view.allowed(path):return 404,b'{}','application/json'
                    return 200,encoded(view._read(db,path,datetime.now(timezone.utc))),'application/json'
                def summarized_day(self,store,day,queue,now):
                    column='ordinary' if queue=='ordinary' else 'reservation'
                    row=db.execute(f'SELECT {column},{column}_sha256 FROM snapshots WHERE store_id=? AND day=?',(store,day)).fetchone()
                    if row is None:raise ValueError('read_model_day_missing')
                    value=unpack(*row);start,end=opening(datetime.fromisoformat(day).date(),now)
                    import math
                    value['expected_slots']=max(0,math.ceil((end-start).total_seconds()/900))
                    return value
            yield Reader()
