"""Synthetic committed projections: gaps, failures, bounded history and reads."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from importlib.resources import files
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.remoteservice import LiveRemoteView, MONITOR_POINTS, RemoteASGI, RemoteQueueService
import test_remote_service as helpers


def record(second=0, labels=('10', '11'), count=5, fail=None):
    with patch('sushiwait.remote._utc', return_value=(helpers.BASE+timedelta(seconds=second)).isoformat()):
        value = helpers.RemoteClient(opener=helpers.Opener(fail)).snapshot('900001')
    if value['queries']['groupqueues']['ok']:
        for name in helpers.QUEUE_NAMES:
            value['queries']['groupqueues']['payload']['queues'][name] = list(labels)
    if value['queries']['storequeuecount']['ok']:
        value['queries']['storequeuecount']['payload']['raw_count'] = count
    return value


async def raw_request(app, path, *, method='GET', query=b'', body=b''):
    output = []
    async def receive(): return {'type': 'http.request', 'body': body, 'more_body': False}
    async def send(value): output.append(value)
    await app({'type': 'http', 'path': path, 'method': method, 'query_string': query}, receive, send)
    return output[0]['status'], output[1]['body'], dict(output[0]['headers'])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.view = LiveRemoteView(['900001', '900002'], stale_after_seconds=120)

    def history(self, store='900001', **kw):
        return self.view.monitor_history(store, now=helpers.BASE+timedelta(hours=8),
            service_state=kw.get('state', 'running'), worker_alive=kw.get('alive', True))

    def test_empty_history_is_unavailable_and_does_not_fabricate_zero(self):
        value = self.history()
        self.assertEqual(value['points'], [])
        self.assertFalse(value['complete_history'] or value['eta_available'] or value['network_performed_by_read'])
        self.assertEqual(value['count_unit'], 'unknown')

    def test_distinct_set_removals_are_not_number_distance_or_no_show_rate(self):
        self.view.publish(record(labels=('10', '10', '11-1')))
        self.view.publish(record(30, labels=('5000',)))
        point = self.history()['points'][1]
        self.assertEqual(point['display_comparison']['removed_labels']['storeQueue'], 2)
        self.assertEqual(point['display_comparison']['interval_seconds'], 30)
        self.assertEqual(point['display_sizes']['storeQueue'], 1)
        self.assertFalse(self.history()['display_turnover_is_no_show_rate'])

    def test_failed_pair_has_null_counts_and_breaks_comparison(self):
        self.view.publish(record())
        self.view.publish(record(30, fail='groupqueues?'))
        self.view.publish(record(60, labels=('12',)))
        points = self.history()['points']
        self.assertIsNone(points[1]['reported_count_raw'])
        self.assertIsNone(points[1]['display_sizes'])
        self.assertFalse(points[1]['queries']['storequeuecount']['attempted'])
        self.assertEqual(points[2]['display_comparison']['state'], 'insufficient')
        self.assertIsNone(points[2]['display_comparison']['removed_labels'])

    def test_partial_pair_keeps_new_queue_metrics_without_old_count(self):
        self.view.publish(record())
        self.view.publish(record(30, labels=('12',), fail='storequeuecount?'))
        point = self.history()['points'][1]
        self.assertIsNone(point['reported_count_raw'])
        self.assertEqual(point['display_sizes']['storeQueue'], 1)
        self.assertEqual(point['display_comparison']['removed_labels']['storeQueue'], 2)

    def test_long_gap_is_measured_but_no_turnover_is_inferred(self):
        self.view.publish(record())
        self.view.publish(record(121, labels=('12',)))
        value = self.history()['points'][1]['display_comparison']
        self.assertEqual(value['state'], 'gap')
        self.assertEqual(value['interval_seconds'], 121)
        self.assertIsNone(value['removed_labels'])

    def test_exact_gap_boundary_is_comparable(self):
        self.view.publish(record())
        self.view.publish(record(120, labels=('12',)))
        self.assertEqual(self.history()['points'][1]['display_comparison']['state'], 'comparable_display_sets')

    def test_endpoint_timestamps_are_retained_independently(self):
        value = record()
        for key in ('started_at', 'received_at'):
            value['queries']['storequeuecount'][key] = (helpers.BASE+timedelta(seconds=10)).isoformat()
        self.view.publish(value)
        queries = self.history()['points'][0]['queries']
        self.assertNotEqual(queries['groupqueues']['received_at'], queries['storequeuecount']['received_at'])

    def test_duplicate_publish_does_not_append_another_observation(self):
        value = record()
        self.view.publish(value)
        self.view.publish(deepcopy(value))
        self.assertEqual(self.history()['retained_points'], 1)

    def test_projection_is_bounded_and_marks_eviction(self):
        for i in range(MONITOR_POINTS+5): self.view.publish(record(i*30, count=i))
        value = self.history()
        self.assertEqual(value['retained_points'], MONITOR_POINTS)
        self.assertEqual(value['evicted_points_this_process'], 5)
        self.assertEqual(value['points'][0]['reported_count_raw'], 5)
        self.assertEqual(value['points'][-1]['reported_count_raw'], MONITOR_POINTS+4)

    def test_saved_history_and_stopped_worker_are_explicit(self):
        self.view.publish(record(), saved_history=True)
        value = self.history(state='completed', alive=False)
        self.assertEqual(value['points'][0]['origin'], 'saved_history')
        self.assertEqual(value['service_state'], 'completed')
        self.assertFalse(value['worker_alive'])

    def test_reads_are_independent_copies_without_private_or_transport_fields(self):
        value = record();value['private_secret'] = 'private-marker'
        value['queries']['groupqueues']['private_secret'] = 'private-marker'
        self.view.publish(value)
        first = self.history();first['points'][0]['queries'].clear()
        self.assertIn('groupqueues', self.history()['points'][0]['queries'])
        self.assertNotIn('private-marker', json.dumps(self.history()))
        self.assertNotIn('transport', json.dumps(self.history()))

    def test_rejected_record_does_not_change_history(self):
        self.view.publish(record());before = self.history()
        bad = record(30);bad['queries']['groupqueues']['payload']['queues']['storeQueue'] = ['<unsafe>']
        with self.assertRaises(ValueError): self.view.publish(bad)
        self.assertEqual(before, self.history())

    def test_store_projection_is_isolated(self):
        self.view.publish(record())
        self.assertEqual(self.history('900002')['points'], [])
        with self.assertRaises(ValueError): self.history('900003')

    def test_assets_and_api_do_not_open_db_or_request_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RemoteQueueService(db=Path(directory)/'never-created.sqlite3',
                task_file=Path(directory)/'never-created.json', store_ids=['900001'])
            service.view.publish(record())
            app = RemoteASGI(service)
            # Create the loop before blocking asyncio's own local socket pair.
            loop = asyncio.new_event_loop()
            try:
                with patch('socket.socket', side_effect=AssertionError('network')), \
                        patch('sqlite3.connect', side_effect=AssertionError('database')), \
                        patch('sushiwait.remoteservice.RemoteClient', side_effect=AssertionError('upstream')):
                    for path, content_type in [('/monitor', b'text/html'), ('/monitor.js', b'text/javascript'), ('/monitor.css', b'text/css')]:
                        status, body, headers = loop.run_until_complete(raw_request(app, path))
                        self.assertEqual(status, 200);self.assertTrue(headers[b'content-type'].startswith(content_type))
                        self.assertIn(b"connect-src 'self'", headers[b'content-security-policy'])
                        self.assertNotIn(b'http://', body);self.assertNotIn(b'https://', body)
                    status, body, headers = loop.run_until_complete(raw_request(app, '/api/v1/stores/900001/history'))
                    value = json.loads(body)
                    self.assertEqual(status, 200);self.assertEqual(value['retained_points'], 1)
                    self.assertFalse(value['network_performed_by_read'])
                    self.assertNotIn(directory, body.decode())
                    self.assertEqual(headers[b'cache-control'], b'no-store')
            finally: loop.close()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_monitor_routes_keep_method_body_scope_and_query_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            app = RemoteASGI(RemoteQueueService(db=Path(directory)/'db',task_file=Path(directory)/'task',store_ids=['900001']))
            for path, kw, expected in [('/monitor', {'method':'POST'},405),('/monitor',{'query':b'x=1'},400),
                    ('/monitor',{'body':b'unsafe'},400),('/api/v1/stores/900002/history',{},404),('/monitor/../secret',{},404)]:
                self.assertEqual(asyncio.run(raw_request(app,path,**kw))[0],expected)

    def test_browser_behavior_with_saved_synthetic_data(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node must be on PATH for browser behavior checks')
        result = subprocess.run([node,str(Path(__file__).with_name('monitor.test.js')),
            str(files('sushiwait').joinpath('web','monitor.js'))],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

