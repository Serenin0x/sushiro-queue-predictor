"""Read-only ASGI views of one bounded, persisted anonymous collection task.

The worker owns SQLite; requests only read validated, committed copies. This
does not create an unlimited daemon, renew credentials, or estimate call times.
"""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from importlib.resources import files
import json
import re
import threading
import time

from .remote import MAX_RECORD, SOURCE, QUEUE_NAMES, RemoteClient, RemoteStore, _id, _time, validate_record
from .client import _unique_json_object, _reject_json_constant
from .remotetasks import RemoteTask, RemoteTaskError, collect_remote_task, public_status, task_config


class RemoteServiceError(ValueError):
    pass


def _utc():
    return datetime.now(timezone.utc)


MONITOR_POINTS = 360
_MONITOR_FILES = {'/monitor': ('monitor.html', b'text/html; charset=utf-8'),
    '/monitor.js': ('monitor.js', b'text/javascript; charset=utf-8'),
    '/monitor.css': ('monitor.css', b'text/css; charset=utf-8')}
_MONITOR_FILES.update({'/statistics':('statistics.html',b'text/html; charset=utf-8'),
    '/statistics.js':('statistics.js',b'text/javascript; charset=utf-8'),
    '/reference-model.js':('reference-model.js',b'text/javascript; charset=utf-8'),
    '/statistics.css':('statistics.css',b'text/css; charset=utf-8')})
