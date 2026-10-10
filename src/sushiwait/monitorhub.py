"""A bounded local observation hub over explicit local collector projections.

This module never contacts the restaurant, opens a collector database, starts a
collector, or restarts a failed task. Each selected store retains its worker's
actual state and budget. Local HTTP reads are distinct from upstream queries.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import http.client
import json
import re
import threading
import hashlib
from urllib.parse import urlsplit

from .credentials import _read_private_file
from .outcomes import _json, _time, _utc
from .remoteservice import _MONITOR_CSP

MAX_BODY = 2_097_152
MAX_FLEET_INDEX = 8 * 1024 * 1024


class HubError(ValueError):
    def __init__(self, code='monitor_hub_unavailable'):
        self.error_code = code if code in {'monitor_hub_invalid_config',
            'monitor_hub_unavailable', 'monitor_hub_scope_mismatch',
            'monitor_hub_invalid_response'} else 'monitor_hub_unavailable'
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def validate_config(value, *, now=None):
    try:
        clock = _clock() if now is None else now
        if type(value) is not dict or type(value.get('schema_version')) is not int:
            raise ValueError
        fleet = value['schema_version'] == 3
        continuous = value['schema_version'] in (2, 3)
        if (continuous and (set(value) != {'schema_version', 'mode', 'workers'}
                or value['mode'] != ('daily_fleet_readonly' if fleet else 'daily_controller_readonly'))
                or not continuous and (value['schema_version'] != 1
                    or set(value) != {'schema_version', 'deadline_at', 'workers'}
                    or not clock < _time(value['deadline_at']) <= clock+timedelta(days=14))
                or type(value['workers']) is not list or not 1 <= len(value['workers']) <= (1 if fleet else 16)):
            raise ValueError
        stores, endpoints, workers = set(), set(), []
        for worker in value['workers']:
            if type(worker) is not dict or set(worker) != {'endpoint', 'stores'}:
                raise ValueError
            match = re.fullmatch(r'http://127\.0\.0\.1:([0-9]{1,5})', worker['endpoint'])
            if (match is None or not 1 <= int(match[1]) <= 65535
                    or worker['endpoint'] in endpoints or type(worker['stores']) is not dict
                    or not 1 <= len(worker['stores']) <= (256 if fleet else 1 if continuous else 3)):
                raise ValueError
            for identity, name in worker['stores'].items():
                if (type(identity) is not str or not re.fullmatch('[1-9][0-9]{0,9}', identity)
                        or int(identity) > 2**31-1 or identity in stores or type(name) is not str
                        or not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name)):
                    raise ValueError
                stores.add(identity)
            endpoints.add(worker['endpoint'])
            workers.append({'endpoint': worker['endpoint'], 'stores': dict(worker['stores'])})
        if continuous:
            return {'schema_version': 3 if fleet else 2,
                'mode': 'daily_fleet_readonly' if fleet else 'daily_controller_readonly', 'workers': workers}
        return {'schema_version': 1, 'deadline_at': _utc(_time(value['deadline_at'])), 'workers': workers}
    except Exception:
        raise HubError('monitor_hub_invalid_config') from None


def read_config(path):
    try:
        return validate_config(_json(_read_private_file(path)))
    except Exception:
        raise HubError('monitor_hub_invalid_config') from None


def _read_local(worker, path):
    large_index = path == '/api/v1/days' or re.fullmatch(r'/api/v1/months/20[0-9]{2}-[0-9]{2}', path)
    bound = MAX_FLEET_INDEX if large_index else MAX_BODY
    connection = http.client.HTTPConnection('127.0.0.1', int(worker['endpoint'].rsplit(':', 1)[1]), timeout=8 if large_index else 3)
    try:
        connection.request('GET', path, headers={'Accept': 'application/json', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise HubError()
        raw = response.read(bound+1)
        if len(raw) > bound:
            raise HubError('monitor_hub_invalid_response')
        value = _json(raw)
        if type(value) is not dict:
            raise HubError('monitor_hub_invalid_response')
        return value
    except HubError:
        raise
    except Exception:
        raise HubError() from None
    finally:
        connection.close()


class MonitorHub:
    def __init__(self, config, *, reader=None, now=None):
        self.config = validate_config(config, now=now)
        self.reader = _read_local if reader is None else reader
        self.names = {store:name for w in self.config['workers'] for store,name in w['stores'].items()}
        self.by_store = {store:w for w in self.config['workers'] for store in w['stores']}

    def status(self, store=None):
        store = next(iter(self.names)) if store is None else store
        if store not in self.by_store:
            raise HubError('monitor_hub_scope_mismatch')
        worker = self.by_store[store]
        fleet = self.config['schema_version'] == 3
        value = self.reader(worker, f'/api/v1/stores/{store}/status' if fleet else '/api/v1/status')
        if (type(value) is not dict or value.get('store_ids') != ([store] if fleet else list(worker['stores']))
                or fleet and value.get('fleet_store_ids') != list(worker['stores'])
                or value.get('service_state') not in {'running','starting','ready','not_started',
                    'completed','failed','stopped'} or type(value.get('worker_alive')) is not bool
                or value.get('network_performed_by_read') is not False or value.get('eta_available') is not False):
            raise HubError('monitor_hub_scope_mismatch')
        return {**value, 'store_ids': list(self.names), 'store_names': dict(self.names),
            'selected_store_id': store, 'active_batch_store_ids': list(worker['stores']),
            'hub_worker_count': len(self.config['workers']), 'status_scope': 'selected_store_worker',
            'local_projection_read_performed': True, 'upstream_network_performed_by_hub': False,
            'collector_database_opened_by_hub': False, 'collector_started_by_hub': False}

    def projection(self, store, kind):
        if store not in self.by_store or kind not in {'queue', 'history'}:
            raise HubError('monitor_hub_scope_mismatch')
        value = self.reader(self.by_store[store], f'/api/v1/stores/{store}/{kind}')
        if (type(value) is not dict or value.get('requested_store_id') != store
                or value.get('network_performed_by_read') is not False or value.get('eta_available') is not False):
            raise HubError('monitor_hub_scope_mismatch')
        return value

    def asset(self, path):
        name, mime = {'/monitor': ('monitor.html', 'text/html; charset=utf-8'),
            '/monitor.js': ('monitor.js', 'text/javascript; charset=utf-8'),
            '/monitor.css': ('monitor.css', 'text/css; charset=utf-8'),
            '/statistics':('statistics.html','text/html; charset=utf-8'),
            '/statistics.js':('statistics.js','text/javascript; charset=utf-8'),
            '/reference-model.js':('reference-model.js','text/javascript; charset=utf-8'),
            '/experience-model.js':('experience-model.js','text/javascript; charset=utf-8'),
            '/experience-ui.js':('experience-ui.js','text/javascript; charset=utf-8'),
            '/statistics.css':('statistics.css','text/css; charset=utf-8')}[path]
        raw = files('sushiwait').joinpath('web', name).read_text()
        if path == '/monitor.js':
            old = 'const status=await getJSON("/api/v1/status");'
            new = 'const status=await getJSON(selected?`/api/v1/stores/${encodeURIComponent(selected)}/status`:"/api/v1/status");'
            label = 'option.textContent=`门店 ${id}`;'
            if raw.count(old) != 1 or raw.count(label) != 1:
                raise HubError('monitor_hub_invalid_response')
            raw = raw.replace(old, new).replace(label, 'option.textContent=status.store_names?.[id]||`门店 ${id}`;')
            raw = raw.replace('本任务累计', '当前门店所属批次累计')
        if path == '/monitor':
            raw = raw.replace('查看已保存的门店观测、展示号码变化和采集缺口。',
                f'观察 {len(self.names)} 家门店的已保存数据；状态和查询预算对应当前门店所属批次。切换门店不会增加寿司郎查询。')
        return raw.encode(), mime

    def daily(self,store,day=None,month=None):
        if store not in self.by_store:raise HubError('monitor_hub_scope_mismatch')
        if month is not None:
            from .dailyarchive import selected_month
            selected_month(month)
        path=f'/api/v1/stores/{store}/months/{month}' if month else f'/api/v1/stores/{store}/days'+('/'+day if day else '')
        value=self.reader(self.by_store[store],path)
        if (type(value) is not dict or value.get('daily_schema_version')!=1
                or value.get('requested_store_id')!=store or value.get('network_performed_by_read') is not False
                or value.get('eta_available') is not False or day and value.get('local_date')!=day
                or month and value.get('month') != month):
            raise HubError('monitor_hub_scope_mismatch')
        return value

    def daily_index(self,month=None):
        if self.config['schema_version'] == 3:
            return self.fleet_index(month)
        days={};unavailable=[]
        for store in self.names:
            try:
                value=self.daily(store,month=month)
                summaries=value.get('days')
                if type(summaries) is not list or len(summaries)>(31 if month else 31 if self.config['schema_version']==2 else 16):raise HubError()
                if value.get('unavailable_archive_dates'):unavailable.append(store)
                seen=set()
                for item in summaries:
                    if (type(item) is not dict or item.get('store_id')!=store
                            or type(item.get('local_date')) is not str
                            or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',item['local_date'])
                            or item['local_date'] in seen
                            or month and not item['local_date'].startswith(month+'-')):raise HubError()
                    seen.add(item['local_date'])
                for item in summaries:days.setdefault(item['local_date'],{})[store]=item
            except (HubError,ValueError):
                if store not in unavailable:unavailable.append(store)
        return {**({'month':month} if month else {}),'daily_schema_version':1,'source':'crm_remote_v1_1','days':days,
            'store_names':dict(self.names),'configured_store_ids':list(self.names),'unavailable_store_ids':unavailable,
            'cohort_id':hashlib.sha256(json.dumps(list(self.names),separators=(',',':')).encode()).hexdigest(),
            'heatmap_semantics':'coverage_only_traffic_not_calibrated','actual_called_count':None,
            'network_performed_by_read':False,'upstream_network_performed_by_hub':False,'eta_available':False}

    def fleet_index(self, month=None):
        if month is not None:
            from .dailyarchive import selected_month
            selected_month(month)
        value = self.reader(self.config['workers'][0], '/api/v1/months/'+month if month else '/api/v1/days')
        if (type(value) is not dict or value.get('daily_schema_version') != 1
                or value.get('configured_store_ids') != list(self.names)
                or value.get('store_names') != self.names or value.get('network_performed_by_read') is not False
                or value.get('eta_available') is not False or month and value.get('month') != month
                or type(value.get('days')) is not dict or len(value['days']) > 31
                or type(value.get('unavailable_store_ids')) is not list
                or set(value['unavailable_store_ids']) - set(self.names)):
            raise HubError('monitor_hub_scope_mismatch')
        from .dailyfleet import SUMMARY_KEYS
        if 'calendar_index_state' in value:
            pending=value.get('calendar_pending_store_ids')
            if (type(pending) is not list or any(type(s) is not str for s in pending)
                    or len(set(pending))!=len(pending) or set(pending)-set(self.names)
                    or not set(pending)<=set(value['unavailable_store_ids'])
                    or value['calendar_index_state']!=('preparing' if pending else 'ready')
                    or type(value.get('calendar_verified_store_count')) is not int
                    or value['calendar_verified_store_count']!=len(self.names)-len(pending)):
                raise HubError('monitor_hub_scope_mismatch')
        for day, summaries in value['days'].items():
            if (not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', day)
                    or not day.startswith(value.get('month', '')+'-') or type(summaries) is not dict
                    or set(summaries) - set(self.names)):
                raise HubError('monitor_hub_scope_mismatch')
            for store, row in summaries.items():
                if (type(row) is not dict or set(row) != set(SUMMARY_KEYS)
                        or row['store_id'] != store or row['local_date'] != day):
                    raise HubError('monitor_hub_scope_mismatch')
        return {**value, 'upstream_network_performed_by_hub': False}

    def dispatch(self, target, *, method='GET', body_present=False, now=None):
        clock = _clock() if now is None else now
        if method != 'GET':
            return self._error(405, 'method_not_allowed')
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or body_present:
            return self._error(400, 'request_parameters_not_supported')
        if self.config['schema_version']==1 and clock >= _time(self.config['deadline_at']):
            return self._error(503, 'monitor_hub_deadline_reached')
        try:
            if parsed.path in {'/monitor','/monitor.js','/monitor.css','/statistics','/statistics.js','/statistics.css','/reference-model.js','/experience-model.js','/experience-ui.js'}:
                raw, mime = self.asset(parsed.path)
                return 200, raw, mime
            if parsed.path == '/api/v1/status':
                value = self.status()
            elif parsed.path=='/api/v1/days':value=self.daily_index()
            elif re.fullmatch(r'/api/v1/months/20[0-9]{2}-[0-9]{2}',parsed.path):
                from .dailyarchive import selected_month
                month=parsed.path.rsplit('/',1)[1];selected_month(month);value=self.daily_index(month)
            elif re.fullmatch(r'/api/v1/stores/[1-9][0-9]{0,9}/days(?:/[0-9]{4}-[0-9]{2}-[0-9]{2})?',parsed.path):
                pieces=parsed.path.split('/');value=self.daily(pieces[4],pieces[6] if len(pieces)==7 else None)
            else:
                match = re.fullmatch('/api/v1/stores/([1-9][0-9]{0,9})/(status|queue|history)', parsed.path)
                if match is None or match[1] not in self.by_store:
                    return self._error(404, 'store_or_route_not_in_scope')
                value = self.status(match[1]) if match[2] == 'status' else self.projection(match[1], match[2])
            body = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            bound=MAX_FLEET_INDEX if self.config['schema_version']==3 and (
                parsed.path=='/api/v1/days' or re.fullmatch(r'/api/v1/months/20[0-9]{2}-[0-9]{2}',parsed.path)) else MAX_BODY
            if len(body) > bound:
                raise HubError('monitor_hub_invalid_response')
            return 200, body, 'application/json; charset=utf-8'
        except Exception as error:
            return self._error(503, error.error_code if isinstance(error, HubError) else 'monitor_hub_unavailable')

    @staticmethod
    def _error(status, code):
        return status, json.dumps({'error_code': code, 'eta_available': False,
            'upstream_network_performed_by_hub': False}).encode(), 'application/json; charset=utf-8'


def serve_hub(config, *, port=51930, ready=None, stop=None):
    """Serve only on loopback, at most 16 concurrent local projection reads."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise HubError('monitor_hub_invalid_config')
    hub = MonitorHub(config)
    slots = threading.BoundedSemaphore(16)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(8)
        def log_message(self, *args):
            pass
        def do_GET(self):
            if not slots.acquire(blocking=False):
                status, body, mime = hub._error(503, 'monitor_hub_busy')
            else:
                try:
                    status, body, mime = hub.dispatch(self.path,
                        body_present=('Content-Length' in self.headers or 'Transfer-Encoding' in self.headers))
                finally:
                    slots.release()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if mime.startswith(('text/html', 'text/javascript', 'text/css')):
                self.send_header('Content-Security-Policy', _MONITOR_CSP.decode())
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            self.send_error(405)
        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST
    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        server.daemon_threads = True
        server.timeout = 1
        if ready is not None:
            ready(server.server_address[1])
        while (hub.config['schema_version'] in (2, 3) or _clock() < _time(hub.config['deadline_at'])) and not (stop is not None and stop.is_set()):
            server.handle_request()
