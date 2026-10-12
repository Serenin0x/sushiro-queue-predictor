"""Optional read-only statistics gateway; exposure requires an operator decision.

Reads configured loopback projections only. No tickets, credentials, writes,
collector control routes or filesystem downloads are exposed.
"""
import argparse
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import re
import time
import json

from sushiwait.monitorhub import MonitorHub, read_config


class StatisticsGateway:
    def __init__(self, hub, *, official_view=None, calendar_source='crm_remote_v1_1',
                 refresh_seconds=30, refresh_policy_file=None):
        if (calendar_source not in {'crm_remote_v1_1', 'sapi_miniapp_gateway'}
                or calendar_source == 'sapi_miniapp_gateway' and official_view is None):
            raise ValueError('calendar_source_requires_configured_view')
        self.hub = hub
        self.official_view = official_view
        self.calendar_source = calendar_source
        if type(refresh_seconds) is not int or not 30 <= refresh_seconds <= 3600:
            raise ValueError('statistics_refresh_interval_invalid')
        self.refresh_seconds = refresh_seconds
        self.refresh_policy_file = refresh_policy_file
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.deadline = datetime.fromisoformat(hub.config['deadline_at'].replace('Z', '+00:00')) if hub.config['schema_version']==1 else None

    def allowed(self, path):
        if path in {'/statistics-history', '/api/v1/statistics/settings'}:
            return True
        if path.startswith('/api/v1/official/'):
            return bool(self.official_view is not None and self.official_view.allowed(path))
        if path in {'/statistics', '/statistics.js', '/statistics.css', '/reference-model.js', '/reference-alerts.js', '/experience-model.js', '/experience-ui.js', '/statistics-legacy', '/calendar.css', '/calendar.mjs', '/calendar-data.mjs', '/calendar-directory.mjs', '/calendar-icons.mjs', '/statistics-client.mjs', '/saved-statistics.mjs', '/engineering-logo.svg', '/api/v1/days'}:
            return True
        if re.fullmatch(r'/api/v1/months/20[0-9]{2}-(?:0[1-9]|1[0-2])',path):
            return True
        match = re.fullmatch(r'/api/v1/stores/([1-9][0-9]{0,9})/days/([0-9]{4}-[0-9]{2}-[0-9]{2})', path)
        return bool(match and match[1] in self.hub.names)

    def polling_seconds(self):
        if self.refresh_policy_file is None:
            return self.refresh_seconds
        from sushiwait.credentials import _read_private_file
        from sushiwait.storage import _unique_object
        value = json.loads(_read_private_file(self.refresh_policy_file), object_pairs_hook=_unique_object)
        if (type(value) is not dict or set(value) != {'schema_version', 'interval_seconds', 'ordinary_query_limit'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['interval_seconds']) is not int or not 60 <= value['interval_seconds'] <= 3600
                or type(value['ordinary_query_limit']) is not int or not 1 <= value['ordinary_query_limit'] <= 396):
            raise ValueError('statistics_refresh_policy_invalid')
        return value['interval_seconds']

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
        if scope['method'] != 'GET':
            status, body, mime = 405, b'GET only', 'text/plain; charset=utf-8'
        elif scope.get('query_string') or scope.get('raw_path', path.encode()) != path.encode():
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
                    if path == '/api/v1/statistics/settings':
                        try:
                            body = json.dumps({'schema_version':1, 'poll_seconds':self.polling_seconds()}).encode()
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
            (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")]
        if status == 405:
            headers.append((b'allow', b'GET'))
        await send({'type': 'http.response.start', 'status': status, 'headers': headers})
        await send({'type': 'http.response.body', 'body': body})


async def serve(args):
    import uvicorn
    from sushiwait.officialview import read_config as read_official_config
    hub = MonitorHub(read_config(args.config_file))
    official = read_official_config(args.official_view_file) if args.official_view_file else None
    app = StatisticsGateway(hub, official_view=official, calendar_source=args.calendar_source,
        refresh_seconds=args.refresh_seconds, refresh_policy_file=args.refresh_policy_file)
    remaining = (app.deadline - datetime.now(timezone.utc)).total_seconds() if app.deadline is not None else None
    if remaining is not None and remaining <= 0:
        raise ValueError('original_trial_deadline_reached')
    server = uvicorn.Server(uvicorn.Config(app, host=args.listen_host, port=args.port,
        proxy_headers=False, server_header=False, access_log=False,
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
    parser.add_argument('--official-view-file', help='Optional private official public-field archive configuration; no credentials')
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