_MONITOR_CSP = (b"default-src 'none'; script-src 'self'; style-src 'self'; "
    b"connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _monitor_point(safe, previous, *, saved_history, max_gap):
    """Summarize a committed pair without reusing old success as new data."""
    queues, count = safe['queries']['groupqueues'], safe['queries']['storequeuecount']
    prior = previous.get('groupqueues')
    comparison = {'state': 'insufficient', 'interval_seconds': None, 'removed_labels': None}
    if queues['ok'] and prior is not None and prior['ok']:
        delta = (_time(queues['received_at']) - _time(prior['received_at'])).total_seconds()
        comparison['interval_seconds'] = round(delta, 3)
        if delta <= 0:
            comparison['state'] = 'time_order_or_duplicate'
        elif delta > max_gap:
            comparison['state'] = 'gap'
        else:
            comparison['state'] = 'comparable_display_sets'
            comparison['removed_labels'] = {name: len(set(prior['payload']['queues'][name])
                - set(queues['payload']['queues'][name])) for name in QUEUE_NAMES}
    return {'origin': 'saved_history' if saved_history else 'worker_commit',
        'pair_ok': safe['ok'],
        'queries': {name: {key: result[key] for key in
            ('attempted', 'ok', 'error_code', 'http_status', 'started_at', 'received_at')}
            for name, result in safe['queries'].items()},
        'reported_count_raw': count['payload']['raw_count'] if count['ok'] else None,
        'count_unit': 'unknown',
        'display_sizes': {name: len(queues['payload']['queues'][name]) for name in QUEUE_NAMES}
            if queues['ok'] else None,
        'display_comparison': comparison}


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
        self.lock = threading.RLock()
        self.records = {store:{'latest':{},'successful':{},'origins':{}} for store in store_ids}
        self.history = {store: deque(maxlen=MONITOR_POINTS) for store in store_ids}
        self.evicted = {store: 0 for store in store_ids}

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
            if current['latest'] != safe['queries']:
                point = _monitor_point(safe, current['latest'], saved_history=saved_history,
                                       max_gap=self.stale_after)
                if len(self.history[store]) == MONITOR_POINTS:
                    self.evicted[store] += 1
                self.history[store].append(point)
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


    def monitor_history(self, store_id, *, now, service_state, worker_alive):
        if store_id not in self.records:
            raise RemoteServiceError('remote_service_store_scope')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RemoteServiceError('remote_service_clock_invalid')
        with self.lock:
            points = deepcopy(list(self.history[store_id]))
            evicted = self.evicted[store_id]
        return {'monitor_schema_version': 1, 'source': SOURCE, 'requested_store_id': store_id,
            'generated_at': now.isoformat(), 'service_state': service_state, 'worker_alive': worker_alive,
            'points': points, 'retained_points': len(points), 'max_points': MONITOR_POINTS,
            'evicted_points_this_process': evicted, 'complete_history': False,
            'history_semantics': 'bounded_committed_projection', 'comparison_max_gap_seconds': self.stale_after,
            'count_unit': 'unknown', 'display_turnover_is_no_show_rate': False,
            'source_freshness': 'unknown', 'response_store_identity_verified': False,
            'eta_available': False, 'network_performed_by_read': False}

    def tracking_projection(self, store_id, *, now, service_state, worker_alive):
        """Copy display and history under one local projection lock.

        This prevents mixed local commits; the two upstream GETs remain
        non-atomic, with unknown source freshness and store identity.
        """
        with self.lock:
            options = dict(now=now, service_state=service_state, worker_alive=worker_alive)
            return {'tracking_projection_schema_version': 1, 'source': SOURCE,
                'requested_store_id': store_id, 'generated_at': now.isoformat(),
                'view': self.snapshot(store_id, **options),
                'history': self.monitor_history(store_id, **options),
                'local_projection_atomic': True, 'upstream_snapshot_atomic': False,
                'network_performed_by_read': False, 'source_freshness': 'unknown',
                'eta_available': False, 'verified_training_labels': 0}


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
        self.daily_view = None
        self.state,self.error_code,self.task_status = 'not_started',None,None

    def _set(self, state, *, error=None, task=None):
        with self.lock:
            self.state,self.error_code = state,error
            if task is not None:self.task_status = deepcopy(task)

    def _restore(self, database):
        if self.daily_view is not None:self.daily_view.restore(database)
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
                        if 'record' in event:
                            if self.daily_view is not None:self.daily_view.committed(event)
                            self.view.publish(event['record'])
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

    def daily_index(self,store_id):
        if self.daily_view is None:raise RemoteServiceError('daily_view_not_enabled')
        return self.daily_view.index(store_id,now=self.wall_clock())

    def daily_detail(self,store_id,day):
        if self.daily_view is None:raise RemoteServiceError('daily_view_not_enabled')
        try:return self.daily_view.detail(store_id,day,now=self.wall_clock())
        except ValueError:raise RemoteServiceError('daily_view_invalid_date_or_scope') from None

    def monitor_history(self, store_id):
        state = self.status()
        return self.view.monitor_history(store_id, now=self.wall_clock(),
            service_state=state['service_state'], worker_alive=state['worker_alive'])

    def fusion_context(self, store_id):
        from .fusion import context_from_history, FusionError
        history = self.monitor_history(store_id)
        try:
            contexts = {queue: context_from_history(history, queue_type=queue, now=_time(history['generated_at']))
                        for queue in ('ordinary', 'reservation')}
        except FusionError:
            raise RemoteServiceError('remote_service_fusion_context_invalid') from None
        return {'fusion_context_schema_version': 1, 'source': SOURCE, 'requested_store_id': store_id,
                'contexts': contexts, 'network_performed_by_read': False,
                'verified_training_labels': 0, 'eta_available': False,
                'revision_scope': 'writer_process_projection', 'display_turnover_is_no_show_rate': False}

    def tracking_projection(self, store_id):
        state = self.status()
        return self.view.tracking_projection(store_id, now=self.wall_clock(),
            service_state=state['service_state'], worker_alive=state['worker_alive'])


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
        status,payload,asset = 200,None,None
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
                elif path in _MONITOR_FILES:
                    name, content_type = _MONITOR_FILES[path]
                    asset = files('sushiwait').joinpath('web', name).read_bytes(), content_type
                elif path=='/api/v1/days':
                    if self.service.daily_view is None:status,payload=503,{'error_code':'daily_view_not_enabled'}
                    else:payload=self.service.daily_view.batch_index(now=self.service.wall_clock())
                elif re.fullmatch(r'/api/v1/stores/[1-9][0-9]{0,18}/days(?:/[0-9]{4}-[0-9]{2}-[0-9]{2})?',path):
                    pieces=path.split('/');store=pieces[4]
                    if store not in self.service.view.stores:status,payload=404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload=self.service.daily_index(store) if len(pieces)==6 else self.service.daily_detail(store,pieces[6])
                        except (RemoteServiceError,RemoteTaskError):status,payload=503,{'error_code':'daily_view_unavailable'}
                elif path.startswith('/api/v1/stores/') and path.endswith('/history'):
                    store = path[len('/api/v1/stores/'):-len('/history')]
                    if store not in self.service.view.stores:status,payload = 404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload = self.service.monitor_history(store)
                        except RemoteServiceError:status,payload = 503,{'error_code':'remote_service_view_unavailable'}
                elif path.startswith('/api/v1/stores/') and path.endswith('/fusion-context'):
                    store = path[len('/api/v1/stores/'):-len('/fusion-context')]
                    if store not in self.service.view.stores:status,payload = 404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload = self.service.fusion_context(store)
                        except RemoteServiceError:status,payload = 503,{'error_code':'remote_service_view_unavailable'}
                elif path.startswith('/api/v1/stores/') and path.endswith('/tracking-projection'):
                    store = path[len('/api/v1/stores/'):-len('/tracking-projection')]
                    if store not in self.service.view.stores:status,payload = 404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload = self.service.tracking_projection(store)
                        except RemoteServiceError:status,payload = 503,{'error_code':'remote_service_view_unavailable'}
                elif path.startswith('/api/v1/stores/') and path.endswith('/queue'):
                    store = path[len('/api/v1/stores/'):-len('/queue')]
                    if store not in self.service.view.stores:status,payload = 404,{'error_code':'store_not_in_scope'}
                    else:
                        try:payload = self.service.store_view(store)
                        except RemoteServiceError:status,payload = 503,{'error_code':'remote_service_view_unavailable'}
                else:status,payload = 404,{'error_code':'route_not_found'}
        body, content_type = asset if asset is not None else (
            json.dumps(payload,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode(),
            b'application/json; charset=utf-8')
        headers = [(b'content-type',content_type),(b'content-length',str(len(body)).encode()),
            (b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff')]
        if asset is not None:headers.append((b'content-security-policy',_MONITOR_CSP))
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
