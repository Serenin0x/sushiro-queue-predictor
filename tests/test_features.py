"""Complete historical scans and private joins; all inputs are synthetic."""
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.features import FeatureError, build_feature_dataset, validate_feature_plan
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.remote import RemoteStore
from sushiwait.reviews import OutcomeReviewStore
import test_backtest as outcome_helpers
from test_backtest import BASE, stamp
from test_remote_signals import record as raw_record


def queue_record(second, labels=(), count=0, **kwargs):
    value = raw_record(second, labels, count, **kwargs)
    if value['queries']['groupqueues']['ok']:
        value['queries']['groupqueues']['payload']['queues']['mixedQueue'] = list(labels)
    for item in value['queries'].values():
        if item['attempted']:
            from test_remote_signals import BASE as old_base
            for key in ('started_at', 'received_at'):
                from datetime import datetime
                item[key] = (datetime.fromisoformat(item[key].replace('Z', '+00:00'))
                             - (old_base-BASE)).isoformat()
    return value


class FeatureDatasetTests(unittest.TestCase):
    episode = outcome_helpers.BacktestTests.episode
    add = outcome_helpers.BacktestTests.add
    receive_review = outcome_helpers.BacktestTests.receive_review

    def setUp(self):
        outcome_helpers.BacktestTests.setUp(self)
        (self.parent/'remote').mkdir(mode=0o700)
        self.remote_path = self.parent/'remote/remote.sqlite3'
        self.plan = {'schema_version': 1, 'as_of': stamp(1000), 'data_origin': 'synthetic',
            'queue_data_origin': 'synthetic', 'api_profile': 'miniapp_gateway', 'store_id': '900001',
            'elapsed_seconds': [0, 300], 'max_cases': 100, 'window_seconds': 120, 'max_gap_seconds': 90}
        self.write([(t, [str(i), '99-1'], 10-i) for i, t in enumerate([0, 30, 60, 90, 110])])

    def write(self, entries, *, receipt=None, run=None):
        with RemoteStore(self.remote_path) as store:
            if run is not None:
                store.run_id = run
            for t, labels, count, *options in entries:
                value = queue_record(t, labels, count, **(options[0] if options else {}))
                with patch('sushiwait.remoteintake._clock', return_value=BASE+timedelta(seconds=t+1 if receipt is None else receipt)):
                    store.append(value)

    def calculate(self, plan=None, **kwargs):
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, \
                OutcomeReviewStore(self.review_path, read_only=True) as reviews, \
                RemoteStore(self.remote_path, read_only=True) as remote, \
                patch('sushiwait.features._clock', return_value=BASE+timedelta(seconds=3000)), \
                patch('sushiwait.reviews._clock', return_value=BASE+timedelta(seconds=3000)), \
                patch('socket.socket', side_effect=AssertionError('network')):
            return build_feature_dataset(source=source, reviews=reviews, remote=remote,
                                         plan=self.plan if plan is None else plan, **kwargs)

    def target_row(self, result=None, elapsed=0):
        result = self.calculate() if result is None else result
        return next(row for row in result['rows'] if row['episode_id'] == self.target['episode_id']
                    and row['features']['elapsed_seconds'] == elapsed)

    def queues(self, result=None, elapsed=0):
        return self.target_row(result, elapsed)['features']['queue_observations']

    def mutate(self, transform, *, row=1):
        db = sqlite3.connect(self.remote_path)
        encoded = db.execute('SELECT payload_json FROM remote_samples WHERE id=?', (row,)).fetchone()[0]
        value = json.loads(encoded)
        transform(value)
        db.execute('UPDATE remote_samples SET payload_json=? WHERE id=?', (json.dumps(value), row))
        db.commit(); db.close()

    def test_received_features_join_reviewed_intervals_without_label_midpoints(self):
        result = self.calculate(); row = self.target_row(result); features = self.queues(result)
        self.assertEqual(result['research_rows'], 4)
        self.assertEqual(features['reported_count_raw'], 6)
        self.assertEqual(features['count_unit'], 'unknown')
        self.assertEqual(features['display_sizes']['storeQueue'], {'array_length': 2, 'distinct_labels': 2})
        self.assertEqual(features['display_window_statistics']['storeQueue']['whole']['comparable_pairs'], 4)
        self.assertEqual(row['target_remaining_call_interval_us'], {'lower': 600_000_000, 'upper': 660_000_000})
        self.assertEqual(row['features']['calendar_at_prediction']['date_type'], 'holiday')
        self.assertFalse(result['training_eligible'] or result['eta_available'] or result['model_fitted'])
        self.assertFalse(row['historical_target_context_verified'])
        self.assertEqual(result['verified_training_labels'], 0)

    def test_later_queries_do_not_change_earlier_features_or_fingerprints(self):
        before = self.calculate()
        self.write([(800, ['9999999'], 900)])
        after = self.calculate()
        self.assertEqual(before['feature_dataset_sha256'], after['feature_dataset_sha256'])
        self.assertEqual(self.target_row(before), self.target_row(after))
        self.assertGreater(after['audit_diagnostics_not_features']['queue_rows_audited'],
                           before['audit_diagnostics_not_features']['queue_rows_audited'])

    def test_complete_scan_keeps_old_rows_when_future_rows_are_added(self):
        before = self.queues()
        self.write([(800+i, ['500'], i) for i in range(20)])
        self.assertEqual(before, self.queues())
        with self.assertRaises(FeatureError) as caught:
            self.calculate(max_observations=5)
        self.assertEqual(caught.exception.error_code, 'features_history_limit')

    def test_late_received_old_response_is_excluded_at_prediction_cutoff(self):
        before = self.queues()
        self.write([(115, ['800'], 50)], receipt=200)
        self.assertEqual(before, self.queues())
        row = self.queues(elapsed=300)
        self.assertEqual(row['queue_availability'], 'no_responses')

    def test_pair_completion_prevents_using_first_response_before_second_finishes(self):
        before = self.queues()
        self.write([(115, ['800'], 50, {'count_received': 200})], receipt=201)
        self.assertEqual(before, self.queues())

    def test_missing_first_receipt_never_invents_historical_availability(self):
        self.mutate(lambda value: value.pop('local_intake'), row=5)
        result = self.queues()
        self.assertEqual(result['queue_availability'], 'unknown')
        self.assertIsNone(result['display_sizes'])
        self.assertIsNone(result['reported_count_raw'])

    def test_missing_receipt_breaks_change_chain_before_next_valid_observation(self):
        self.mutate(lambda value: value.pop('local_intake'), row=4)
        value = self.queues()['display_window_statistics']['storeQueue']['whole']
        self.assertEqual(value['comparable_pairs'], 2)

    def test_receipt_tampering_rejects_all_rows_not_partial_features(self):
        self.mutate(lambda value: value['local_intake'].update(record_sha256='0'*64))
        with self.assertRaises(FeatureError) as caught:
            self.calculate()
        self.assertEqual(caught.exception.error_code, 'features_remote_invalid')

    def test_wrong_store_payload_binding_rejects(self):
        self.mutate(lambda value: value.update(requested_store_id='900002'))
        with self.assertRaises(FeatureError):
            self.calculate()

    def test_unrelated_store_rows_do_not_enter_features(self):
        before = self.calculate()
        self.write([(115, ['800'], 50, {'store': '900002'})])
        self.assertEqual(before['feature_dataset_sha256'], self.calculate()['feature_dataset_sha256'])

    def test_failed_count_retains_queue_features_without_current_count(self):
        self.write([(115, ['800'], 0, {'count_error': True})])
        value = self.queues()
        self.assertEqual(value['queue_availability'], 'recent_responses')
        self.assertEqual(value['count_availability'], 'interrupted')
        self.assertIsNone(value['reported_count_raw'])
        self.assertEqual(value['display_sizes']['storeQueue']['array_length'], 1)

    def test_queue_failure_and_skipped_count_do_not_reuse_last_good_payload(self):
        self.write([(115, [], 0, {'queue_error': True})])
        value = self.queues()
        self.assertEqual(value['queue_availability'], 'interrupted')
        self.assertEqual(value['count_availability'], 'interrupted')
        self.assertIsNone(value['display_sizes']); self.assertIsNone(value['reported_count_raw'])

    def test_run_boundary_is_not_a_comparable_change(self):
        self.write([(115, ['800'], 0)], run='00000000-0000-0000-0000-000000000002')
        self.assertEqual(self.queues()['display_window_statistics']['storeQueue']['whole']['comparable_pairs'], 4)

    def test_large_gap_does_not_create_comparable_change(self):
        result = self.calculate({**self.plan, 'max_gap_seconds': 10})
        self.assertEqual(self.queues(result)['display_window_statistics']['storeQueue']['whole']['comparable_pairs'], 0)

    def test_non_increasing_time_refuses_current_payload_and_double_counting(self):
        self.write([(90, ['800'], 0)], receipt=115)
        value = self.queues()
        self.assertEqual(value['queue_availability'], 'interrupted')
        self.assertIsNone(value['display_sizes']); self.assertIsNone(value['reported_count_raw'])

    def test_opaque_duplicate_labels_have_lengths_not_numeric_queue_positions(self):
        self.write([(115, ['99', '99', '100-1'], 0)])
        value = self.queues()
        self.assertEqual(value['display_sizes']['storeQueue'], {'array_length': 3, 'distinct_labels': 2})
        self.assertEqual(value['reported_count_raw'], 0)
        self.assertIsNone(value['true_no_show_rate'])
        self.assertNotIn('maximum_number', json.dumps(value))

    def test_stale_responses_keep_observed_count_but_availability_is_stale(self):
        value = self.queues(self.calculate({**self.plan, 'window_seconds': 600}), elapsed=300)
        self.assertEqual(value['queue_availability'], 'stale_responses')
        self.assertEqual(value['reported_count_raw'], 6)
        self.assertGreater(value['queue_response_age_seconds'], 90)

    def test_possible_call_at_reconstructed_time_is_excluded(self):
        result = self.calculate({**self.plan, 'elapsed_seconds': [0, 900]})
        self.assertEqual(result['research_rows'], 2)
        self.assertEqual(result['excluded_call_not_definitely_future'], 2)

    def test_review_receipt_must_be_known_by_evaluation_cutoff(self):
        result = self.calculate({**self.plan, 'as_of': stamp(901)})
        self.assertEqual(result['reviewed_scope_episodes'], 1)

    def test_case_limit_stops_before_remote_scan(self):
        with patch('sushiwait.features._history', side_effect=AssertionError('should not read')) as history:
            with self.assertRaises(FeatureError) as caught:
                self.calculate({**self.plan, 'max_cases': 3})
        self.assertEqual(caught.exception.error_code, 'features_case_limit')
        self.assertEqual(history.call_count, 0)

    def test_full_outcome_revision_limit_is_not_silent_sampling(self):
        with self.assertRaises(FeatureError) as caught:
            self.calculate(max_revisions=1)
        self.assertEqual(caught.exception.error_code, 'features_source_invalid')

    def test_total_byte_limit_stops_without_partial_dataset(self):
        with patch('sushiwait.features.MAX_HISTORY_BYTES', 1):
            with self.assertRaises(FeatureError) as caught:
                self.calculate()
        self.assertEqual(caught.exception.error_code, 'features_history_limit')

    def test_invalid_plans_and_mixed_source_declarations_are_refused(self):
        changes = [('schema_version', True), ('as_of', stamp(3001)), ('store_id', '001'),
            ('elapsed_seconds', []), ('elapsed_seconds', [0, 0]), ('elapsed_seconds', [True]),
            ('elapsed_seconds', [172801]), ('max_cases', 101), ('window_seconds', 29),
            ('max_gap_seconds', 0), ('queue_data_origin', 'live'), ('authorization', 'private')]
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(FeatureError):
                validate_feature_plan({**self.plan, key: value}, now=BASE+timedelta(seconds=3000))

    def cli(self, **changes):
        self.input_path.write_text(json.dumps(self.plan)); self.input_path.chmod(0o600)
        argv = {'--source-db': str(self.source_path), '--reviews-db': str(self.review_path),
            '--remote-db': str(self.remote_path), '--input': str(self.input_path), '--output': str(self.output_path)}
        argv.update(changes)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('socket.socket', side_effect=AssertionError('network')), \
                patch('sushiwait.cli.client_for', side_effect=AssertionError('client')), \
                patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')):
            code = main(['outcome-feature-export', *(piece for item in argv.items() for piece in item)])
        return code, json.loads(output.getvalue())

    def test_cli_safe_summary_private_artifact_and_unchanged_sources(self):
        before = tuple(p.read_bytes() for p in (self.source_path, self.review_path, self.remote_path))
        code, result = self.cli()
        self.assertEqual(code, 0); self.assertEqual(result['research_rows'], 4)
        self.assertEqual(self.output_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, tuple(p.read_bytes() for p in (self.source_path, self.review_path, self.remote_path)))
        for private in (self.target['episode_id'], stamp(120), '900001', str(self.parent)):
            self.assertNotIn(private, json.dumps(result))

    def test_existing_artifact_is_not_overwritten(self):
        self.cli(); before = self.output_path.read_bytes()
        code, result = self.cli()
        self.assertEqual(code, 1); self.assertEqual(result['error_code'], 'features_output_exists')
        self.assertEqual(before, self.output_path.read_bytes())

    def test_durability_unknown_is_nonzero_and_committed_not_retried(self):
        with patch('sushiwait.features._write_packet', return_value={'durability_confirmed': False}) as writer:
            code, result = self.cli()
        self.assertEqual(code, 1); self.assertTrue(result['committed'])
        self.assertFalse(result['durability_confirmed']); self.assertEqual(writer.call_count, 1)

    def test_invalid_plan_does_not_open_sources(self):
        self.plan['authorization'] = 'private'
        with patch('sushiwait.cli.OutcomeIntakeStore', side_effect=AssertionError('read')) as source:
            code, result = self.cli()
        self.assertEqual(code, 1); self.assertEqual(source.call_count, 0)
        self.assertEqual(result['error_code'], 'features_invalid_input')

    def test_path_alias_refused_before_sources_open(self):
        with patch('sushiwait.cli.OutcomeIntakeStore', side_effect=AssertionError('read')) as source:
            code, result = self.cli(**{'--output': str(self.remote_path)})
        self.assertEqual(code, 1); self.assertEqual(source.call_count, 0)

    def test_running_writer_prevents_external_database_read(self):
        with RemoteStore(self.remote_path):
            code, result = self.cli()
        self.assertEqual(code, 1); self.assertFalse(self.output_path.exists())
        self.assertEqual(result['error_code'], 'features_remote_invalid')

    def test_input_symlink_and_nonprivate_permissions_are_refused(self):
        self.input_path.write_text(json.dumps(self.plan)); self.input_path.chmod(0o644)
        from sushiwait.features import read_feature_plan
        with self.assertRaises(FeatureError): read_feature_plan(self.input_path)
        self.input_path.chmod(0o600)
        link = self.input_path.parent/'alias.json'; link.symlink_to(self.input_path)
        with self.assertRaises(FeatureError): read_feature_plan(link)

    def test_remote_writer_is_not_allowed_as_dataset_source(self):
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, \
                OutcomeReviewStore(self.review_path, read_only=True) as reviews, \
                RemoteStore(self.remote_path) as remote:
            with self.assertRaises(FeatureError) as caught:
                build_feature_dataset(source=source, reviews=reviews, remote=remote, plan=self.plan)
        self.assertEqual(caught.exception.error_code, 'features_remote_invalid')
