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

from sushiwait.monitorhub import MonitorHub, read_config


class StatisticsGateway:
    def __init__(self, hub):
        self.hub = hub
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.deadline = datetime.fromisoformat(hub.config['deadline_at'].replace('Z', '+00:00')) if hub.config['schema_version']==1 else None

    def allowed(self, path):
        if path in {'/statistics', '/statistics.js', '/statistics.css', '/reference-model.js', '/api/v1/days'}:
            return True
        if re.fullmatch(r'/api/v1/months/20[0-9]{2}-(?:0[1-9]|1[0-2])',path):
            return True
        match = re.fullmatch(r'/api/v1/stores/([1-9][0-9]{0,9})/days/([0-9]{4}-[0-9]{2}-[0-9]{2})', path)
        return bool(match and match[1] in self.hub.names)

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
                    status, body, mime = await asyncio.to_thread(self.hub.dispatch, path)
                    self.cache[path] = (time.monotonic(), status, body, mime)
                    self.cache.move_to_end(path)
                    while len(self.cache) > 16:
                        self.cache.popitem(last=False)
        if path == '/statistics' and status == 200:
            # The internal monitor is deliberately outside the public scope.
            body = body.replace(b'<a href="/monitor">' + '最近观测'.encode() + b'</a>',
                '<span>每30秒自动更新</span>'.encode())
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
    hub = MonitorHub(read_config(args.config_file))
    app = StatisticsGateway(hub)
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
    parser.add_argument('--listen-host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18900)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('a non-privileged TCP port is required')
    asyncio.run(serve(args))
