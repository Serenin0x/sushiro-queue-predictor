"""Fixed local workers, per-store status and zero additional upstream work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.monitorhub import HubError, MonitorHub, read_config, serve_hub, validate_config

NOW = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)


def config():
    return {'schema_version': 1, 'deadline_at': (NOW+timedelta(days=2)).isoformat(), 'workers': [
        {'endpoint': 'http://127.0.0.1:10001', 'stores': {'900001': '第一店', '900002': '第二店'}},
        {'endpoint': 'http://127.0.0.1:10002', 'stores': {'900003': '第三店'}}]}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.failures = set()
        self.hub = MonitorHub(config(), reader=self.read, now=NOW)

    def read(self, worker, path):
        self.calls.append((worker['endpoint'], path))
        if worker['endpoint'] in self.failures:
            raise OSError('private_connection_detail')
        if path == '/api/v1/status':
            good = worker['endpoint'].endswith('10002')
            return {'store_ids': list(worker['stores']), 'service_state': 'running' if good else 'failed',
                'worker_alive': good, 'task': {'recorded_http_attempts': 20 if good else 7},
                'network_performed_by_read': False, 'eta_available': False}
        return {'requested_store_id': path.split('/')[4], 'network_performed_by_read': False,
            'eta_available': False, 'retained_points': 5}

    def request(self, path, **options):
        status, raw, mime = self.hub.dispatch(path, now=NOW, **options)
        return status, json.loads(raw) if mime.startswith('application/json') else raw

    def test_expansion_preserves_selected_worker_status_and_budget(self):
        _, first = self.request('/api/v1/status')
        _, other = self.request('/api/v1/stores/900003/status')
        self.assertEqual(first['store_ids'], ['900001', '900002', '900003'])
        self.assertEqual(first['store_names']['900003'], '第三店')
        self.assertEqual(first['service_state'], 'failed')
        self.assertFalse(first['worker_alive'])
        self.assertEqual(first['task']['recorded_http_attempts'], 7)
        self.assertEqual(other['service_state'], 'running')
        self.assertTrue(other['worker_alive'])
        self.assertEqual(other['task']['recorded_http_attempts'], 20)
        self.assertEqual(other['active_batch_store_ids'], ['900003'])
        self.assertFalse(other['upstream_network_performed_by_hub'] or other['collector_started_by_hub'])

    def test_history_and_queue_only_read_the_selected_local_worker(self):
        with patch('socket.socket', side_effect=AssertionError('no_socket_in_fixture')) as sock:
            for kind in ('queue', 'history'):
                code, value = self.request('/api/v1/stores/900003/'+kind)
                self.assertEqual(code, 200)
                self.assertEqual(value['requested_store_id'], '900003')
            self.assertEqual(sock.call_count, 0)
        self.assertEqual(self.calls, [('http://127.0.0.1:10002', '/api/v1/stores/900003/queue'),
            ('http://127.0.0.1:10002', '/api/v1/stores/900003/history')])

    def test_unknown_routes_queries_body_and_methods_never_read_a_worker(self):
        for target, options, expected in [('/api/v1/stores/900004/history', {}, 404),
                ('/api/v1/stores/900001/fusion-context', {}, 404), ('/monitor?x=1', {}, 400),
                ('http://example.com/api/v1/status', {}, 400), ('/api/v1/status', {'body_present': True}, 400),
                ('/api/v1/status', {'method': 'POST'}, 405), ('/monitor/../secret', {}, 404)]:
            self.assertEqual(self.request(target, **options)[0], expected)
        self.assertEqual(self.calls, [])

    def test_deadline_does_not_poll_or_restart_workers(self):
        self.assertEqual(self.hub.dispatch('/api/v1/status', now=NOW+timedelta(days=2))[0], 503)
        self.assertEqual(self.calls, [])

    def test_failed_local_read_is_unknown_and_private_error_is_not_returned(self):
        self.failures.add('http://127.0.0.1:10002')
        status, value = self.request('/api/v1/stores/900003/status')
        self.assertEqual(status, 503)
        self.assertEqual(value['error_code'], 'monitor_hub_unavailable')
        self.assertNotIn('private_connection_detail', json.dumps(value))
        self.assertEqual(self.request('/api/v1/stores/900001/status')[0], 200)

    def test_foreign_store_scope_and_fake_worker_state_rejected(self):
        for change in ({'store_ids': ['900099']}, {'worker_alive': 1}, {'service_state': 'fictional'},
                {'eta_available': True}, {'network_performed_by_read': True}):
            original = self.read(self.hub.config['workers'][0], '/api/v1/status')
            self.hub.reader = lambda w, p: {**original, **change}
            self.assertEqual(self.request('/api/v1/status')[0], 503)
        self.hub.reader = lambda w, p: {'requested_store_id': '900099',
            'eta_available': False, 'network_performed_by_read': False}
        self.assertEqual(self.request('/api/v1/stores/900001/history')[0], 503)

    def test_direct_methods_cannot_construct_arbitrary_routes(self):
        for store, kind in [('900099', 'queue'), ('900001', '../status'), ('900001', 'count?x=1')]:
            with self.assertRaises(HubError):
                self.hub.projection(store, kind)
        with self.assertRaises(HubError):
            self.hub.status('900099')
        self.assertEqual(self.calls, [])

    def test_config_rejects_nonloopback_redirects_duplicate_stores_and_excess_scope(self):
        for endpoint in ['https://crm-cn-prd.sushiro.com.cn', 'http://localhost:1234',
                'http://127.0.0.1:1234/path', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
                'http://user@127.0.0.1:1234', 'http://127.0.0.2:1234']:
            value = config(); value['workers'][0]['endpoint'] = endpoint
            with self.assertRaises(HubError): validate_config(value, now=NOW)
        for alteration in ('duplicate_store','duplicate_worker','four_stores','bool_schema','expired','long_deadline'):
            value = config()
            if alteration == 'duplicate_store': value['workers'][1]['stores'] = {'900001': '重复店'}
            if alteration == 'duplicate_worker': value['workers'][1]['endpoint'] = value['workers'][0]['endpoint']
            if alteration == 'four_stores': value['workers'][0]['stores'] = {str(900010+i): '店' for i in range(4)}
            if alteration == 'bool_schema': value['schema_version'] = True
            if alteration == 'expired': value['deadline_at'] = NOW.isoformat()
            if alteration == 'long_deadline': value['deadline_at'] = (NOW+timedelta(days=15)).isoformat()
            with self.assertRaises(HubError): validate_config(value, now=NOW)

    def test_private_config_permissions_and_no_sensitive_stdout(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); root.chmod(0o700)
            p = root/'workers.json'; p.write_text(json.dumps(config())); p.chmod(0o600)
            with patch('sushiwait.monitorhub._clock', return_value=NOW):
                self.assertEqual(len(read_config(p)['workers']), 2)
                p.chmod(0o644)
                with self.assertRaises(HubError): read_config(p)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(['collection-hub-serve', '--config-file', str(p)]), 1)
            self.assertNotIn(str(root), output.getvalue())
            self.assertNotIn('10001', output.getvalue())

    def test_modified_hub_browser_selects_names_and_each_worker_status(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node must be on PATH')
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'hub.js'; p.write_bytes(self.hub.asset('/monitor.js')[0])
            r = subprocess.run([node, str(Path(__file__).with_name('monitorhub.test.js')), str(p)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_actual_local_http_read_rejects_redirect_and_oversized_response(self):
        from sushiwait.monitorhub import _read_local
        class Response:
            status = 302
            def read(self, limit): raise AssertionError('redirect_body_not_read')
        class Connection:
            def request(self, *a, **k): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch('sushiwait.monitorhub.http.client.HTTPConnection', return_value=Connection()) as client:
            with self.assertRaises(HubError): _read_local(self.hub.config['workers'][0], '/api/v1/status')
            self.assertEqual(client.call_args.args, ('127.0.0.1', 10001))

    def test_local_history_over_small_input_size_is_kept_but_two_mib_is_bounded(self):
        from sushiwait.monitorhub import _read_local, MAX_BODY
        class Response:
            status = 200
            raw = json.dumps({'history': 'x'*20000}).encode()
            def read(self, limit): return self.raw[:limit]
        response = Response()
        class Connection:
            def request(self, *a, **k): pass
            def getresponse(self): return response
            def close(self): pass
        with patch('sushiwait.monitorhub.http.client.HTTPConnection', return_value=Connection()):
            self.assertEqual(len(_read_local(self.hub.config['workers'][0], '/api/v1/status')['history']), 20000)
            response.raw = b'x'*(MAX_BODY+1)
            with self.assertRaises(HubError): _read_local(self.hub.config['workers'][0], '/api/v1/status')
            response.raw = b'{"duplicate":1,"duplicate":2}'
            with self.assertRaises(HubError): _read_local(self.hub.config['workers'][0], '/api/v1/status')

    def test_actual_server_loopback_scope_no_store_query_on_page_and_clean_stop(self):
        started, stop = threading.Event(), threading.Event(); port = []
        current = datetime.now(timezone.utc)
        value = config(); value['deadline_at'] = (current+timedelta(minutes=5)).isoformat()
        def ready(number): port.append(number); started.set()
        errors = []
        def run():
            try: serve_hub(value, port=0, ready=ready, stop=stop)
            except Exception as e: errors.append(str(e)); started.set()
        with patch('sushiwait.monitorhub._read_local', side_effect=self.read):
            thread = threading.Thread(target=run); thread.start()
            try:
                self.assertTrue(started.wait(5)); self.assertFalse(errors)
                client = http.client.HTTPConnection('127.0.0.1', port[0], timeout=3)
                client.request('GET', '/monitor'); response = client.getresponse(); response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
                self.assertEqual(self.calls, [])
                client.close()
                client = http.client.HTTPConnection('127.0.0.1', port[0], timeout=3)
                client.request('GET', '/api/v1/stores/900003/status'); response = client.getresponse()
                self.assertEqual(json.loads(response.read())['task']['recorded_http_attempts'], 20)
                client.close()
            finally:
                stop.set(); thread.join(5)
            self.assertFalse(thread.is_alive()); self.assertFalse(errors)
