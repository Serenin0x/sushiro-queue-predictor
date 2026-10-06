"""Whole terminal-window evidence, zero network and preserved private files."""
from datetime import datetime, timedelta, timezone
import io, json, os, sqlite3, unittest
from unittest.mock import patch

import test_remote_window as fixtures
from sushiwait.cli import main
from sushiwait.remote import RemoteStore
from sushiwait.remotequality import window_quality_report
from sushiwait.remotetasks import RemoteTaskError


class RemoteQualityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RemoteWindowTests()
        self.f.setUp()
        self.network = patch('socket.socket', side_effect=AssertionError('network'))
        self.socket = self.network.start()
        self.credentials = patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials'))
        self.auth = self.credentials.start()

    def tearDown(self):
        self.assertEqual((self.socket.call_count, self.auth.call_count), (0, 0))
        self.credentials.stop(); self.network.stop(); self.f.tearDown()

    def report(self, gap=90, as_of=None):
        with RemoteStore(self.f.db, read_only=True) as db:
            return window_quality_report(db, self.f.path,
                as_of=(as_of or self.f.clock.wall()).isoformat(), max_gap_seconds=gap)

    def test_complete_deadline_window_preserves_files_and_reports_full_chain(self):
        self.f.collect()
        before = [p.read_bytes() for p in (self.f.db, self.f.path, self.f.plan)]
        result = self.report()
        store = result['stores'][0]
        self.assertEqual((result['rows_examined'], result['recorded_http_attempts']), (3, 6))
        self.assertTrue(result['deadline_checkpoint_reached'])
        self.assertEqual(store['request_span_seconds'], 120)
        self.assertEqual(store['successful_request_gap_seconds']['end_boundary'], 60)
        self.assertEqual(store['days']['2026-10-06']['hour_counts'], {'20': 3})
        self.assertEqual([p.read_bytes() for p in (self.f.db, self.f.path, self.f.plan)], before)
        self.assertNotIn(str(self.f.parent), json.dumps(result))
        self.assertNotIn('payload', json.dumps(result))
        self.assertFalse(result['continuous_collection_verified'])
        self.assertEqual((result['source_freshness'], result['verified_training_labels']), ('unknown', 0))

    def test_explicit_threshold_counts_internal_and_tail_gaps_without_rate_guess(self):
        self.f.collect(); result = self.report(gap=59)['stores'][0]
        self.assertEqual(result['successful_request_gap_seconds']['internal_gaps_above_threshold'], 2)
        self.assertTrue(result['successful_request_gap_seconds']['end_boundary_above_threshold'])
        self.assertEqual(result['long_gap_boundaries'], 2)

    def test_budget_completion_is_not_a_completed_deadline_window(self):
        self.f.collect(self.f.config(pairs=2)); self.f.clock.seconds = 180
        result = self.report(gap=60)
        self.assertEqual(result['end_reason'], 'budget')
        self.assertFalse(result['deadline_checkpoint_reached'])
        self.assertEqual(result['stores'][0]['successful_request_gap_seconds']['end_boundary'], 120)

    def test_queue_failure_retains_one_http_attempt_and_absent_successful_span(self):
        self.f.opener.fail = True; self.f.collect(); self.f.clock.seconds = 180
        result = self.report()
        self.assertEqual((result['failed_pairs'], result['recorded_http_attempts']), (1, 1))
        self.assertEqual(result['checkpoint_state'], 'failed')
        self.assertEqual(result['stores'][0]['successful_request_gap_seconds']['start_boundary'], 180)

    def test_count_failure_keeps_both_attempts_and_does_not_count_a_success(self):
        original = self.f.opener.open
        def failed_count(request, *, timeout):
            result = original(request, timeout=timeout)
            if 'storequeuecount?' in request.full_url: result.status = 503
            return result
        self.f.opener.open = failed_count; self.f.collect()
        result = self.report()
        self.assertEqual((result['successful_pairs'], result['failed_pairs'], result['recorded_http_attempts']), (0, 1, 2))

    def test_three_stores_keep_independent_daily_counts_and_intervals(self):
        self.f.collect(self.f.config(stores=('900001', '900002', '900003')))
        result = self.report()
        self.assertEqual((result['rows_examined'], result['recorded_http_attempts']), (9, 18))
        for store in result['stores']:
            self.assertEqual(store['successful_pairs'], 3)
            self.assertEqual(store['request_interval_seconds']['maximum'], 60)
            self.assertEqual(store['days']['2026-10-06']['successful_pairs'], 3)

    def test_array_and_count_changes_are_aggregated_without_raw_values(self):
        original = self.f.opener.open
        def changing(request, *, timeout):
            response = original(request, timeout=timeout)
            sample = (len(self.f.opener.calls)+1)//2
            payload = ({name:[str(777777+sample)] for name in fixtures.QUEUE_NAMES}
                if 'groupqueues?' in request.full_url else 918273+sample)
            response.read = lambda size:json.dumps(payload).encode()[:size]
            return response
        self.f.opener.open = changing; self.f.collect()
        result = self.report(); store = result['stores'][0]
        self.assertEqual(list(store['queue_changes'].values()), [2]*5)
        self.assertEqual(store['count_changes'], 2)
        self.assertNotIn('777778', json.dumps(result)); self.assertNotIn('918274', json.dumps(result))

    def test_lost_pending_result_stays_unknown_and_is_not_assigned_to_a_store(self):
        config = self.f.pending(saved=False)
        self.f.clock.seconds = 30; self.f.collect(config, resume=True)
        result = self.report()
        self.assertEqual(result['uncertain_pair_slots'], 1)
        self.assertEqual(result['unrecorded_http_attempts'], 'unknown')
        self.assertEqual(result['unknown_slots_by_store'], 'unknown')

    def test_live_checkpoint_cli_rejected_before_database_is_opened(self):
        self.f.collect(stop=lambda:len(self.f.opener.calls) >= 2)
        before = self.f.path.read_bytes()
        with patch('sushiwait.remote.RemoteStore') as db, patch('sys.stdout', new_callable=io.StringIO) as out:
            code = main(['remote-window-quality', '--db', str(self.f.db), '--task-file', str(self.f.path),
                         '--as-of', self.f.clock.wall().isoformat()])
        self.assertEqual((code, db.call_count), (2, 0))
        self.assertEqual(json.loads(out.getvalue())['error_code'], 'remote_quality_not_terminal')
        self.assertEqual(self.f.path.read_bytes(), before)

    def test_future_checkpoint_is_not_used_at_an_earlier_as_of(self):
        self.f.collect()
        with self.assertRaisesRegex(RemoteTaskError, 'future_checkpoint'):
            self.report(as_of=self.f.clock.wall()-timedelta(seconds=1))

    def test_copied_database_cannot_borrow_original_identity(self):
        self.f.collect(); copied = self.f.parent/'copied.sqlite3'
        copied.write_bytes(self.f.db.read_bytes()); copied.chmod(0o600)
        with RemoteStore(copied, read_only=True) as db:
            with self.assertRaisesRegex(RemoteTaskError, 'database_conflict'):
                window_quality_report(db, self.f.path, as_of=self.f.clock.wall().isoformat())

    def test_tampered_committed_record_rejected(self):
        self.f.collect()
        connection = sqlite3.connect(self.f.db)
        raw = json.loads(connection.execute('SELECT payload_json FROM remote_samples WHERE id=1').fetchone()[0])
        raw['queries']['storequeuecount']['payload']['raw_count'] = 918273
        connection.execute('UPDATE remote_samples SET payload_json=? WHERE id=1', (json.dumps(raw),))
        connection.commit(); connection.close()
        with self.assertRaisesRegex(RemoteTaskError, 'checkpoint_conflict'):
            self.report()

    def test_oversized_identifiers_rejected_by_bounded_projection(self):
        self.f.collect()
        for column in ('run_id', 'store_id'):
            connection = sqlite3.connect(self.f.db)
            previous = connection.execute('SELECT '+column+' FROM remote_samples WHERE id=1').fetchone()[0]
            connection.execute('UPDATE remote_samples SET '+column+'=? WHERE id=1', ('x'*100000,))
            connection.commit(); connection.close()
            with self.subTest(column=column), self.assertRaisesRegex(RemoteTaskError, 'record_conflict'):
                self.report()
            connection = sqlite3.connect(self.f.db)
            connection.execute('UPDATE remote_samples SET '+column+'=? WHERE id=1', (previous,))
            connection.commit(); connection.close()

    def test_extra_record_after_terminal_checkpoint_rejected(self):
        self.f.collect(); self.f.clock.seconds = 179
        with RemoteStore(self.f.db) as db: db.append(self.f.client.snapshot('900001'))
        self.f.clock.seconds = 180
        with self.assertRaises(RemoteTaskError): self.report()

    def test_checkpoint_change_during_report_rejected(self):
        self.f.collect(); raw = self.f.path.read_bytes()
        with patch('sushiwait.remotequality._read_private_file', side_effect=[raw, raw+b' ']):
            with self.assertRaisesRegex(RemoteTaskError, 'checkpoint_changed'): self.report()

    def test_writer_connection_cannot_be_used(self):
        self.f.collect()
        with RemoteStore(self.f.db) as db:
            with self.assertRaisesRegex(RemoteTaskError, 'requires_read_only'):
                window_quality_report(db, self.f.path, as_of=self.f.clock.wall().isoformat())

    def test_invalid_gap_bound_rejected(self):
        self.f.collect()
        for gap in (0, -1, 7201, True, 1.5):
            with self.subTest(gap=gap), self.assertRaisesRegex(RemoteTaskError, 'invalid_gap_bound'):
                self.report(gap=gap)

    def test_rows_before_initial_checkpoint_are_not_counted(self):
        with RemoteStore(self.f.db) as db: db.append(self.f.client.snapshot('900001'))
        self.f.collect(); result = self.report()
        self.assertEqual((result['rows_examined'], result['recorded_http_attempts']), (3, 6))

    def test_restarted_run_breaks_queue_comparison_and_preserves_observed_gap(self):
        config = self.f.config()
        self.f.collect(config, stop=lambda:len(self.f.opener.calls) >= 2)
        self.f.clock.seconds = 60
        self.f.collect(config, resume=True)
        result = self.report()['stores'][0]
        self.assertEqual(result['run_boundaries'], 1)
        self.assertEqual(result['request_interval_seconds']['maximum'], 120)
        self.assertEqual(result['long_gap_boundaries'], 1)

    def test_asia_shanghai_midnight_has_separate_dates_and_hours(self):
        start = datetime(2026, 10, 6, 15, 59, tzinfo=timezone.utc)
        self.f.clock.wall = lambda:start+timedelta(seconds=self.f.clock.seconds)
        self.f.collect(); days = self.report()['stores'][0]['days']
        self.assertEqual(list(days), ['2026-10-06', '2026-10-07'])
        self.assertEqual(days['2026-10-06']['hour_counts'], {'23': 1})
        self.assertEqual(days['2026-10-07']['hour_counts'], {'0': 2})

    def test_inflight_pair_can_complete_after_deadline(self):
        original = self.f.opener.open
        def delayed(request, *, timeout):
            response = original(request, timeout=timeout)
            if 'storequeuecount?' in request.full_url: self.f.clock.seconds += 45
            return response
        self.f.opener.open = delayed
        self.f.collect(self.f.config(duration=30))
        result = self.report()
        self.assertEqual(result['analysis_interval_seconds'], 30)
        self.assertEqual(result['stores'][0]['responses_completed_after_deadline'], 1)

    def test_database_metadata_change_during_read_rejected(self):
        self.f.collect()
        from sushiwait.calendar import date_features
        def touched(*args, **kw):
            stamp = os.stat(self.f.db).st_mtime_ns
            os.utime(self.f.db, ns=(stamp, stamp+1))
            return date_features(*args, **kw)
        with patch('sushiwait.remotequality.date_features', side_effect=touched):
            with self.assertRaisesRegex(RemoteTaskError, 'database_changed'): self.report()


if __name__ == '__main__': unittest.main()
