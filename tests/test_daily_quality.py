"""Receipt timing, unfinished bins and immutable offline daily diagnostics."""
from copy import deepcopy
from datetime import timedelta
import io
import json
import unittest
from unittest.mock import patch

import test_daily_controller as fixture
from sushiwait.cli import main
from sushiwait.dailycontroller import DailyController
from sushiwait.dailyquality import daily_quality_report
from sushiwait.remotetasks import RemoteTaskError


class DailyQualityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.DailyControllerTests()
        self.f.setUp()
        with DailyController(config=self.f.config(), now=self.f.clock.wall()) as controller:
            self.f.tick(controller)
        self.projection = self.f.archive(name='projection.json')

    def tearDown(self):
        self.f.tearDown()

    def stamp(self, seconds):
        return (fixture.BASE + timedelta(seconds=seconds)).isoformat()

    def report(self, projection=None, **kwargs):
        value = self.projection if projection is None else projection
        return daily_quality_report(value, store_id='900001', day='2026-10-09',
            as_of=kwargs.pop('as_of', value['generated_at']), **kwargs)

    def test_complete_two_minute_day_and_offline_safe_output(self):
        before = deepcopy(self.projection)
        with patch('socket.socket', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('database')):
            report = self.report()
        self.assertEqual(report['quality_state'], 'declared_day_observed')
        self.assertEqual(report['coverage']['completed_bin_receipt_fraction'], 1)
        self.assertEqual(report['queue_receipt_intervals']['maximum_seconds'], 60)
        self.assertEqual(self.projection, before)
        self.assertIsNone(report['actual_called_count']); self.assertIsNone(report['no_show_rate'])
        self.assertFalse(report['eta_available']); self.assertFalse(report['network_performed_by_report'])
        self.assertNotIn('queues', report); self.assertNotIn(str(self.f.root), json.dumps(report))

    def test_delayed_receipts_do_not_inherit_operation_start_coverage(self):
        p = deepcopy(self.projection)
        for i, point in enumerate(p['points']):
            point.update(queue_received_at=self.stamp(61+60*i), count_received_at=self.stamp(61.5+60*i))
        p['generated_at'] = self.stamp(122)
        r = self.report(p)
        self.assertEqual(r['coverage']['operation_start_covered_completed_bins'], 2)
        self.assertEqual(r['coverage']['queue_receipt_covered_completed_bins'], 1)
        self.assertEqual(r['coverage']['queue_receipt_missing_completed_bins'], 1)
        self.assertEqual(r['operation_to_queue_receipt']['maximum_seconds'], 61)
        self.assertEqual(r['queue_to_count_receipt']['maximum_seconds'], .5)
        self.assertEqual(r['quality_state'], 'attention')

    def test_unfinished_bin_excluded_and_later_report_does_not_extend_cutoff(self):
        p = deepcopy(self.projection); p['generated_at'] = self.stamp(61)
        first = self.report(p); later = self.report(p, as_of=self.stamp(3600))
        self.assertEqual(first['coverage']['completed_background_bins'], 1)
        self.assertEqual(first['quality_state'], 'in_progress')
        self.assertEqual(first['coverage'], later['coverage'])
        self.assertFalse(later['declared_day_closed_at_projection_cutoff'])
        self.assertEqual(later['projection_age_seconds'], 3539)

    def test_projection_after_requested_time_rejected(self):
        with self.assertRaisesRegex(RemoteTaskError, 'future_projection'):
            self.report(as_of=self.stamp(60))

    def test_truncated_graph_missing_bins_not_whole_day_failure(self):
        p = deepcopy(self.projection); p['points'] = p['points'][1:]
        p.update(graph_truncated=True, returned_graph_points=1)
        p['summary'].update(graph_truncated=True, returned_graph_points=1)
        r = self.report(p)
        self.assertEqual(r['quality_state'], 'partial_projection')
        self.assertIsNone(r['coverage']['queue_receipt_missing_completed_bins'])
        self.assertIsNone(r['coverage']['completed_bin_receipt_fraction'])
        self.assertEqual(r['coverage']['returned_projection_coverage_lower_bound_fraction'], .5)

    def test_empty_unknown_day_does_not_claim_zero_missing(self):
        p = deepcopy(self.projection); p.update(points=[], summary=None, returned_graph_points=0)
        r = self.report(p)
        self.assertEqual(r['quality_state'], 'no_observations')
        self.assertIsNone(r['coverage']['completed_background_bins'])
        self.assertFalse(r['declared_day_observed_without_detected_gap'])

    def test_failed_count_keeps_good_queue_receipt_but_reports_pair_failure(self):
        p = deepcopy(self.projection); row = p['points'][1]
        row.update(pair_ok=False, count_raw=None, error_codes={'storequeuecount': 'http_error'})
        p['summary'].update(successful_pairs=1, failed_pairs=1)
        r = self.report(p)
        self.assertEqual(r['subset_queue_usable_responses'], 2)
        self.assertEqual(r['coverage']['completed_bin_receipt_fraction'], 1)
        self.assertEqual(r['quality_state'], 'attention')

    def test_numeric_backwards_and_rapid_steps_are_not_no_show_estimates(self):
        p = deepcopy(self.projection)
        for row, label in zip(p['points'], ['100', '20']):
            for q in ['mixedQueue', 'reservationQueue']:
                row['queues'][q] = [label]; row['call_reference_labels'][q] = label
                row['display_sizes'][q] = 1
        r = self.report(p)
        self.assertEqual(r['first_label_changes']['mixedQueue']['backward_steps'], 1)
        p['points'][1]['queues']['mixedQueue'] = ['200']
        p['points'][1]['call_reference_labels']['mixedQueue'] = '200'
        r = self.report(p)
        self.assertEqual(r['first_label_changes']['mixedQueue']['rapid_forward_steps'], 1)
        self.assertIsNone(r['no_show_rate']); self.assertFalse(r['actual_call_verified'])

    def test_run_boundary_does_not_create_numeric_step(self):
        p = deepcopy(self.projection); p['points'][1]['comparison_state'] = 'run_boundary'
        self.assertEqual(self.report(p)['first_label_changes']['mixedQueue']['comparable_numeric_steps'], 0)

    def test_missing_receipt_and_time_inversion_excluded_from_coverage(self):
        for change, code in [({'queue_received_at': None}, 'queue_payload_without_receipt'),
                             ({'queue_received_at': self.stamp(59)}, 'response_before_operation_start')]:
            p = deepcopy(self.projection); p['points'][1].update(change)
            r = self.report(p)
            self.assertEqual(r['anomaly_counts'][code], 1)
            self.assertEqual(r['coverage']['queue_receipt_missing_completed_bins'], 1)

    def test_future_response_and_reverse_pair_receipts_reported(self):
        p = deepcopy(self.projection); p['points'][1]['queue_received_at'] = self.stamp(125)
        r = self.report(p)
        self.assertEqual(r['anomaly_counts']['point_after_projection_cutoff'], 1)
        self.assertEqual(r['anomaly_counts']['count_receipt_before_queue_receipt'], 1)

    def test_summary_counts_not_silently_trusted(self):
        p = deepcopy(self.projection); p['summary']['group_successes'] = 1
        self.assertEqual(self.report(p)['anomaly_counts']['summary_queue_success_count_mismatch'], 1)

    def test_overlapping_or_outside_day_intervals_rejected(self):
        for intervals in [[[self.stamp(0), self.stamp(120)], [self.stamp(60), self.stamp(180)]],
                          [[self.stamp(-86400), self.stamp(120)]]]:
            p = deepcopy(self.projection); p['summary']['declared_intervals'] = intervals
            with self.assertRaisesRegex(RemoteTaskError, 'invalid_declared_intervals'): self.report(p)

    def test_policy_and_wrong_store_rejected(self):
        for policy in [{'interval_seconds': 0}, {'max_gap_seconds': 59}, {'rapid_positions': True}]:
            with self.assertRaises(RemoteTaskError): self.report(**policy)
        p = deepcopy(self.projection); p['requested_store_id'] = '900002'
        with self.assertRaises(RemoteTaskError): self.report(p)

    def test_split_business_windows_do_not_create_lunch_gap(self):
        p = deepcopy(self.projection)
        row = p['points'][1]; row.update(request_started_at=self.stamp(180),
            queue_received_at=self.stamp(180), count_received_at=self.stamp(180), comparison_state='gap')
        p['summary']['declared_intervals'] = [[self.stamp(0), self.stamp(60)], [self.stamp(180), self.stamp(240)]]
        p['generated_at'] = self.stamp(240)
        r = self.report(p)
        self.assertEqual(r['queue_receipt_intervals']['over_threshold_count'], 0)
        self.assertEqual(r['quality_state'], 'declared_day_observed')

    def test_cli_reads_one_explicit_private_projection_and_changes_no_files(self):
        path = self.f.root/'2026-10-09/projection.json'; before = path.read_bytes()
        out = io.StringIO()
        with patch('sys.stdout', out), patch('socket.socket', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('database')):
            code = main(['daily-quality-report', '--projection-file', str(path), '--store-id', '900001',
                '--date', '2026-10-09', '--as-of', self.stamp(120)])
        self.assertEqual(code, 0); self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(out.getvalue())['quality_state'], 'declared_day_observed')
        self.assertNotIn(str(path), out.getvalue())

    def test_cli_rejects_public_file_without_exposing_path(self):
        path = self.f.root/'2026-10-09/projection.json'; path.chmod(0o644)
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = main(['daily-quality-report', '--projection-file', str(path), '--store-id', '900001',
                '--date', '2026-10-09', '--as-of', self.stamp(120)])
        self.assertEqual(code, 2); self.assertNotIn(str(path), out.getvalue())

    def test_controller_automatically_archives_deterministic_quality(self):
        r = self.f.archive(name='quality.json')
        self.assertEqual(r, self.report())
        self.assertEqual(r['projection_content_sha256'], self.f.archive()['projection_sha256'])
        self.assertEqual((self.f.root/'2026-10-09/quality.json').stat().st_mode & 0o777, 0o600)
