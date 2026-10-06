"""Private bounded anonymous collection checkpoints; no auth or replay.

An attempted pair consumes one slot even if a crash leaves its outcome unknown.
The exact committed database prefix is checked before any resumed HTTP request.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from .capture import _check_parent, _open_parent
from .credentials import _identity, _private_file, _read_private_file
from .remote import SOURCE, MAX_RECORD, _id, validate_record

_MAX = 16 * 1024
_EMPTY = hashlib.sha256(b'').hexdigest()
_KEYS = {'schema_version','source','config','database_identity','initial_id',
    'cursor','successful','failed','uncertain','recorded_http_attempts',
    'records_digest','pending','last_attempt_at','updated_at','state','last_gap'}


class RemoteTaskError(ValueError):
    pass


def _integer(value, low=0, high=2**63-1):
    return type(value) is int and low <= value <= high


def _at(value):
    if not isinstance(value,str) or len(value)>40:
        raise RemoteTaskError('remote_task_invalid')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise RemoteTaskError('remote_task_invalid')
    return result.astimezone(timezone.utc)


def _now(value):
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RemoteTaskError('remote_task_clock_invalid')
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def _json(body):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError
            result[key]=value
        return result
    return json.loads(body,object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _decode(body):
    try:
        if len(body)>_MAX:raise ValueError
        v=_json(body)
        if (not isinstance(v,dict) or set(v)!=_KEYS or type(v['schema_version']) is not int
                or v['schema_version']!=1 or v['source']!=SOURCE):raise ValueError
        c=v['config']
        if (not isinstance(c,dict) or set(c)!={'db','store_ids','interval','samples'}
                or not isinstance(c['db'],str) or len(c['db'])>4096 or not Path(c['db']).is_absolute()
                or not isinstance(c['store_ids'],list) or not 1<=len(c['store_ids'])<=3
                or len(set(c['store_ids']))!=len(c['store_ids'])
                or not _integer(c['interval'],30,3600) or not _integer(c['samples'],1,120)):raise ValueError
        for store in c['store_ids']:_id(store)
        total=len(c['store_ids'])*c['samples']
        if (not all(_integer(v[k],0,total) for k in ('cursor','successful','failed','uncertain'))
                or v['failed']>1 or v['cursor']!=v['successful']+v['failed']+v['uncertain']
                or not _integer(v['initial_id'])
                or not _integer(v['recorded_http_attempts'],0,2*(v['successful']+v['failed']))
                or v['state'] not in ('ready','running','completed','failed')
                or (v['state']=='failed')!=(v['failed']==1)
                or (v['state']=='completed')!=(v['cursor']==total and v['failed']==0)
                or not isinstance(v['records_digest'],str) or len(v['records_digest'])!=64
                or any(x not in '0123456789abcdef' for x in v['records_digest'])):raise ValueError
        identity=v['database_identity']
        if (not isinstance(identity,list) or len(identity)!=2 or not all(_integer(x) for x in identity)):raise ValueError
        updated=_at(v['updated_at'])
        if v['last_attempt_at'] is not None and _at(v['last_attempt_at'])>updated:raise ValueError
        p=v['pending']
        if p is not None:
            if (not isinstance(p,dict) or set(p)!={'cursor','run_id','after_id','started_at'}
                    or p['cursor']!=v['cursor'] or not _integer(p['cursor'],0,total-1)
                    or not _integer(p['after_id'])
                    or p['after_id']!=v['initial_id']+v['successful']+v['failed']
                    or not isinstance(p['run_id'],str) or len(p['run_id'])!=36
                    or v['state']!='running' or _at(p['started_at'])>updated):raise ValueError
            UUID(p['run_id'])
        if (v['state']=='running')!=(p is not None):raise ValueError
        gap=v['last_gap']
        if gap is not None:
            if (not isinstance(gap,dict) or set(gap)!={'from','to','reason'}
                    or gap['reason'] not in ('restart','uncertain_attempt')
                    or _at(gap['to'])<_at(gap['from']) or _at(gap['to'])>updated):raise ValueError
        return v
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError,UnicodeError):
        raise RemoteTaskError('remote_task_invalid') from None


def task_config(db, store_ids, interval, samples):
    return {'db':os.path.abspath(db),'store_ids':list(store_ids),'interval':interval,'samples':samples}


def public_status(v):
    c=v['config'];total=len(c['store_ids'])*c['samples']
    return {'task_schema_version':1,'source':SOURCE,'state':v['state'],
        'store_ids':list(c['store_ids']),'interval_seconds':c['interval'],
        'target_rounds':c['samples'],'target_pair_slots':total,'maximum_request_budget':2*total,
        'completed_pair_slots':v['cursor'],'successful_pairs':v['successful'],
        'failed_pairs':v['failed'],'uncertain_pair_slots':v['uncertain'],
        'recorded_http_attempts':v['recorded_http_attempts'],
        'unrecorded_http_attempts':'unknown' if v['uncertain'] or v['pending'] else 0,
        'pending_attempt':v['pending'] is not None,
        'all_slots_successful':v['state']=='completed' and v['uncertain']==0,
        'updated_at':v['updated_at'],'last_gap':deepcopy(v['last_gap']),
        'process_liveness':'unknown','retries':0,'source_freshness':'unknown',
        'eta_available':False,'verified_training_labels':0}


def remote_task_status(path):
    try:return public_status(_decode(_read_private_file(path)))
    except (OSError,ValueError):
        raise RemoteTaskError('remote_task_unavailable_or_unsafe') from None


class RemoteTask:
    def __init__(self,path,*,config,resume,now):
        self.path=Path(os.path.abspath(path));self.parent_fd=self.lock_fd=None
        self.identity=None;self.loaded=False;self.db=None
        try:
            import fcntl
            self.parent_fd,self.name=_open_parent(self.path,private=True)
            self.lock_name=self.name+'.lock'
            if Path(config['db']) in (self.path,Path(str(self.path)+'.lock')):
                raise RemoteTaskError('remote_task_path_conflict')
            self.lock_fd=os.open(self.lock_name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self.parent_fd)
            info=os.fstat(self.lock_fd)
            if not _private_file(info) or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError
            self.lock_identity=(info.st_dev,info.st_ino)
            fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:self.identity=self._named_identity()
            except FileNotFoundError:pass
            if self.identity is not None:
                if not resume:raise RemoteTaskError('remote_task_exists_use_resume')
                self.value=_decode(_read_private_file(self.path))
                if self.value['config']!=config:raise RemoteTaskError('remote_task_config_conflict')
                self.loaded=True
            elif resume:raise RemoteTaskError('remote_task_missing')
            else:
                self.value={'schema_version':1,'source':SOURCE,'config':deepcopy(config),
                    'database_identity':[0,0],'initial_id':0,'cursor':0,'successful':0,
                    'failed':0,'uncertain':0,'recorded_http_attempts':0,'records_digest':_EMPTY,
                    'pending':None,'last_attempt_at':None,'updated_at':_now(now),'state':'ready','last_gap':None}
                _decode(json.dumps(self.value).encode())
            self._guard()
        except BaseException as error:
            self.close()
            if isinstance(error,(KeyboardInterrupt,SystemExit,RemoteTaskError)):raise
            raise RemoteTaskError('remote_task_busy' if isinstance(error,BlockingIOError)
                else 'remote_task_unavailable_or_unsafe') from None

    def _named_identity(self):
        info=os.stat(self.name,dir_fd=self.parent_fd,follow_symlinks=False)
        if not _private_file(info) or stat.S_IMODE(info.st_mode)!=0o600:
            raise RemoteTaskError('remote_task_unavailable_or_unsafe')
        return _identity(info)

    def _guard(self):
        _check_parent(self.path,self.parent_fd,private=True)
        info=os.stat(self.lock_name,dir_fd=self.parent_fd,follow_symlinks=False)
        if (not _private_file(info) or stat.S_IMODE(info.st_mode)!=0o600
                or (info.st_dev,info.st_ino)!=self.lock_identity):
            raise RemoteTaskError('remote_task_lock_changed')
        try:identity=self._named_identity()
        except FileNotFoundError:identity=None
        if identity!=self.identity:raise RemoteTaskError('remote_task_changed')

    def _commit(self,value):
        body=json.dumps(value,sort_keys=True,allow_nan=False).encode();_decode(body);self._guard()
        name=self.name+'.'+uuid4().hex+'.tmp';fd=None;renamed=False
        try:
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.parent_fd)
            view=memoryview(body)
            while view:
                written=os.write(fd,view)
                if written<=0:raise OSError
                view=view[written:]
            os.fsync(fd);self._guard()
            os.replace(name,self.name,src_dir_fd=self.parent_fd,dst_dir_fd=self.parent_fd)
            renamed=True;self.identity=self._named_identity();self.value=value;os.fsync(self.parent_fd)
        except OSError:
            raise RemoteTaskError('remote_task_durability_unconfirmed' if renamed else 'remote_task_write_failed') from None
        finally:
            if fd is not None:os.close(fd)
            if not renamed:
                try:os.unlink(name,dir_fd=self.parent_fd)
                except FileNotFoundError:pass

    def prepare_database(self):
        fd=None
        try:
            path=Path(self.value['config']['db']);fd,name=_open_parent(path,private=True)
            try:info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:
                if self.loaded:raise RemoteTaskError('remote_task_database_changed') from None
                return
            if not _private_file(info) or stat.S_IMODE(info.st_mode)!=0o600:
                raise RemoteTaskError('remote_task_database_unsafe')
            if self.loaded and [info.st_dev,info.st_ino]!=self.value['database_identity']:
                raise RemoteTaskError('remote_task_database_changed')
        finally:
            if fd is not None:os.close(fd)

    def _rows(self):
        self._guard();self.db._guard()
        if list(self.db.identity)!=self.value['database_identity']:
            raise RemoteTaskError('remote_task_database_changed')
        rows=self.db.db.execute('SELECT id,run_id,store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
            'THEN payload_json END FROM remote_samples WHERE id>? ORDER BY id LIMIT 362',
            (MAX_RECORD,self.value['initial_id'])).fetchall()
        count=self.value['successful']+self.value['failed']
        if len(rows) not in ((count,count+1) if self.value['pending'] else (count,)):
            raise RemoteTaskError('remote_task_result_conflict')
        digest=_EMPTY;attempts=0
        for index,row in enumerate(rows):
            if row[0]!=self.value['initial_id']+index+1:raise RemoteTaskError('remote_task_result_conflict')
            try:
                UUID(row[1]);record=validate_record(_json(row[4]))
                if row[2]!=record['requested_store_id'] or row[2] not in self.value['config']['store_ids'] or row[3]!=int(record['ok']):raise ValueError
            except (ValueError,TypeError):raise RemoteTaskError('remote_task_result_conflict') from None
            if index<count:
                digest=_chain(digest,row);attempts+=sum(q['attempted'] for q in record['queries'].values())
        if digest!=self.value['records_digest'] or attempts!=self.value['recorded_http_attempts']:
            raise RemoteTaskError('remote_task_database_changed')
        return rows[count:]

    def bind(self,db,*,now):
        self.db=db
        if str(db.path)!=self.value['config']['db']:raise RemoteTaskError('remote_task_config_conflict')
        at=_now(now)
        if self.loaded and _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_task_clock_rollback')
        if not self.loaded:
            value=deepcopy(self.value);value['database_identity']=list(db.identity)
            value['initial_id']=db.db.execute('SELECT COALESCE(MAX(id),0) FROM remote_samples').fetchone()[0]
            self._commit(value)
        self._rows();unknown=self.value['uncertain']
        if self.value['pending']:self.reconcile(now=now,interrupted=True)
        if self.loaded and self.value['last_attempt_at'] and self.value['state'] not in ('completed','failed') and self.value['uncertain']==unknown:
            value=deepcopy(self.value);value['last_gap']={'from':value['last_attempt_at'],'to':at,'reason':'restart'}
            value['updated_at']=at;self._commit(value)

    def begin(self,*,now):
        at=_now(now);self._rows()
        if self.value['state']!='ready':raise RemoteTaskError('remote_task_invalid')
        if _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_task_clock_rollback')
        value=deepcopy(self.value);value['pending']={'cursor':value['cursor'],'run_id':self.db.run_id,
            'after_id':value['initial_id']+value['successful']+value['failed'],'started_at':at}
        value.update(state='running',last_attempt_at=at,updated_at=at);self._commit(value)

    def reconcile(self,*,now,interrupted=False):
        pending=self.value['pending'];at=_now(now)
        if pending is None:raise RemoteTaskError('remote_task_invalid')
        if _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_task_clock_rollback')
        rows=self._rows();value=deepcopy(self.value)
        if not rows and not interrupted:raise RemoteTaskError('remote_task_result_conflict')
        if rows:
            row=rows[0];record=validate_record(_json(row[4]))
            expected=value['config']['store_ids'][value['cursor']%len(value['config']['store_ids'])]
            if row[1]!=pending['run_id'] or row[2]!=expected or _at(record['queries']['groupqueues']['started_at'])<_at(pending['started_at']):
                raise RemoteTaskError('remote_task_result_conflict')
            value['successful' if record['ok'] else 'failed']+=1
            value['recorded_http_attempts']+=sum(q['attempted'] for q in record['queries'].values())
            value['records_digest']=_chain(value['records_digest'],row)
        else:
            value['uncertain']+=1
            value['last_gap']={'from':pending['started_at'],'to':at,'reason':'uncertain_attempt'}
        value['cursor']+=1;value['pending']=None;value['updated_at']=at
        total=len(value['config']['store_ids'])*value['config']['samples']
        value['state']='failed' if value['failed'] else 'completed' if value['cursor']==total else 'ready'
        self._commit(value)

    def close(self):
        for attribute in ('lock_fd','parent_fd'):
            fd=getattr(self,attribute,None)
            if fd is not None:os.close(fd);setattr(self,attribute,None)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def _chain(digest,row):
    body=json.dumps(row,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(bytes.fromhex(digest)+body).hexdigest()


def collect_remote_task(task,client,*,wall_clock,monotonic_clock,sleep,emit):
    """Continue remaining bounded slots, waiting a full interval after restart."""
    value=task.value;c=value['config'];total=len(c['store_ids'])*c['samples']
    target=monotonic_clock()+(c['interval'] if task.loaded else 0);round_start=None
    while task.value['cursor']<total and task.value['state']!='failed':
        if round_start is None:
            delay=max(0,target-monotonic_clock())
            if delay>0:sleep(delay)
            round_start=monotonic_clock()
        task.begin(now=wall_clock())
        cursor=task.value['cursor'];store=c['store_ids'][cursor%len(c['store_ids'])]
        record=client.snapshot(store);identifier=task.db.append(record)
        task.reconcile(now=wall_clock())
        emit({'id':identifier,'pair_slot':cursor+1,'record':record})
        if task.value['cursor']%len(c['store_ids'])==0:
            target=round_start+c['interval']
            if monotonic_clock()>target:target=monotonic_clock()+c['interval']
            round_start=None
    summary=public_status(task.value)
    summary['ok']=summary['all_slots_successful']
    emit({'remote_task_summary':summary})
    return summary
