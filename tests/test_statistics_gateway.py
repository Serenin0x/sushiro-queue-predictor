import asyncio
from datetime import datetime, timezone, timedelta
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('statistics_gateway', Path(__file__).resolve().parents[1] / 'deploy/statistics_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class Hub:
    def __init__(self):
        self.names = {'3014': 'Test store'}
        self.config = {'deadline_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.calls = []

    def dispatch(self, path):
        self.calls.append(path)
        return 200, b'{}', 'application/json'


class StatisticsGatewayTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.app = gateway.StatisticsGateway(self.hub)

    def request(self, path='/api/v1/days', method='GET', query=b'', raw=None):
        async def run():
            messages = []
            async def send(value):
                messages.append(value)
            async def receive():
                return {'type': 'http.request', 'body': b''}
            scope = {'type': 'http', 'method': method, 'path': path,
                     'raw_path': path.encode() if raw is None else raw, 'query_string': query}
            await self.app(scope, receive, send)
            return messages
        return asyncio.run(run())

    def test_fixed_statistics_and_known_store_date_only(self):
        for path in ['/statistics', '/statistics.js', '/statistics.css', '/api/v1/days',
                     '/api/v1/stores/3014/days/2026-10-09']:
            self.assertEqual(self.request(path)[0]['status'], 200)
        self.assertEqual(len(self.hub.calls), 5)

    def test_no_management_status_ticket_queue_or_file_routes(self):
        for path in ['/api/v1/status', '/monitor', '/api/v1/stores/3014/queue',
                     '/api/v1/stores/3014/status', '/etc/passwd', '/hub.json',
                     '/api/v1/stores/3004/days/2026-10-09']:
            self.assertEqual(self.request(path)[0]['status'], 404)
        self.assertEqual(self.hub.calls, [])

    def test_mutation_methods_rejected_without_projection_reads(self):
        for method in ['POST', 'PUT', 'DELETE', 'PATCH', 'HEAD']:
            self.assertEqual(self.request(method=method)[0]['status'], 405)
        self.assertEqual(self.hub.calls, [])

    def test_query_and_encoded_route_rejected(self):
        self.assertEqual(self.request(query=b'url=https://example.com')[0]['status'], 400)
        self.assertEqual(self.request(raw=b'/api/v1/%64ays')[0]['status'], 400)
        self.assertEqual(self.hub.calls, [])

    def test_read_cache_does_not_repeat_worker_reads(self):
        self.request()
        self.request()
        self.assertEqual(self.hub.calls, ['/api/v1/days'])
        self.assertEqual(len(self.app.cache), 1)

    def test_cache_is_bounded(self):
        for day in range(1, 24):
            self.request('/api/v1/stores/3014/days/2026-10-' + str(day).zfill(2))
        self.assertEqual(len(self.app.cache), 16)

    def test_deadline_denies_even_cached_payload(self):
        self.request()
        self.app.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.request()[0]['status'], 503)
        self.assertEqual(self.hub.calls, ['/api/v1/days'])

    def test_response_headers_and_body_are_bounded_projection(self):
        messages = self.request()
        headers = dict(messages[0]['headers'])
        self.assertEqual(headers[b'cache-control'], b'no-store')
        self.assertEqual(headers[b'referrer-policy'], b'no-referrer')
        self.assertIn(b"frame-ancestors 'none'", headers[b'content-security-policy'])
        self.assertEqual(messages[1]['body'], b'{}')

    def test_public_html_does_not_link_to_private_monitor(self):
        self.hub.dispatch = lambda path: (200, '<a href="/monitor">最近观测</a>'.encode(), 'text/html')
        body = self.request('/statistics')[1]['body']
        self.assertNotIn(b'/monitor', body)
        self.assertIn('每30秒自动更新'.encode(), body)


if __name__ == '__main__':
    unittest.main()
