"""Public saved statistics and optional owner-scoped ticket research.

Private POST forms require the exact HTTPS origin. No query credentials,
collector control, account sessions or filesystem downloads are exposed.
"""
import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import re
import time
import json
import math
from urllib.parse import parse_qsl

from sushiwait.monitorhub import MonitorHub, read_config


class JoinedOfficialViews:
    """Join disjoint saved archives without widening any packet's scope."""
    def __init__(self, primary, extra):
        if set(primary.names) & set(extra.names) or len(primary.names) + len(extra.names) > 5:
            raise ValueError('official_view_join_scope_invalid')
        self.views = (primary, extra)
        self.names = {**primary.names, **extra.names}

    def allowed(self, path):
        return any(view.allowed(path) for view in self.views)

    def dispatch(self, path):
        if not re.fullmatch(r'/api/v1/official/months/20[0-9]{2}-(?:0[1-9]|1[0-2])', path):
            view = next((v for v in self.views if v.allowed(path)), None)
            return view.dispatch(path) if view else (404, b'Unknown saved route', 'text/plain')
        rows = []
        for view in self.views:
            status, body, mime = view.dispatch(path)
            if status != 200:
                return status, body, mime
            rows.append(json.loads(body))
        a, b = rows
        for key in ('source', 'api_profile', 'data_origin', 'month', 'timezone', 'daily_schema_version'):
            if a[key] != b[key]:
                return 503, b'Archive scope mismatch', 'text/plain'
        result = {**a, 'store_names': self.names,
                  'configured_store_ids': list(self.names),
                  'unavailable_store_ids': list(dict.fromkeys(a['unavailable_store_ids'] + b['unavailable_store_ids'])),
                  'days': {day: {**a['days'].get(day, {}), **b['days'].get(day, {})}
                           for day in sorted(set(a['days']) | set(b['days']))}}
        return 200, json.dumps(result, ensure_ascii=False, allow_nan=False).encode(), mime


