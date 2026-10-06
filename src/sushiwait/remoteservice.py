"""Read-only ASGI views of one bounded, persisted anonymous collection task.

The worker owns SQLite; requests only read validated, committed copies. This
does not create an unlimited daemon, renew credentials, or estimate call times.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time

from .remote import MAX_RECORD, SOURCE, RemoteClient, RemoteStore, _id, _time, validate_record
from .client import _unique_json_object, _reject_json_constant
from .remotetasks import RemoteTask, RemoteTaskError, collect_remote_task, public_status, task_config


class RemoteServiceError(ValueError):
    pass


def _utc():
    return datetime.now(timezone.utc)


class LiveRemoteView:
    """Small thread-safe projection; it never opens a database or a socket."""
    def __init__(self, store_ids, *, stale_after_seconds):
        if (type(store_ids) is not list or not 1 <= len(store_ids) <= 3
                or len(set(store_ids)) != len(store_ids)
                or type(stale_after_seconds) is not int or not 30 <= stale_after_seconds <= 7200):
            raise RemoteServiceError('remote_service_invalid_scope')
        for store in store_ids:_id(store)
        self.stores = tuple(store_ids)
        self.stale_after = stale_after_seconds
        self.lock = threading.Lock()
        self.records = {store:{'latest':{},'successful':{},'origins':{}} for store in store_ids}

    def publish(self, record, *, saved_history=False):
        safe = validate_record(record)
        store = safe['requested_store_id']
        if store not in self.records:
            raise RemoteServiceError('remote_service_store_scope')
        with self.lock:
            current = self.records[store]
            for endpoint, result in safe['queries'].items():
                before = current['latest'].get(endpoint)
                if (before and result['started_at'] is not None and before['started_at'] is not None
                        and _time(result['started_at']) < _time(before['started_at'])):
                    raise RemoteServiceError('remote_service_record_time_order')
            current['latest'] = deepcopy(safe['queries'])
            for endpoint, result in safe['queries'].items():
                if result['ok']:
                    current['successful'][endpoint] = deepcopy(result)
                    current['origins'][endpoint] = 'saved_history' if saved_history else 'worker_commit'

    def snapshot(self, store_id, *, now, service_state, worker_alive):
        if store_id not in self.records:
            raise RemoteServiceError('remote_service_store_scope')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RemoteServiceError('remote_service_clock_invalid')
        with self.lock:current = deepcopy(self.records[store_id])
        fields = {}
        for endpoint in ('groupqueues','storequeuecount'):
            latest = current['latest'].get(endpoint)
            success = current['successful'].get(endpoint)
            age = None if success is None else (now - _time(success['received_at'])).total_seconds()
            reason = None
            if success is None:state, reason = 'unavailable','no_successful_response'
            elif age < 0:state, reason = 'clock_invalid','response_time_in_future'
            elif latest is not None and not latest['ok']:state, reason = 'last_known_only','latest_query_failed_or_skipped'
            elif current['origins'][endpoint] == 'saved_history':state, reason = 'saved_history','loaded_from_database'
            elif not worker_alive or service_state != 'running':state, reason = 'last_known_only','collector_not_running'
            elif age > self.stale_after:state, reason = 'stale_response','response_age_exceeded'
            else:state = 'recent_response'
            fields[endpoint] = {'state':state,'reason':reason,
                'payload':deepcopy(success['payload']) if success else None,
                'received_at':success['received_at'] if success else None,
                'response_age_seconds':round(age,3) if age is not None and age >= 0 else None,
                'latest_attempt':{k:latest[k] for k in ('attempted','ok','error_code','http_status','started_at','received_at')}
                    if latest else None}
        return {'view_schema_version':1,'source':SOURCE,'requested_store_id':store_id,
            'service_state':service_state,'worker_alive':worker_alive,'fields':fields,
            'age_semantics':'local_successful_response_age','stale_after_seconds':self.stale_after,
            'source_freshness':'unknown','source_update_time_verified':False,
            'response_store_identity_verified':False,'atomic_snapshot':False,
            'eta_available':False,'verified_training_labels':0,'network_performed_by_read':False}


class RemoteQueueService:
    """A single worker, existing task budget, explicit recovery and cancellation."""
    def __init__(self, *, db, task_file, store_ids, interval=60, samples=120, resume=False,
                 stale_after_seconds=None, client_factory=RemoteClient, wall_clock=_utc,
                 monotonic_clock=time.monotonic, wait=None):
        if (type(interval) is not int or not 30 <= interval <= 3600
                or type(samples) is not int or not 1 <= samples <= 120 or type(resume) is not bool):
            raise RemoteServiceError('remote_service_invalid_bounds')
        self.view = LiveRemoteView(store_ids,stale_after_seconds=max(120,2*interval) if stale_after_seconds is None else stale_after_seconds)
        self.config = task_config(db,store_ids,interval,samples)
        self.task_file,self.resume = task_file,resume
        self.client_factory,self.wall_clock,self.monotonic_clock = client_factory,wall_clock,monotonic_clock
        self.wait = wait
        self.stop_event,self.ready,self.activated = threading.Event(),threading.Event(),threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.state,self.error_code,self.task_status = 'not_started',None,None

    def _set(self, state, *, error=None, task=None):
        with self.lock:
            self.state,self.error_code = state,error
            if task is not None:self.task_status = deepcopy(task)

    def _restore(self, database):
        for store in self.view.stores:
            database._guard()
            rows = database.db.execute('SELECT store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
                'THEN payload_json END FROM remote_samples WHERE store_id=? ORDER BY id DESC LIMIT 1000',
                (MAX_RECORD,store)).fetchall()
            for identifier,ok,body in reversed(rows):
                if body is None:raise RemoteServiceError('remote_service_record_invalid')
                record = validate_record(json.loads(body,object_pairs_hook=_unique_json_object,parse_constant=_reject_json_constant))
                if identifier != record['requested_store_id'] or ok != int(record['ok']):
                    raise RemoteServiceError('remote_service_record_invalid')
                self.view.publish(record,saved_history=True)

    def _make_task(self):
        return RemoteTask(self.task_file,config=self.config,resume=self.resume,now=self.wall_clock())

    @staticmethod
    def _task_status(task):return public_status(task.value)

    def _collect(self,task,client,sleep,emit):
        return collect_remote_task(task,client,wall_clock=self.wall_clock,
            monotonic_clock=self.monotonic_clock,sleep=sleep,emit=emit,should_stop=self.stop_event.is_set)

    def _run(self):
        try:
            with self._make_task() as task:
                task.prepare_database()
                with RemoteStore(self.config['db'],exclusive_create=task.require_new_database) as database:
                    task.bind(database,now=self.wall_clock())
                    self._restore(database)
                    self._set('ready',task=self._task_status(task));self.ready.set()
                    self.activated.wait()
                    if self.stop_event.is_set():
                        self._set('stopped',task=self._task_status(task));return
                    if task.value['state'] in ('completed','failed'):
                        state = task.value['state']
                        self._set(state,error='remote_service_query_failed' if state=='failed' else None,
                            task=self._task_status(task));return
                    self._set('running',task=self._task_status(task))
                    def emit(event):
                        if 'record' in event:self.view.publish(event['record'])
                        self._set('running',task=self._task_status(task))
                    def sleep(seconds):
                        if self.wait is None:self.stop_event.wait(seconds)
                        else:self.wait(seconds,self.stop_event)
                    result = self._collect(task,self.client_factory(),sleep,emit)
                    state = 'failed' if task.value['state']=='failed' else 'completed' if task.value['state']=='completed' else 'stopped'
                    self._set(state,error='remote_service_query_failed' if state=='failed' else None,task=result)
        except BaseException as error:
            code = str(error) if isinstance(error,RemoteTaskError) else 'remote_service_storage_or_input_error'
            self._set('failed',error=code)
        finally:
            self.ready.set()

    def start(self):
        with self.lock:
            if self.thread is not None:raise RemoteServiceError('remote_service_already_started')
            self.state = 'starting'
            self.thread = threading.Thread(target=self._run,name='sushiwait-remote-worker',daemon=False)
            self.thread.start()
        if not self.ready.wait(5):
            self.stop_event.set();self.activated.set()
            raise RemoteServiceError('remote_service_startup_unconfirmed')
        with self.lock:state,error = self.state,self.error_code
        if state == 'failed':raise RemoteServiceError(error)
        self.activated.set()

    def shutdown(self):
        self.stop_event.set();self.activated.set()
        if self.thread is not None:self.thread.join(35)
        if self.thread is not None and self.thread.is_alive():
            raise RemoteServiceError('remote_service_shutdown_unconfirmed')

    def status(self):
        with self.lock:state,error,task = self.state,self.error_code,deepcopy(self.task_status)
        alive = self.thread is not None and self.thread.is_alive()
        if state == 'running' and not alive:state,error = 'failed','remote_service_worker_unavailable'
        return {'service_schema_version':1,'source':SOURCE,'service_state':state,
            'worker_alive':alive,'error_code':error,'store_ids':list(self.view.stores),
            'task':task,'bounded_task':True,'automatic_task_restart':False,
            'task_status_semantics':'last_published_checkpoint','health_semantics':'collector_worker_liveness',
            'network_performed_by_read':False,'source_freshness':'unknown','eta_available':False,
            'verified_training_labels':0}

    def store_view(self, store_id):
        state = self.status()
        return self.view.snapshot(store_id,now=self.wall_clock(),service_state=state['service_state'],
            worker_alive=state['worker_alive'])


class RemoteASGI:
    """Fixed GET routes, no user-controlled upstream targets or business writes."""
    def __init__(self, service):self.service = service

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                if message['type'] == 'lifespan.startup':
                    try:await asyncio.to_thread(self.service.start)
                    except RemoteServiceError as error:
                        if str(error) != 'remote_service_already_started':
                            try:await asyncio.to_thread(self.service.shutdown)
                            except RemoteServiceError:pass
                        await send({'type':'lifespan.startup.failed','message':'remote_service_startup_failed'});return
                    await send({'type':'lifespan.startup.complete'})
                elif message['type'] == 'lifespan.shutdown':
                    try:await asyncio.to_thread(self.service.shutdown)
                    except RemoteServiceError:
                        await send({'type':'lifespan.shutdown.failed','message':'remote_service_shutdown_unconfirmed'});return
                    await send({'type':'lifespan.shutdown.complete'});return
            return
        if scope['type'] != 'http':raise RemoteServiceError('remote_service_protocol_unsupported')
        status,payload = 200,None
        if scope.get('method') != 'GET':status,payload = 405,{'error_code':'method_not_allowed'}
        elif scope.get('query_string',b''):status,payload = 400,{'error_code':'query_parameters_not_supported'}
        else:
            request = await receive()
            if request['type'] == 'http.disconnect':return
            if request.get('body') or request.get('more_body'):
                status,payload = 400,{'error_code':'request_body_not_supported'}
            else:
                path = scope.get('path','')
                if path in ('/health','/api/v1/status'):
                    payload = self.service.status()
                    if path=='/health' and (payload['service_state']!='running' or not payload['worker_alive']):status = 503
                elif path.startswith('/api/v1/stores/') and path.endswith('/queue'):
                    store = path[len('/api/v1/stores/'):-len('/queue')]
                    if store not in self.service.view.stores:status,payload = 404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload = self.service.store_view(store)
                        except RemoteServiceError:status,payload = 503,{'error_code':'remote_service_view_unavailable'}
                else:status,payload = 404,{'error_code':'route_not_found'}
        body = json.dumps(payload,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()
        headers = [(b'content-type',b'application/json; charset=utf-8'),(b'content-length',str(len(body)).encode()),
            (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff')]
        if status==405:headers.append((b'allow',b'GET'))
        await send({'type':'http.response.start','status':status,'headers':headers})
        await send({'type':'http.response.body','body':body})


def serve_local(service, *, port=8765, listen_host='127.0.0.1'):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RemoteServiceError('remote_service_invalid_port')
    if listen_host not in ('127.0.0.1','0.0.0.0'):
        raise RemoteServiceError('remote_service_invalid_listen_host')
    try:import uvicorn
    except ImportError:raise RemoteServiceError('remote_service_install_server_extra') from None
    try:
        uvicorn.run(RemoteASGI(service),host=listen_host,port=port,workers=1,lifespan='on',
            loop='asyncio',http='h11',ws='none',proxy_headers=False,access_log=False,
            timeout_graceful_shutdown=40,limit_concurrency=32,timeout_keep_alive=5)
    finally:service.shutdown()
