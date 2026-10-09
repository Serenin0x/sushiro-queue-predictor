"""Persisted shared polling windows, explicit deadlines and no catch-up replay."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib,json,math,os
from pathlib import Path
from uuid import UUID

from .remote import SOURCE,MAX_RECORD,_id,validate_record
from .remotetasks import RemoteTask,RemoteTaskError,_at,_now,_json,_integer,_EMPTY,_chain
from .credentials import _read_private_file
from .shared_monitoring import read_plan_file,shared_polling_policy
from .remoteservice import RemoteQueueService
from .planupdates import read_update,check_successor,PlanUpdateError,MAX_REVISION

MAX_PAIRS=25920
MAX_DURATION=72*3600
_KEYS={'schema_version','source','config','database_identity','initial_id','cursor',
    'successful','failed','uncertain','recorded_http_attempts','records_digest','pending',
    'last_attempt_at','updated_at','state','last_gap','created_at','deadline_at','end_reason','starts'}


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _plans(path,stores,base,now):
    value=read_plan_file(path)
    if value.get('last_poll_started_at') not in (None,{}):raise RemoteTaskError('remote_window_borrowed_poll_start')
    policy=shared_polling_policy(value,as_of=_now(now),base_interval=base)
    if any(s['store_id'] not in stores for s in policy['stores']):raise RemoteTaskError('remote_window_store_scope')
    return value


def window_config(db,plan_file,store_ids,base_interval,duration_seconds,max_pairs,*,now,plan_updates_file=None,business_hours=None):
    if (type(store_ids) is not list or not 1<=len(store_ids)<=3 or len(set(store_ids))!=len(store_ids)
            or not _integer(base_interval,60,3600) or not _integer(duration_seconds,30,MAX_DURATION)
            or not _integer(max_pairs,1,MAX_PAIRS)):
        raise RemoteTaskError('remote_window_invalid_config')
    for store in store_ids:_id(store)
    document=_plans(plan_file,store_ids,base_interval,now)
    config={'db':os.path.abspath(db),'plan_file':os.path.abspath(plan_file),
        'plan_digest':_digest(document),'store_ids':list(store_ids),'base_interval':base_interval,
        'duration_seconds':duration_seconds,'max_pairs':max_pairs}
    if plan_updates_file is not None:
        path=os.path.abspath(plan_updates_file)
        if path==config['plan_file'] or Path(path).parent==Path(config['db']).parent:
            raise RemoteTaskError('remote_window_updates_path_conflict')
        read_update(path,stores=store_ids,base_interval=base_interval,now=now)
        config['plan_updates_file']=path
    if business_hours is not None:
        from .businesshours import validate_hours
        config['business_hours']=validate_hours(business_hours)
    return config


def _decode_window(body):
    try:
        if len(body)>16*1024:raise ValueError
        v=_json(body);c=v['config']
        if type(v.get('schema_version')) is int and v['schema_version'] in (4,5):
            from .businesshours import validate_hours
            feed=v['schema_version']==5
            if (set(v)!=_KEYS|{'scheduled_pauses'}|({'plan_context'} if feed else set())
                    or type(c) is not dict or 'business_hours' not in c
                    or not _integer(v['scheduled_pauses'],0,v['cursor'])
                    or not _integer(v['successful'],0,v['cursor'])):raise ValueError
            validate_hours(c['business_hours'])
            base=deepcopy(v);base['schema_version']=3 if feed else 2
            base['config'].pop('business_hours');base['successful']+=base.pop('scheduled_pauses')
            _decode_window(json.dumps(base,separators=(',',':')).encode())
            return v
        if type(v.get('schema_version')) is int and v['schema_version']==3:
            if (set(v)!=_KEYS|{'plan_context'} or type(c) is not dict
                    or set(c)!={'db','plan_file','plan_digest','store_ids','base_interval','duration_seconds','max_pairs','plan_updates_file'}):raise ValueError
            path=c['plan_updates_file'];p=v['plan_context']
            if (type(path) is not str or len(path)>4096 or not Path(path).is_absolute()
                    or path==c['plan_file'] or Path(path).parent==Path(c['db']).parent
                    or type(p) is not dict or set(p)!={'series_id','revision','document_digest','declared_at','applied_at','unobserved_revisions'}
                    or type(p['series_id']) is not str or str(UUID(p['series_id']))!=p['series_id'] or UUID(p['series_id']).version!=4
                    or not _integer(p['revision'],1,MAX_REVISION)
                    or not _integer(p['unobserved_revisions'],0,p['revision']-1)
                    or type(p['document_digest']) is not str or len(p['document_digest'])!=64
                    or any(x not in '0123456789abcdef' for x in p['document_digest'])
                    or not _at(v['created_at'])<=_at(p['applied_at'])<=_at(v['updated_at'])
                    # Task times are millisecond floors; retain the full
                    # declaration in its digest and rollback checks, but compare
                    # chronology at the checkpoint's stored resolution.
                    or _at(p['declared_at']).replace(microsecond=(_at(p['declared_at']).microsecond//1000)*1000)>_at(p['applied_at'])):raise ValueError
            base=deepcopy(v);base['schema_version']=2;base.pop('plan_context');base['config'].pop('plan_updates_file')
            _decode_window(json.dumps(base,separators=(',',':')).encode())
            return v
        if (type(v) is not dict or set(v)!=_KEYS or type(v['schema_version']) is not int or v['schema_version']!=2
                or v['source']!=SOURCE or type(c) is not dict
                or set(c)!={'db','plan_file','plan_digest','store_ids','base_interval','duration_seconds','max_pairs'}):raise ValueError
        for key in ('db','plan_file'):
            if type(c[key]) is not str or len(c[key])>4096 or not Path(c[key]).is_absolute():raise ValueError
        if c['db']==c['plan_file']:raise ValueError
        if (type(c['store_ids']) is not list or not 1<=len(c['store_ids'])<=3
                or len(set(c['store_ids']))!=len(c['store_ids'])
                or not _integer(c['base_interval'],60,3600) or not _integer(c['duration_seconds'],30,MAX_DURATION)
                or not _integer(c['max_pairs'],1,MAX_PAIRS)):raise ValueError
        for store in c['store_ids']:_id(store)
        for digest in (c['plan_digest'],v['records_digest']):
            if type(digest) is not str or len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ValueError
        if (not all(_integer(v[k],0,c['max_pairs']) for k in ('cursor','successful','failed','uncertain'))
                or v['failed']>1 or v['cursor']!=v['successful']+v['failed']+v['uncertain']
                or not _integer(v['initial_id'])
                or not _integer(v['recorded_http_attempts'],0,2*(v['successful']+v['failed']))
                or v['state'] not in ('ready','running','completed','failed')
                or (v['state']=='failed')!=(v['failed']==1)):raise ValueError
        created,deadline,updated=_at(v['created_at']),_at(v['deadline_at']),_at(v['updated_at'])
        if deadline!=created+timedelta(seconds=c['duration_seconds']) or updated<created:raise ValueError
        identity=v['database_identity']
        if type(identity) is not list or len(identity)!=2 or not all(_integer(x) for x in identity):raise ValueError
        starts=v['starts']
        if type(starts) is not dict or set(starts)-set(c['store_ids']):raise ValueError
        for at in starts.values():
            if not created<=_at(at)<=updated:raise ValueError
        last=v['last_attempt_at']
        if last is not None and not created<=_at(last)<=updated:raise ValueError
        if last is None and (v['cursor'] or starts):raise ValueError
        p=v['pending']
        if (v['state']=='running')!=(p is not None):raise ValueError
        if p is not None:
            if (type(p) is not dict or set(p)!={'cursor','store_id','run_id','after_id','started_at'}
                    or p['cursor']!=v['cursor'] or not _integer(p['cursor'],0,c['max_pairs']-1)
                    or p['store_id'] not in c['store_ids']
                    or p['after_id']!=v['initial_id']+v['successful']+v['failed']
                    or type(p['run_id']) is not str or len(p['run_id'])!=36
                    or p['started_at']!=last or starts.get(p['store_id'])!=last or _at(last)>=deadline):raise ValueError
            UUID(p['run_id'])
        reason=v['end_reason']
        if (v['state']=='completed')!=(reason in ('deadline','budget','monotonic_duration')):raise ValueError
        if reason=='budget' and v['cursor']!=c['max_pairs']:raise ValueError
        if reason=='deadline' and updated<deadline:raise ValueError
        if reason not in (None,'deadline','budget','monotonic_duration'):raise ValueError
        gap=v['last_gap']
        if gap is not None:
            if (type(gap) is not dict or set(gap)!={'from','to','reason'}
                    or gap['reason'] not in ('restart','uncertain_attempt')
                    or not created<=_at(gap['from'])<=_at(gap['to'])<=updated):raise ValueError
        return v
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError):
        raise RemoteTaskError('remote_window_invalid') from None


def window_status(v):
    c=v['config']
    result={'task_schema_version':v['schema_version'],'source':SOURCE,'mode':'persistent_shared_window','state':v['state'],
        'store_ids':list(c['store_ids']),'base_interval_seconds':c['base_interval'],
        'duration_seconds':c['duration_seconds'],'deadline_at':v['deadline_at'],'end_reason':v['end_reason'],
        'maximum_pair_budget':c['max_pairs'],'maximum_request_budget':2*c['max_pairs'],
        'completed_pair_slots':v['cursor'],'successful_pairs':v['successful'],'failed_pairs':v['failed'],
        'uncertain_pair_slots':v['uncertain'],'recorded_http_attempts':v['recorded_http_attempts'],
        'unrecorded_http_attempts':'unknown' if v['pending'] or v['uncertain'] else 0,
        'pending_attempt':v['pending'] is not None,'updated_at':v['updated_at'],'last_gap':deepcopy(v['last_gap']),
        'catch_up_requests':0,'process_liveness':'unknown','source_freshness':'unknown',
        'eta_available':False,'verified_training_labels':0}
    if 'business_hours' in c:
        result.update(business_hours_enabled=True,scheduled_pause_slots=v['scheduled_pauses'],
            all_pairs_successful=not(v['failed'] or v['uncertain'] or v['scheduled_pauses']),
            business_hours_source='user_assumed',business_hours_verified=False)
    if 'plan_context' in v:
        result.update(plan_updates_enabled=True,accepted_plan_revision=v['plan_context']['revision'],
            unobserved_plan_revisions=v['plan_context']['unobserved_revisions'],complete_plan_history_verified=False)
    return result


def remote_window_status(path):
    return window_status(_decode_window(_read_private_file(path)))


class RemoteWindowTask(RemoteTask):
    decode=staticmethod(_decode_window)

    @staticmethod
    def initial_value(config,now):
        value=RemoteTask.initial_value(config,now)
        value.update(schema_version=2,created_at=_now(now),
            deadline_at=_now(_at(_now(now))+timedelta(seconds=config['duration_seconds'])),end_reason=None,starts={})
        if 'plan_updates_file' in config:
            update=read_update(config['plan_updates_file'],stores=config['store_ids'],base_interval=config['base_interval'],now=now)
            value.update(schema_version=3,plan_context={'series_id':update['series_id'],'revision':update['revision'],
                'document_digest':_digest(update),'declared_at':update['declared_at'],'applied_at':_now(now),
                'unobserved_revisions':update['revision']-1})
        if 'business_hours' in config:
            value.update(schema_version=5 if 'plan_context' in value else 4,scheduled_pauses=0)
        return value

    def __init__(self,path,*,config,resume,now,resume_if_present=False):
        if config['plan_file'] in (os.path.abspath(path),os.path.abspath(str(path)+'.lock'),config['db']):
            raise RemoteTaskError('remote_window_path_conflict')
        if 'plan_updates_file' in config and Path(config['plan_updates_file']).parent==Path(os.path.abspath(path)).parent:
            raise RemoteTaskError('remote_window_updates_path_conflict')
        self.document=_plans(config['plan_file'],config['store_ids'],config['base_interval'],now)
        if _digest(self.document)!=config['plan_digest']:raise RemoteTaskError('remote_window_plan_changed')
        self.fingerprint=None;self.data_version=None
        super().__init__(path,config=config,resume=resume,now=now,resume_if_present=resume_if_present)
        try:
            if 'plan_context' in self.value and self.value['state'] not in ('completed','failed'):
                self.document=self._checked_update(now=now)['document']
        except Exception:
            self.close();raise

    def _checked_update(self,*,now=None):
        config=self.value['config'];context=self.value['plan_context']
        update=read_update(config['plan_updates_file'],stores=config['store_ids'],base_interval=config['base_interval'],now=now)
        check_successor(context,update,old_digest=context['document_digest'])
        return update

    def refresh_plans(self,*,now):
        """Apply only between pairs; task deadline, budget, starts and cursor remain."""
        if 'plan_context' not in self.value or self.value['state']!='ready':return
        if _at(_now(now))<_at(self.value['updated_at']):raise RemoteTaskError('remote_window_clock_rollback')
        update=self._checked_update(now=now);context=self.value['plan_context']
        if update['revision']>context['revision']:
            self._tail();value=deepcopy(self.value)
            value['plan_context']={'series_id':update['series_id'],'revision':update['revision'],
                'document_digest':_digest(update),'declared_at':update['declared_at'],'applied_at':_now(now),
                'unobserved_revisions':context['unobserved_revisions']+update['revision']-context['revision']-1}
            value['updated_at']=_now(now);self._commit(value)
        self.document=deepcopy(update['document'])

    def _fingerprint(self):
        info=os.stat(self.db.path,follow_symlinks=False)
        return info.st_size,info.st_mtime_ns,info.st_ctime_ns

    def _remember_database(self):
        self.fingerprint=self._fingerprint()
        self.data_version=self.db.db.execute('PRAGMA data_version').fetchone()[0]

    def _database_guard(self,*,own_append=False):
        self._guard();self.db._guard()
        try:document=read_plan_file(self.value['config']['plan_file'])
        except ValueError:raise RemoteTaskError('remote_window_plan_unavailable') from None
        if _digest(document)!=self.value['config']['plan_digest']:
            raise RemoteTaskError('remote_window_plan_changed')
        if 'plan_context' in self.value and self.value['state'] not in ('completed','failed'):
            self._checked_update()
        if list(self.db.identity)!=self.value['database_identity']:
            raise RemoteTaskError('remote_window_database_changed')
        if self.data_version is not None and self.db.db.execute('PRAGMA data_version').fetchone()[0]!=self.data_version:
            raise RemoteTaskError('remote_window_database_changed')
        if not own_append and self.fingerprint is not None and self._fingerprint()!=self.fingerprint:
            raise RemoteTaskError('remote_window_database_changed')

    def _record(self,row):
        try:
            UUID(row[1]);record=validate_record(_json(row[4]))
            if (row[2]!=record['requested_store_id'] or row[2] not in self.value['config']['store_ids']
                    or row[3]!=int(record['ok'])):raise ValueError
            return record
        except (ValueError,TypeError):raise RemoteTaskError('remote_window_result_conflict') from None

    def _read_rows(self):
        return self.db.db.execute('SELECT id,run_id,store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
            'THEN payload_json END FROM remote_samples WHERE id>? ORDER BY id LIMIT ?',
            (MAX_RECORD,self.value['initial_id'],self.value['config']['max_pairs']+2))

    def _verify_prefix(self):
        self._database_guard();count=self.value['successful']+self.value['failed']+self.value.get('scheduled_pauses',0);digest=_EMPTY;attempts=0;pending=[];seen=0
        observed={'successful':0,'failed':0,'scheduled_pauses':0}
        for index,row in enumerate(self._read_rows()):
            seen+=1
            if row[0]!=self.value['initial_id']+index+1:raise RemoteTaskError('remote_window_result_conflict')
            record=self._record(row)
            if index<count:
                digest=_chain(digest,row);attempts+=sum(q['attempted'] for q in record['queries'].values())
                paused=any(q['error_code']=='business_window_closed' for q in record['queries'].values())
                observed['scheduled_pauses' if paused else 'successful' if record['ok'] else 'failed']+=1
            else:pending.append(row)
        if seen not in ((count,count+1) if self.value['pending'] else (count,)):
            raise RemoteTaskError('remote_window_result_conflict')
        if digest!=self.value['records_digest'] or attempts!=self.value['recorded_http_attempts']:
            raise RemoteTaskError('remote_window_database_changed')
        if 'business_hours' in self.value['config'] and any(observed[k]!=self.value[k] for k in observed):
            raise RemoteTaskError('remote_window_result_conflict')
        return pending

    def _tail(self,*,own_append=False):
        self._database_guard(own_append=own_append)
        count=self.value['successful']+self.value['failed']+self.value.get('scheduled_pauses',0);after=self.value['initial_id']+count
        maximum=self.db.db.execute('SELECT COALESCE(MAX(id),0) FROM remote_samples').fetchone()[0]
        if maximum not in ((after,after+1) if self.value['pending'] else (after,)):
            raise RemoteTaskError('remote_window_result_conflict')
        if maximum==after:return []
        row=self.db.db.execute('SELECT id,run_id,store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
            'THEN payload_json END FROM remote_samples WHERE id=?',(MAX_RECORD,maximum)).fetchone()
        self._record(row);return [row]

    def bind(self,db,*,now):
        if self.require_new_database and not db.exclusive_create:
            raise RemoteTaskError('remote_task_new_database_required')
        self.db=db;at=_now(now)
        if str(db.path)!=self.value['config']['db']:raise RemoteTaskError('remote_task_config_conflict')
        if _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_window_clock_rollback')
        if not self.loaded:
            value=deepcopy(self.value);value['database_identity']=list(db.identity)
            value['initial_id']=db.db.execute('SELECT COALESCE(MAX(id),0) FROM remote_samples').fetchone()[0]
            self._commit(value)
        self._remember_database();self._verify_prefix();self._database_guard();uncertain=self.value['uncertain']
        if self.value['pending']:self.reconcile(now=now,interrupted=True)
        if self.loaded and self.value['state']=='ready' and self.value['last_attempt_at'] and self.value['uncertain']==uncertain:
            value=deepcopy(self.value);value['last_gap']={'from':value['last_attempt_at'],'to':at,'reason':'restart'}
            value['updated_at']=at;self._commit(value)
        self.finish_if_due(now=now)

    def finish_if_due(self,*,now,monotonic_expired=False):
        at=_now(now)
        if _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_window_clock_rollback')
        if self.value['state']!='ready':return self.value['state'] in ('completed','failed')
        reason='budget' if self.value['cursor']==self.value['config']['max_pairs'] else 'deadline' if _at(at)>=_at(self.value['deadline_at']) else 'monotonic_duration' if monotonic_expired else None
        if reason:
            self._tail();value=deepcopy(self.value);value.update(state='completed',end_reason=reason,updated_at=at);self._commit(value)
        return reason is not None

    def begin(self,store_id,*,now):
        at=_now(now);self._tail()
        if self.finish_if_due(now=now) or self.value['state']!='ready' or store_id not in self.value['config']['store_ids']:
            raise RemoteTaskError('remote_window_start_not_allowed')
        value=deepcopy(self.value);value['pending']={'cursor':value['cursor'],'store_id':store_id,
            'run_id':self.db.run_id,'after_id':value['initial_id']+value['successful']+value['failed']+value.get('scheduled_pauses',0),'started_at':at}
        value['starts'][store_id]=at;value.update(state='running',last_attempt_at=at,updated_at=at);self._commit(value)

    def reconcile(self,*,now,interrupted=False):
        at=_now(now);pending=self.value['pending']
        if pending is None or _at(at)<_at(self.value['updated_at']):raise RemoteTaskError('remote_window_result_conflict')
        rows=self._tail(own_append=not interrupted);value=deepcopy(self.value)
        if not rows and not interrupted:raise RemoteTaskError('remote_window_result_conflict')
        if rows:
            row=rows[0];record=self._record(row)
            if (row[1]!=pending['run_id'] or row[2]!=pending['store_id']
                    or _at(record['queries']['groupqueues']['started_at'])<_at(pending['started_at'])):
                raise RemoteTaskError('remote_window_result_conflict')
            paused=('business_hours' in value['config'] and any(
                q['error_code']=='business_window_closed' for q in record['queries'].values()))
            value['scheduled_pauses' if paused else 'successful' if record['ok'] else 'failed']+=1
            value['recorded_http_attempts']+=sum(q['attempted'] for q in record['queries'].values())
            value['records_digest']=_chain(value['records_digest'],row)
        else:
            value['uncertain']+=1;value['last_gap']={'from':pending['started_at'],'to':at,'reason':'uncertain_attempt'}
        value['cursor']+=1;value.update(pending=None,updated_at=at,state='failed' if value['failed'] else 'ready')
        self._commit(value);self._remember_database();self.finish_if_due(now=now)


class PersistentWindowSchedule:
    def __init__(self,task,*,wall,monotonic,previous_starts=None,previous_monotonic=None,resume_wait=False):
        self.task=task;self.last_wall=None;self.last_mono=None;self.starts_mono={}
        from .businesshours import BusinessHours
        self.hours=BusinessHours(task.value['config']['business_hours']) if 'business_hours' in task.value['config'] else None
        at,mono=self.clock(wall,monotonic)
        self.previous_starts=deepcopy(previous_starts or {})
        carried=deepcopy(previous_monotonic or {})
        stores=set(task.value['config']['store_ids'])
        if (type(resume_wait) is not bool or set(self.previous_starts)-stores or set(carried)-stores
                or any(_at(t)>at for t in self.previous_starts.values())
                or any(type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<=mono for t in carried.values())):
            raise RemoteTaskError('remote_window_invalid_carryover')
        self.starts_mono=carried
        self.resume_mono=mono if task.loaded or resume_wait else None
        self.deadline_mono=mono+max(0,(_at(task.value['deadline_at'])-at).total_seconds())

    def clock(self,wall,mono):
        at=_at(_now(wall))
        if (type(mono) not in (int,float) or not math.isfinite(mono) or not 0<=mono<=1e12
                or self.last_mono is not None and mono<self.last_mono
                or self.last_wall is not None and at<self.last_wall):raise RemoteTaskError('remote_window_clock_rollback')
        self.last_wall,self.last_mono=at,mono;return at,mono

    def decision(self,*,wall,monotonic):
        at,mono=self.clock(wall,monotonic);value=self.task.value;c=value['config']
        if self.task.finish_if_due(now=wall,monotonic_expired=mono>=self.deadline_mono):
            return {'done':True,'due_stores':[],'wake_monotonic':None}
        self.task.refresh_plans(now=wall);value=self.task.value
        doc=deepcopy(self.task.document);plan_stores={p['store_id'] for p in doc['plans']}
        doc['last_poll_started_at']={s:t for s,t in value['starts'].items() if s in plan_stores}
        policy=shared_polling_policy(doc,as_of=_now(wall),base_interval=c['base_interval'])
        policies={p['store_id']:p for p in policy['stores']};due=[];wakes=[]
        for store in c['store_ids']:
            if self.hours is not None:
                opening=self.hours.decision(store,at)
                if not opening['is_open_window']:
                    wakes.append(mono+max(0,(_at(opening['next_open_at'])-at).total_seconds())
                        if opening['next_open_at'] else self.deadline_mono)
                    continue
            item=policies.get(store);interval=(item['requested_interval_seconds'] if item else None) or c['base_interval']
            previous=value['starts'].get(store,self.previous_starts.get(store))
            target=mono+max(0,(_at(previous)+timedelta(seconds=interval)-at).total_seconds()) if previous else mono
            if store in self.starts_mono:target=max(target,self.starts_mono[store]+interval)
            if self.resume_mono is not None:target=max(target,self.resume_mono+interval)
            if target<=mono:due.append(store)
            wake=target
            if item and item['next_policy_transition_at']:
                wake=min(wake,mono+(_at(item['next_policy_transition_at'])-at).total_seconds())
            wakes.append(wake)
        return {'done':False,'due_stores':due,'wake_monotonic':min(self.deadline_mono,*wakes)}

    def mark(self,store,*,wall,monotonic):
        if store not in self.decision(wall=wall,monotonic=monotonic)['due_stores']:
            raise RemoteTaskError('remote_window_start_not_due')
        self.task.begin(store,now=wall);self.starts_mono[store]=monotonic


def collect_remote_window(task,client,*,wall_clock,monotonic_clock,sleep,emit,should_stop=lambda:False,schedule=None,
                          before_attempt=lambda:None):
    if schedule is None:schedule=PersistentWindowSchedule(task,wall=wall_clock(),monotonic=monotonic_clock())
    if schedule.task is not task:raise RemoteTaskError('remote_window_schedule_conflict')
    if schedule.hours is not None and client is not None:
        from .remote import RemoteClient
        if not isinstance(client,RemoteClient) or client.request_guard is not None:
            raise RemoteTaskError('business_hours_transport_guard_required')
        def guard(store):
            at,mono=schedule.clock(wall_clock(),monotonic_clock())
            return (not should_stop() and at<_at(task.value['deadline_at'])
                and mono<schedule.deadline_mono and schedule.hours.decision(store,at)['is_open_window'])
        client.request_guard=guard
    try:
        while not should_stop():
            decision=schedule.decision(wall=wall_clock(),monotonic=monotonic_clock())
            if decision['done']:break
            if not decision['due_stores']:
                delay=max(0,decision['wake_monotonic']-monotonic_clock())
                sleep(min(1,delay) if 'plan_context' in task.value or schedule.hours is not None else delay);continue
            store=decision['due_stores'][0]
            before_attempt()
            schedule.mark(store,wall=wall_clock(),monotonic=monotonic_clock())
            record=client.snapshot(store);identifier=task.db.append(record);task.reconcile(now=wall_clock())
            emit({'id':identifier,'pair_slot':task.value['cursor'],'record':record})
            if task.value['state']=='failed':break
    finally:
        if schedule.hours is not None and client is not None:
            client.request_guard=None
    result=window_status(task.value)
    result['ok']=task.value['state']=='completed' and not task.value['failed'] and not task.value['uncertain']
    result['stopped_by_request']=should_stop();emit({'remote_window_summary':result});return result


class RemoteWindowService(RemoteQueueService):
    def __init__(self,*,plan_file,base_interval=300,duration_seconds=86400,max_pairs=8640,resume_if_present=False,plan_updates_file=None,business_hours=None,**kwargs):
        if type(resume_if_present) is not bool or resume_if_present and kwargs.get('resume',False):
            raise RemoteTaskError('remote_task_invalid_resume_mode')
        self.resume_if_present=resume_if_present
        super().__init__(interval=base_interval,samples=1,**kwargs)
        self.config=window_config(kwargs['db'],plan_file,kwargs['store_ids'],base_interval,duration_seconds,max_pairs,
            now=self.wall_clock(),plan_updates_file=plan_updates_file,business_hours=business_hours)
        if business_hours is not None:
            from .dailyview import DailyView
            self.daily_view=DailyView(kwargs['store_ids'],hours=business_hours,base_interval=base_interval)
    def _make_task(self):
        return RemoteWindowTask(self.task_file,config=self.config,resume=self.resume,now=self.wall_clock(),
            resume_if_present=self.resume_if_present)
    @staticmethod
    def _task_status(task):return window_status(task.value)
    def _collect(self,task,client,sleep,emit):
        return collect_remote_window(task,client,wall_clock=self.wall_clock,monotonic_clock=self.monotonic_clock,
            sleep=sleep,emit=emit,should_stop=self.stop_event.is_set)