class StatisticsGateway:
    def __init__(self, hub, *, official_view=None, calendar_source='crm_remote_v1_1',
                 refresh_seconds=30, refresh_policy_file=None, model_advice_file=None,
                 research_file=None, public_origin='https://124.222.57.236', ocr_enabled=False,
                 collection_status_file=None):
        if (calendar_source not in {'crm_remote_v1_1', 'sapi_miniapp_gateway'}
                or calendar_source == 'sapi_miniapp_gateway' and official_view is None):
            raise ValueError('calendar_source_requires_configured_view')
        self.hub = hub
        self.official_view = official_view
        self.model_advice_file = model_advice_file
        self.research_file = research_file
        self.public_origin = public_origin
        self.research_clients = OrderedDict()
        self.ocr_enabled = ocr_enabled
        self.collection_status_file = collection_status_file
        self.ocr_clients = OrderedDict()
        self.ocr_lock = asyncio.Lock()
        self.research_lock = asyncio.Lock()
        self.analysis_cache = OrderedDict()
        self.analysis_lock = asyncio.Lock()
        self.calendar_source = calendar_source
        if type(refresh_seconds) is not int or not 30 <= refresh_seconds <= 3600:
            raise ValueError('statistics_refresh_interval_invalid')
        self.refresh_seconds = refresh_seconds
        self.refresh_policy_file = refresh_policy_file
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.deadline = datetime.fromisoformat(hub.config['deadline_at'].replace('Z', '+00:00')) if hub.config['schema_version']==1 else None

    def allowed(self, path):
        if path == '/api/v1/collection/health':
            return self.collection_status_file is not None
        if path == '/wait-reference.json':
            return self.research_file is not None
        if path in {'/research-ui.js','/screenshot-ui.js','/analytics-ui.js','/analytics.css','/analytics-advice.json'}:
            return True
        if path == '/model-advice.json':
            return True
        if path in {'/statistics-history', '/api/v1/statistics/settings'}:
            return True
        if path.startswith('/api/v1/official/'):
            return bool(self.official_view is not None and self.official_view.allowed(path))
        if path in {'/statistics', '/statistics.js', '/statistics.css', '/reference-model.js', '/reference-alerts.js', '/experience-model.js', '/experience-ui.js', '/statistics-legacy', '/calendar.css', '/calendar.mjs', '/calendar-data.mjs', '/calendar-directory.mjs', '/calendar-icons.mjs', '/statistics-client.mjs', '/saved-statistics.mjs', '/engineering-logo.svg','/brand-wordmark.svg', '/api/v1/days'}:
            return True
        if re.fullmatch(r'/api/v1/months/20[0-9]{2}-(?:0[1-9]|1[0-2])',path):
            return True
        match = re.fullmatch(r'/api/v1/stores/([1-9][0-9]{0,9})/days/([0-9]{4}-[0-9]{2}-[0-9]{2})', path)
        return bool(match and match[1] in self.hub.names)

    def polling_settings(self, *, now=None):
        if self.refresh_policy_file is None:
            return {'schema_version':1, 'poll_seconds':self.refresh_seconds}
        from sushiwait.credentials import _read_private_file
        from sushiwait.storage import _unique_object
        value = json.loads(_read_private_file(self.refresh_policy_file), object_pairs_hook=_unique_object)
        keys = {'schema_version', 'interval_seconds', 'ordinary_query_limit'}
        smooth = type(value) is dict and 'cadence' in value
        if (type(value) is not dict or set(value) != (keys | {'cadence'} if smooth else keys)
                or type(value['schema_version']) is not int or value['schema_version'] != (2 if smooth else 1)
                or type(value['interval_seconds']) is not int or not 60 <= value['interval_seconds'] <= 3600
                or type(value['ordinary_query_limit']) is not int
                or not (value['ordinary_query_limit'] in {396,1008} if smooth
                        else 1 <= value['ordinary_query_limit'] <= 396)
                or smooth and (value['interval_seconds'] != 600
                    or type(value['cadence']) is not str or value['cadence'] != 'meal-peaks-v1')):
            raise ValueError('statistics_refresh_policy_invalid')
        if smooth:
            from sushiwait.cadence import period_after, description
            now = datetime.now(timezone.utc) if now is None else now
            return {'schema_version':1, 'poll_seconds':math.ceil(period_after(now,value['cadence'])),
                    'cadence_policy':description(value['cadence'])}
        return {'schema_version':1, 'poll_seconds':value['interval_seconds']}

    def polling_seconds(self):
        return self.polling_settings()['poll_seconds']

    def collection_health(self):
        from sushiwait.credentials import _read_private_file
        value = json.loads(_read_private_file(self.collection_status_file))
        state = value['state']
        if state not in {'starting', 'running', 'closed', 'completed', 'needs_attention'}:
            raise ValueError('invalid_collection_health')
        at = datetime.fromisoformat(value['at'])
        if at.utcoffset() is None:
            raise ValueError('invalid_collection_health')
        age = max(0, (datetime.now(timezone.utc)-at).total_seconds())
        result = {'schema_version': 1, 'state': state if age<=120 else 'needs_attention',
                  'updated_at': at.isoformat(), 'heartbeat_stale': age>120}
        for key in ('opened_at', 'closes_at', 'next_open_at', 'last_progress_at'):
            if value.get(key) is not None:
                instant = datetime.fromisoformat(value[key])
                if instant.utcoffset() is None:
                    raise ValueError('invalid_collection_health')
                result[key] = instant.isoformat()
        for key in ('successful_slots', 'failed_slots'):
            if key in value:
                if type(value[key]) is not int or not 0<=value[key]<=10000:
                    raise ValueError('invalid_collection_health')
                result[key] = value[key]
        # Explicit projection: never expose paths, supplier errors or credentials.
        return result

    async def research_request(self, scope, receive):
        if (scope['path'] in {'/api/v1/research/tickets','/api/v1/analytics'} and self.public_origin.startswith('https:') and scope.get('scheme')!='https'):
            return 400, b'{"error_code":"https_required"}', 'application/json'
        if (scope.get('query_string') or scope.get('raw_path', scope['path'].encode()) != scope['path'].encode()
                or dict(scope.get('headers',[])).get(b'origin',b'').decode() not in
                   ({self.public_origin, 'http://124.222.57.236:18080'} if scope['path']=='/api/v1/prediction/presence' else {self.public_origin})
                or dict(scope.get('headers',[])).get(b'content-type',b'').split(b';')[0] != b'application/json'):
            return 400, b'{"error_code":"invalid_research_request"}', 'application/json'
        peer = str(scope.get('client', ('unknown',))[0])
        now = time.monotonic()
        start, count = self.research_clients.get(peer, (now, 0))
        if now-start >= 60:
            start, count = now, 0
        if count >= 40:
            return 429, b'{"error_code":"research_rate_limited"}', 'application/json'
        self.research_clients[peer] = (start, count+1)
        self.research_clients.move_to_end(peer)
        while len(self.research_clients)>256:
            self.research_clients.popitem(last=False)
        raw = bytearray()
        for _ in range(16):
            event = await receive()
            if event['type'] != 'http.request':
                raise ValueError('research_request_disconnected')
            raw.extend(event.get('body',b''))
            if len(raw)>4096:
                raise ValueError('research_request_too_large')
            if not event.get('more_body',False):
                break
        else:
            raise ValueError('research_request_too_many_chunks')
        from sushiwait.storage import _unique_object
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
            raise ValueError('invalid_research_request')
        if scope['path']=='/api/v1/analytics':
            return await self.analysis_request(value)
        from sushiwait.ticketresearch import TicketResearch
        def process():
            research = TicketResearch(self.research_file)
            try:
                if scope['path']=='/api/v1/prediction/presence':
                    return research.presence({k:v for k,v in value.items() if k!='schema_version'})
                if value.get('action')=='submit' and set(value)=={'schema_version','action','record_id','owner_secret','ticket'}:
                    saved = research.submit(value['record_id'],value['owner_secret'],value['ticket'])
                    def read_day(store, date):
                        if self.official_view is None:
                            raise ValueError('official_view_not_configured')
                        status, body, _ = self.official_view.dispatch(f'/api/v1/official/stores/{store}/days/{date}')
                        if status != 200:
                            raise ValueError('saved_archive_unavailable')
                        return json.loads(body)
                    research.update_labels(read_day, record_id=value['record_id'])
                    return saved
                if value.get('action')=='list' and {'schema_version','action','owner_secret'}<=set(value) and not set(value)-{'schema_version','action','owner_secret','page','page_size','filters'}:
                    return research.query_records(value['owner_secret'],page=value.get('page',1),page_size=value.get('page_size',100),filters=value.get('filters'))
                raise ValueError('invalid_research_action')
            finally:
                research.close()
        async with self.research_lock:
            result = await asyncio.to_thread(process)
        return 200, json.dumps(result,ensure_ascii=False,allow_nan=False).encode(), 'application/json'

    async def analysis_request(self, value):
        from pathlib import Path
        from datetime import timedelta
        from sushiwait.analytics import selection, build_report, ai_context
        from sushiwait.analyticsai import write_state, read_state
        action=value.get('action')
        if action not in {'report','explain'} or set(value) != ({'schema_version','action','selection'} if action=='report' else {'schema_version','action','selection','report_id','visible'}):
            raise ValueError('invalid_analysis_request')
        selected=selection(value['selection']); key=json.dumps(selected,sort_keys=True)
        reader=self.official_view if selected['source']=='sapi_miniapp_gateway' else self.hub
        if reader is None:raise ValueError('analysis_source_unavailable')
        if action=='report':
            async with self.analysis_lock:
                saved=self.analysis_cache.get(key)
                if saved and time.monotonic()-saved[0]<90:report=saved[1]
                else:
                    def read(route):
                        status,body,_=reader.dispatch(route)
                        if status!=200:raise ValueError('archive_unavailable')
                        return json.loads(body)
                    report=await asyncio.to_thread(build_report,read,reader.names,selected)
                    self.analysis_cache[key]=(time.monotonic(),report)
                    self.analysis_cache.move_to_end(key)
                    while len(self.analysis_cache)>8:self.analysis_cache.popitem(last=False)
                result=report
        else:
            if not self.model_advice_file or type(value['visible']) is not bool:raise ValueError('analysis_ai_not_configured')
            saved=self.analysis_cache.get(key)
            if not saved or saved[1]['report_id']!=value['report_id']:raise ValueError('analysis_report_changed')
            report=saved[1];context=ai_context(report)
            root=Path(self.research_file).parent;job=root/'analytics-job.json'
            now=datetime.now(timezone.utc)
            old=read_state(job) if job.exists() else {}
            if old.get('report_id')!=report['report_id'] and old.get('expires_at') and datetime.fromisoformat(old['expires_at'])>now:
                result={'state':'busy'}
            else:
                write_state(job,{'report_id':report['report_id'],'context':context,'expires_at':(now+timedelta(seconds=120 if value['visible'] else 0)).isoformat()})
                result={'state':'queued' if value['visible'] else 'released'}
        return 200,json.dumps(result,ensure_ascii=False,allow_nan=False).encode(),'application/json'

    @staticmethod
    def supports_query(path, query):
        """Accept only the engineering page's public selection bookmark.

        Selection stays in browser code. It never changes a projection route,
        source request, cache key, file path, or model invocation.
        """
        if not query:
            return True
        if path != '/statistics-legacy' or len(query) > 1024:
            return False
        try:
            pairs = parse_qsl(query.decode('ascii'), keep_blank_values=True,
                             strict_parsing=True, errors='strict', max_num_fields=6)
            if len(dict(pairs)) != len(pairs):
                return False
            for key, value in pairs:
                if key == 'section':
                    valid = value in {'overview', 'analysis', 'quality', 'prediction', 'experience', 'tickets', 'intelligence'}
                elif key == 'date':
                    valid = bool(re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', value))
                    if valid:
                        datetime.strptime(value, '%Y-%m-%d')
                elif key == 'store':
                    valid = bool(re.fullmatch(r'[1-9][0-9]{0,9}', value))
                elif key == 'city':
                    valid = value == 'all' or bool(re.fullmatch(r'[\u4e00-\u9fff]{1,16}', value))
                elif key == 'queue':
                    valid = value in {'mixedQueue', 'reservationQueue'}
                elif key == 'source':
                    valid = value in {'crm_remote_v1_1', 'sapi_miniapp_gateway'}
                else:
                    return False
                if not valid:
                    return False
            return bool(pairs)
        except (ValueError, UnicodeError):
            return False

    async def screenshot_request(self, scope, receive):
        headers = dict(scope.get('headers', []))
        if (scope.get('scheme')!='https' or headers.get(b'origin', b'').decode()!=self.public_origin
                or scope.get('query_string') or scope.get('raw_path',scope['path'].encode())!=scope['path'].encode()
                or headers.get(b'content-type') not in {b'image/png',b'image/jpeg'}):
            return 400, b'{"error_code":"invalid_screenshot_request"}', 'application/json'
        peer=str(scope.get('client', ('unknown',))[0]);now=time.monotonic()
        start,count=self.ocr_clients.get(peer,(now,0))
        if now-start>=60:start,count=now,0
        if count>=20 or self.ocr_lock.locked():
            return 429,b'{"error_code":"screenshot_busy"}','application/json'
        self.ocr_clients[peer]=(start,count+1);self.ocr_clients.move_to_end(peer)
        while len(self.ocr_clients)>128:self.ocr_clients.popitem(last=False)
        from sushiwait.screenshot import MAX_BYTES, recognize
        raw=bytearray()
        for _ in range(1024):
            event=await receive()
            if event['type']!='http.request':raise ValueError('image_disconnected')
            raw.extend(event.get('body',b''))
            if len(raw)>MAX_BYTES:raise ValueError('image_size_unsupported')
            if not event.get('more_body',False):break
        else:raise ValueError('image_chunks_unsupported')
        names={**self.hub.names,**(self.official_view.names if self.official_view else {})}
        from pathlib import Path
        async with self.ocr_lock:
            value=await asyncio.to_thread(recognize,bytes(raw),names,directory=Path(self.research_file).parent)
        return 200,json.dumps(value,ensure_ascii=False).encode(),'application/json'

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        path = scope['path']
        if scope['method']=='POST' and path=='/api/v1/research/screenshots/recognize' and self.ocr_enabled and self.research_file is not None:
            try:
                status,body,mime=await asyncio.wait_for(self.screenshot_request(scope,receive),timeout=30)
            except Exception:
                status,body,mime=400,b'{"error_code":"screenshot_not_recognized"}','application/json'
        elif (scope['method']=='POST' and self.research_file is not None
                and path in {'/api/v1/research/tickets','/api/v1/prediction/presence','/api/v1/analytics'}):
            try:
                if self.deadline is not None and datetime.now(timezone.utc)>=self.deadline:
                    raise ValueError('trial_deadline_reached')
                status,body,mime = await asyncio.wait_for(self.research_request(scope,receive),timeout=35 if path=='/api/v1/analytics' else 3)
            except Exception:
                status,body,mime = 400,b'{"error_code":"research_request_not_saved"}','application/json'
        elif scope['method'] != 'GET':
            status, body, mime = 405, b'GET only', 'text/plain; charset=utf-8'
        elif (not self.supports_query(path, scope.get('query_string', b''))
              or scope.get('raw_path', path.encode()) != path.encode()):
            status, body, mime = 400, b'Parameters are not supported', 'text/plain; charset=utf-8'
        elif not self.allowed(path):
            status, body, mime = 404, b'Statistics route not found', 'text/plain; charset=utf-8'
        elif self.deadline is not None and datetime.now(timezone.utc) >= self.deadline:
            status, body, mime = 503, b'Trial deadline reached', 'text/plain; charset=utf-8'
        else:
            # Serialize bounded cache fills. The hub reads worker-owned copies,
            # never an active SQLite database or the restaurant origin.
            async with self.lock:
                saved = self.cache.get(path)
                if saved and time.monotonic() - saved[0] < 5:
                    _, status, body, mime = saved
                else:
                    if path == '/analytics-advice.json':
                        try:
                            from pathlib import Path
                            from sushiwait.analyticsai import read_state
                            value=read_state(Path(self.model_advice_file).parent/'analytics-result.json')
                            body=json.dumps({k:value.get(k) for k in ('report_id','updated_at','state','advice')},ensure_ascii=False).encode()
                            status,mime=200,'application/json'
                        except Exception:
                            status,body,mime=200,b'{"state":"not_requested"}', 'application/json'
                    elif path == '/api/v1/collection/health':
                        try:
                            body = json.dumps(self.collection_health()).encode()
                            status, mime = 200, 'application/json'
                        except Exception:
                            status, body, mime = 503, b'{"state":"unavailable"}', 'application/json'
                    elif path == '/wait-reference.json' and self.research_file:
                        try:
                            from sushiwait.ticketresearch import TicketResearch
                            research = TicketResearch(self.research_file)
                            try:value=research.public_wait_summary()
                            finally:research.close()
                            status,body,mime=200,json.dumps(value).encode(),'application/json'
                        except Exception:
                            status,body,mime=503,b'Wait reference unavailable','text/plain'
                    elif path == '/model-advice.json' and self.model_advice_file:
                        try:
                            from sushiwait.credentials import _read_private_file
                            from sushiwait.modeladvice import public_projection
                            value = json.loads(_read_private_file(self.model_advice_file))
                            body = json.dumps(public_projection(value)).encode()
                            status, mime = 200, 'application/json'
                        except Exception:
                            status, body, mime = 503, b'Model advice unavailable', 'text/plain; charset=utf-8'
                    elif path == '/api/v1/statistics/settings':
                        try:
                            body = json.dumps(self.polling_settings()).encode()
                            status, mime = 200, 'application/json'
                        except Exception:
                            status, body, mime = 503, b'Sampling policy unavailable', 'text/plain; charset=utf-8'
                    else:
                        reader = self.official_view if path.startswith('/api/v1/official/') else self.hub
                        route = '/statistics' if path == '/statistics-history' else path
                        status, body, mime = await asyncio.to_thread(reader.dispatch, route)
                    self.cache[path] = (time.monotonic(), status, body, mime)
                    self.cache.move_to_end(path)
                    while len(self.cache) > 16:
                        self.cache.popitem(last=False)
        if path in {'/statistics', '/statistics-legacy', '/statistics-history'} and status == 200:
            # The internal monitor is deliberately outside the public scope.
            body = body.replace(b'<a href="/monitor">' + '最近观测'.encode() + b'</a>',
                '<span>每30秒自动更新</span>'.encode())
        if path in {'/statistics', '/statistics-legacy', '/statistics-history'} and status == 200 and self.calendar_source == 'sapi_miniapp_gateway':
            marker = (b'id="sushiwait-month-calendar"' if path != '/statistics-legacy'
                      else b'id="sushiwait-reference"')
            if body.count(marker) != 1 or b'data-statistics-source=' in body:
                status, body, mime = 503, b'Calendar source binding unavailable', 'text/plain; charset=utf-8'
            else:
                body = body.replace(marker, marker + b' data-statistics-source="sapi_miniapp_gateway" data-statistics-history="combined"', 1)
        if path in {'/statistics', '/statistics-history', '/statistics-legacy'} and status == 200:
            marker = b'id="sushiwait-reference"' if path == '/statistics-legacy' else b'id="sushiwait-month-calendar"'
            try:
                seconds = self.polling_seconds()
                if body.count(marker) != 1 or b'data-statistics-poll-seconds=' in body:
                    raise ValueError('calendar_refresh_binding_unavailable')
                body = body.replace(marker, marker + f' data-statistics-poll-seconds="{seconds}"'.encode(), 1)
            except Exception:
                status, body, mime = 503, b'Calendar settings unavailable', 'text/plain; charset=utf-8'
        headers = [(b'content-type', mime.encode()), (b'content-length', str(len(body)).encode()),
            (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
            (b'referrer-policy', b'no-referrer'),
            (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")]
        if status == 405:
            headers.append((b'allow', b'GET'))
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': body})


async def serve(args):
    import uvicorn
    from sushiwait.officialview import read_config as read_official_config
    hub = MonitorHub(read_config(args.config_file))
    official = read_official_config(args.official_view_file) if args.official_view_file else None
    if args.extra_official_view_file:
        if official is None:
            raise ValueError('primary_official_view_required')
        official = JoinedOfficialViews(official, read_official_config(args.extra_official_view_file))
    app = StatisticsGateway(hub, official_view=official, calendar_source=args.calendar_source,
        refresh_seconds=args.refresh_seconds, refresh_policy_file=args.refresh_policy_file, model_advice_file=args.model_advice_file,
        research_file=args.research_file, public_origin=args.public_origin, ocr_enabled=args.ocr_enabled,
        collection_status_file=args.collection_status_file)
    remaining = (app.deadline - datetime.now(timezone.utc)).total_seconds() if app.deadline is not None else None
    if remaining is not None and remaining <= 0:
        raise ValueError('original_trial_deadline_reached')
    server = uvicorn.Server(uvicorn.Config(app, host=args.listen_host, port=args.port,
        proxy_headers=args.listen_host=='127.0.0.1', forwarded_allow_ips='127.0.0.1', server_header=False, access_log=False,
        limit_concurrency=16, backlog=32, timeout_keep_alive=3))
    async def expire():
        await asyncio.sleep(remaining)
        server.should_exit = True
    timer = asyncio.create_task(expire()) if remaining is not None else None
    try:
        await server.serve()
    finally:
        if timer is not None:
            timer.cancel()
            try:
                await timer
            except asyncio.CancelledError:
                pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-file', required=True)
    parser.add_argument('--collection-status-file', help='Optional bounded public sampling health projection')
    parser.add_argument('--model-advice-file', help='Optional validated public Flash advice copy; never a key file')
    parser.add_argument('--ocr-enabled', action='store_true', help='Optional private on-server screenshot OCR')
    parser.add_argument('--public-origin', default='https://124.222.57.236', help='Exact HTTPS origin for private forms')
    parser.add_argument('--research-file', help='Optional private owner-scoped test database and viewing leases')
    parser.add_argument('--official-view-file', help='Optional private official public-field archive configuration; no credentials')
    parser.add_argument('--extra-official-view-file', help='Optional disjoint two-store saved archive; no extra upstream access')
    parser.add_argument('--calendar-source', choices=['crm_remote_v1_1', 'sapi_miniapp_gateway'],
        default='crm_remote_v1_1', help='Explicit source for the calendar; official mode requires its view file')
    parser.add_argument('--refresh-seconds', type=int, default=30, help='Saved-statistics refresh cadence, 30–3600 seconds')
    parser.add_argument('--refresh-policy-file', help='Read only the collector cadence policy; no query credentials')
    parser.add_argument('--listen-host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18900)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('a non-privileged TCP port is required')
    asyncio.run(serve(args))
