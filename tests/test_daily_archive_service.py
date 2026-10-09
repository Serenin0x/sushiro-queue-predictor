"""Monthly archive reads and live ASGI copies never start origin queries."""
import asyncio
from datetime import timedelta
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.dailyarchive import read_day, read_month, selected_month
from sushiwait.dailycontroller import DailyController, daily_controller_status
from sushiwait.dailyservice import DailyCollectorService
from sushiwait.monitorhub import MonitorHub, HubError, validate_config
from sushiwait.remote import RemoteClient
from sushiwait.remoteservice import RemoteASGI
from sushiwait.remotetasks import RemoteTaskError
import test_daily_controller as fixture
from test_remote_service import request, wait_for


class DailyArchiveServiceTests(unittest.TestCase):
    setUp = fixture.DailyControllerTests.setUp
    tearDown = fixture.DailyControllerTests.tearDown
    config = fixture.DailyControllerTests.config
    tick = fixture.DailyControllerTests.tick

    def service(self, **kwargs):
        return DailyCollectorService(root=self.root, store_id='900001', business_hours=self.hours,
            not_before=fixture.BASE.isoformat(), wall_clock=self.clock.wall, monotonic_clock=self.clock.mono,
            client_factory=lambda: RemoteClient(opener=self.opener), **kwargs)

    def fill(self, days=1):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            for i in range(days):
                self.clock.seconds = 86400*i
                self.tick(controller)

    def test_full_month_reads_seventeen_dates_without_database_or_network(self):
        self.fill(17); before = len(self.opener.calls)
        with patch('socket.socket', side_effect=AssertionError('no network')), patch('sqlite3.connect', side_effect=AssertionError('no database')):
            value = read_month(self.root, '900001', '2026-10', now=self.clock.wall())
            self.assertEqual(len(value['days']), 17)
            self.assertEqual(value['unavailable_archive_dates'], [])
            self.assertEqual(len(value['missing_archive_dates']), 14)
            for _ in range(2):
                self.assertEqual(read_day(self.root, '900001', '2026-10-09')['summary']['successful_pairs'], 2)
        self.assertEqual(len(self.opener.calls), before)

    def test_modified_projection_is_unavailable_not_missing_or_zero(self):
        self.fill()
        path = self.root/'2026-10-09/projection.json'; value = json.loads(path.read_text())
        value['summary']['successful_pairs'] = 999
        path.write_text(json.dumps(value))
        result = read_month(self.root, '900001', '2026-10', now=self.clock.wall())
        self.assertEqual(result['days'], [])
        self.assertEqual(result['unavailable_archive_dates'], ['2026-10-09'])
        self.assertNotIn('2026-10-09', result['missing_archive_dates'])

    def test_summary_cache_avoids_reparse_but_changed_file_is_rechecked(self):
        self.fill(17);service=self.service()
        service.daily_index('900001','2026-10')
        with patch('sushiwait.dailyarchive.read_daily_archive',side_effect=AssertionError('cached archive reparsed')):
            self.assertEqual(len(service.daily_index('900001','2026-10')['days']),17)
        self.assertEqual(len(service.archive_cache),17)
        (self.root/'2026-10-09/projection.json').write_text('{}')
        result=service.daily_index('900001','2026-10')
        self.assertEqual(result['unavailable_archive_dates'],['2026-10-09'])

    def test_manifest_without_projection_is_corrupt_not_an_unrecorded_day(self):
        self.fill();(self.root/'2026-10-09/projection.json').unlink()
        result=read_month(self.root,'900001','2026-10',now=self.clock.wall())
        self.assertEqual(result['unavailable_archive_dates'],['2026-10-09'])

    def test_foreign_scope_and_invalid_month_are_rejected(self):
        self.fill()
        with self.assertRaises(RemoteTaskError): read_day(self.root, '900002', '2026-10-09')
        for month in ['2026-00', '2026-13', '../2026-10', '2026-1', '2026-10?x=1']:
            with self.assertRaises(RemoteTaskError): selected_month(month)

    def test_offline_status_does_not_lock_or_mutate_writer_files(self):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            before = (self.root/'controller.json').read_bytes()
            value = daily_controller_status(self.root)
            self.assertEqual(value['process_liveness'], 'unknown')
            self.assertEqual((self.root/'controller.json').read_bytes(), before)
        self.assertEqual(self.opener.calls, [])

    def test_reading_historical_detail_and_month_via_asgi_adds_no_origin_work(self):
        self.fill(17); service = self.service(); before = len(self.opener.calls)
        for path in ['/api/v1/days', '/api/v1/months/2026-10', '/api/v1/stores/900001/months/2026-10',
                     '/api/v1/stores/900001/days/2026-10-09']:
            status, value, _ = asyncio.run(request(RemoteASGI(service), path))
            self.assertEqual(status, 200)
            self.assertFalse(value['network_performed_by_read'])
        self.assertEqual(len(self.opener.calls), before)
        self.assertIsNone(service.thread)

    def test_worker_publishes_live_day_then_archives_before_waiting(self):
        service = None
        def wait(seconds, stop):
            if self.clock.seconds >= 120:
                stop.set()
            else:
                self.clock.sleep(seconds)
        service = self.service(wait=wait)
        try:
            service.start(); wait_for(lambda: not service.thread.is_alive())
            status = service.status()
            self.assertEqual(status['service_state'], 'stopped')
            self.assertTrue(status['bounded_daily_tasks'])
            self.assertFalse(status['bounded_task'])
            self.assertEqual(status['task']['last_finished_date'], '2026-10-09')
            self.assertEqual(service.daily_detail('900001', '2026-10-09')['summary']['successful_pairs'], 2)
            self.assertEqual(len(self.opener.calls), 4)
            self.assertTrue((self.root/'2026-10-09/result.json').is_file())
        finally:
            service.shutdown()

    def test_unavailable_archives_are_not_hidden_by_service(self):
        self.fill(); service = self.service()
        (self.root/'2026-10-09/projection.json').write_text('{}')
        result = service.daily_batch_index('2026-10')
        self.assertEqual(result['unavailable_store_ids'], ['900001'])
        status, value, _ = asyncio.run(request(RemoteASGI(service), '/api/v1/stores/900001/days/2026-10-09'))
        self.assertEqual(status, 503)

    def test_missing_day_is_not_zero_called_and_scope_remains_local(self):
        self.fill(); service = self.service()
        status, value, _ = asyncio.run(request(RemoteASGI(service), '/api/v1/stores/900001/days/2026-10-08'))
        self.assertEqual(status, 200); self.assertIsNone(value['summary']); self.assertIsNone(value['actual_called_count'])
        self.assertEqual(asyncio.run(request(RemoteASGI(service), '/api/v1/stores/900002/months/2026-10'))[0], 404)

    def test_continuous_hub_has_explicit_new_schema_and_old_trial_still_expires(self):
        cfg = {'schema_version': 2, 'mode': 'daily_controller_readonly', 'workers': [
            {'endpoint': 'http://127.0.0.1:18821', 'stores': {'900001': '第一店'}}]}
        self.assertEqual(validate_config(cfg)['schema_version'], 2)
        self.fill(17); service = self.service()
        calls = []
        def reader(worker, path):
            calls.append(path)
            return service.daily_index('900001', path.rsplit('/',1)[1] if '/months/' in path else None)
        hub = MonitorHub(cfg, reader=reader, now=self.clock.wall())
        code, body, _ = hub.dispatch('/api/v1/months/2026-10', now=self.clock.wall()+timedelta(days=30))
        self.assertEqual(code, 200); self.assertEqual(len(json.loads(body)['days']), 17)
        self.assertEqual(calls, ['/api/v1/stores/900001/months/2026-10'])
        from test_monitor_hub import config, NOW
        old = MonitorHub(config(), reader=lambda *_: self.fail('expired trial polled'), now=NOW)
        self.assertEqual(old.dispatch('/api/v1/days', now=NOW+timedelta(days=2))[0], 503)
        for altered in [{**cfg, 'deadline_at': '2099-01-01T00:00:00Z'}, {**cfg, 'mode': 'other'}]:
            with self.assertRaises(HubError): validate_config(altered)

    def test_cli_status_and_month_are_offline_and_safe(self):
        self.fill(); before = len(self.opener.calls)
        for argv in [['remote-daily-status', '--root', str(self.root)],
                     ['daily-archive-month','--root',str(self.root),'--store-id','900001','--month','2026-10']]:
            out = io.StringIO()
            with patch('sys.stdout',out): self.assertEqual(main(argv), 0)
            value = json.loads(out.getvalue())
            self.assertFalse(value['eta_available'])
            self.assertNotIn(str(self.root), out.getvalue())
        self.assertEqual(len(self.opener.calls), before)
