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
        self.config = {'schema_version':1,'deadline_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        self.calls = []

    def dispatch(self, path):
        self.calls.append(path)
        if path == '/statistics':
            return 200, b'<div id="sushiwait-month-calendar"></div>', 'text/html'
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
        for path in ['/statistics', '/statistics.js', '/statistics.css', '/reference-model.js', '/reference-alerts.js', '/experience-model.js', '/experience-ui.js', '/api/v1/days',
                     '/api/v1/stores/3014/days/2026-10-09']:
            self.assertEqual(self.request(path)[0]['status'], 200)
        self.assertEqual(len(self.hub.calls), 9)

    def test_no_management_status_ticket_queue_or_file_routes(self):
        for path in ['/api/v1/status', '/monitor', '/api/v1/stores/3014/queue',
                     '/api/v1/stores/3014/status', '/etc/passwd', '/hub.json',
                     '/api/v1/stores/3004/days/2026-10-09']:
            self.assertEqual(self.request(path)[0]['status'], 404)
        self.assertEqual(self.hub.calls, [])

    def test_continuous_readonly_config_exposes_month_but_no_control(self):
        self.hub.config = {'schema_version':2,'mode':'daily_controller_readonly'}
        self.app = gateway.StatisticsGateway(self.hub)
        self.assertIsNone(self.app.deadline)
        self.assertEqual(self.request('/api/v1/months/2026-09')[0]['status'],200)
        self.assertEqual(self.request('/api/v1/months/2026-13')[0]['status'],404)
        self.assertEqual(self.request('/api/v1/stores/3014/status')[0]['status'],404)
        self.assertEqual(self.hub.calls,['/api/v1/months/2026-09'])

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
        self.hub.dispatch = lambda path: (200, '<div id="sushiwait-month-calendar"><a href="/monitor">最近观测</a></div>'.encode(), 'text/html')
        body = self.request('/statistics')[1]['body']
        self.assertNotIn(b'/monitor', body)
        self.assertIn('每30秒自动更新'.encode(), body)

    def test_official_calendar_requires_explicit_view_and_source(self):
        with self.assertRaises(ValueError):
            gateway.StatisticsGateway(self.hub, calendar_source='sapi_miniapp_gateway')
        with self.assertRaises(ValueError):
            gateway.StatisticsGateway(self.hub, calendar_source='other')
        html = b'<div id="sushiwait-month-calendar"></div>'
        reference_html = b'<main id="sushiwait-reference"></main>'
        self.hub.dispatch = lambda path: (200, reference_html if path == '/statistics-legacy' else html, 'text/html')
        # Configuring the optional API alone must not switch the calendar.
        self.app = gateway.StatisticsGateway(self.hub, official_view=object())
        self.assertNotIn(b'data-statistics-source=', self.request('/statistics')[1]['body'])
        self.assertIn(b'data-statistics-poll-seconds="30"', self.request('/statistics')[1]['body'])
        self.app = gateway.StatisticsGateway(self.hub, official_view=object(),
            calendar_source='sapi_miniapp_gateway')
        self.assertIn(b'data-statistics-source="sapi_miniapp_gateway"',
            self.request('/statistics')[1]['body'])
        reference_body = self.request('/statistics-legacy')[1]['body']
        self.assertIn(b'id="sushiwait-reference"', reference_body)
        self.assertIn(b'data-statistics-source="sapi_miniapp_gateway"', reference_body)
        self.assertIn(b'data-statistics-history="combined"', reference_body)

    def test_official_reference_rejects_unbound_or_ambiguous_html(self):
        for html in (b'{}', b'<main id="sushiwait-reference"></main>' * 2,
                     b'<main id="sushiwait-reference" data-statistics-source="other"></main>',
                     b'<div id="sushiwait-month-calendar"></div>'):
            with self.subTest(html=html):
                self.hub.dispatch = lambda path: (200, html, 'text/html')
                self.app = gateway.StatisticsGateway(self.hub, official_view=object(),
                    calendar_source='sapi_miniapp_gateway')
                self.assertEqual(self.request('/statistics-legacy')[0]['status'], 503)

    def test_reference_default_still_uses_explicit_legacy_source(self):
        html = b'<main id="sushiwait-reference"></main>'
        self.hub.dispatch = lambda path: (200, html, 'text/html')
        body = self.request('/statistics-legacy')[1]['body']
        self.assertIn(b'id="sushiwait-reference"', body)
        self.assertIn(b'data-statistics-poll-seconds="30"', body)
        self.assertNotIn(b'data-statistics-source=', body)
        self.assertNotIn(b'data-statistics-history=', body)

    def test_official_calendar_rejects_missing_or_ambiguous_binding(self):
        for html in (b'{}', b'<div id="sushiwait-month-calendar"></div>' * 2,
                     b'<div id="sushiwait-month-calendar" data-statistics-source="other"></div>'):
            with self.subTest(html=html):
                self.hub.dispatch = lambda path: (200, html, 'text/html')
                self.app = gateway.StatisticsGateway(self.hub, official_view=object(),
                    calendar_source='sapi_miniapp_gateway')
                self.assertEqual(self.request('/statistics')[0]['status'], 503)
                self.assertEqual(self.hub.calls, [])

    def test_earlier_bookmark_uses_the_same_combined_calendar(self):
        html = b'<div id="sushiwait-month-calendar"></div>'
        self.hub.dispatch = lambda path: (200, html, 'text/html')
        self.app = gateway.StatisticsGateway(self.hub, official_view=object(),
            calendar_source='sapi_miniapp_gateway', refresh_seconds=600)
        messages = self.request('/statistics-history')
        self.assertEqual(messages[0]['status'], 200)
        body = messages[1]['body']
        self.assertIn(b'data-statistics-source="sapi_miniapp_gateway"', body)
        self.assertIn(b'data-statistics-history="combined"', body)
        self.assertIn(b'data-statistics-poll-seconds="600"', body)
        self.assertEqual(body,self.request('/statistics')[1]['body'])

    def test_policy_changes_visible_without_restart_and_no_private_fields(self):
        import json, os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o700)
            path = Path(folder).resolve()/'policy.json'
            def write(interval):
                path.write_text(json.dumps({'schema_version':1,'interval_seconds':interval,'ordinary_query_limit':396}))
                os.chmod(path, 0o600)
            write(600)
            self.app = gateway.StatisticsGateway(self.hub, refresh_policy_file=path)
            self.assertEqual(json.loads(self.request('/api/v1/statistics/settings')[1]['body']),
                {'schema_version':1,'poll_seconds':600})
            write(300)
            self.app.cache.clear()
            self.assertEqual(json.loads(self.request('/api/v1/statistics/settings')[1]['body'])['poll_seconds'], 300)
            path.write_text('{"interval_seconds":true,"authorization":"not permitted"}')
            self.app.cache.clear()
            self.assertEqual(self.request('/api/v1/statistics/settings')[0]['status'],503)
            self.assertEqual(self.request('/statistics')[0]['status'],503)
        self.assertEqual(self.hub.calls, ['/statistics'])

    def test_smooth_policy_reports_current_delay_without_origin_reads(self):
        import json, tempfile
        from unittest.mock import patch
        from sushiwait.cadence import CHINA
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder).resolve()/'policy.json'
            path.write_text(json.dumps({'schema_version':2,'interval_seconds':600,
                'ordinary_query_limit':396,'cadence':'meal-peaks-v1'}))
            path.chmod(0o600)
            self.app=gateway.StatisticsGateway(self.hub,refresh_policy_file=path)
            for hour,minute,expected in ((12,0,180),(15,0,600),(11,15,None)):
                now=datetime(2026,10,13,hour,minute,tzinfo=CHINA)
                value=self.app.polling_settings(now=now)
                self.assertEqual(value['cadence_policy']['peak_seconds'],180)
                if expected is None:
                    self.assertTrue(180<value['poll_seconds']<600)
                else:
                    self.assertEqual(value['poll_seconds'],expected)
            fixed=self.app.polling_settings(now=datetime(2026,10,13,12,tzinfo=CHINA))
            with patch.object(self.app,'polling_settings',return_value=fixed):
                response=self.request('/api/v1/statistics/settings')
            self.assertEqual(response[0]['status'],200)
            self.assertEqual(json.loads(response[1]['body']),fixed)
        self.assertEqual(self.hub.calls,[])

    def test_smooth_policy_rejects_invalid_limits_modes_and_extra_fields(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder).resolve()/'policy.json'
            self.app=gateway.StatisticsGateway(self.hub,refresh_policy_file=path)
            base={'schema_version':2,'interval_seconds':600,
                'ordinary_query_limit':1008,'cadence':'meal-peaks-v1'}
            path.write_text(json.dumps(base));path.chmod(0o600)
            self.assertEqual(self.request('/api/v1/statistics/settings')[0]['status'],200)
            for changes in ({'ordinary_query_limit':True},{'ordinary_query_limit':1009},
                            {'interval_seconds':180},{'schema_version':1},
                            {'cadence':'unknown'},{'authorization':'private'}):
                path.write_text(json.dumps({**base,**changes}));self.app.cache.clear()
                self.assertEqual(self.request('/api/v1/statistics/settings')[0]['status'],503)
        self.assertEqual(self.hub.calls,[])


if __name__ == '__main__':
    unittest.main()
