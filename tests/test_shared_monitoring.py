"""Shared demand, wake boundaries and privacy using synthetic plans only."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.shared_monitoring import SharedMonitoringError, read_plan_file, shared_polling_policy


class SharedMonitoringTests(unittest.TestCase):
    def plan(self, store='3004', arrival='2026-10-06T19:00:00+08:00', **changes):
        return {'store_id': store, 'desired_arrival_at': arrival, **changes}

    def policy(self, plans=None, *, as_of='2026-10-06T18:40:00+08:00', last=None, **changes):
        doc = {'schema_version': 1, 'plans': [self.plan()] if plans is None else plans}
        if last is not None:
            doc['last_poll_started_at'] = last
        return shared_polling_policy(doc, as_of=as_of, base_interval=300, **changes)

    def test_many_plans_share_one_query_at_fastest_active_cadence(self):
        result = self.policy([self.plan(), self.plan(arrival='2026-10-06T18:50:00+08:00'),
                              self.plan(plan_status='ended', accelerated_display_turnover=True)])
        store = result['stores'][0]
        self.assertEqual((result['store_count'], store['plan_count'], store['waiting_plan_count']), (1, 3, 2))
        self.assertEqual(store['requested_interval_seconds'], 30)
        self.assertEqual(store['requested_queries_per_due'], 1)
        self.assertTrue(store['requested_poll_due'])

    def test_different_stores_are_independent_and_sorted(self):
        result = self.policy([self.plan('3014'), self.plan('3004', '2026-10-06T18:50:00+08:00')],
                             last={'3004': '2026-10-06T18:39:50+08:00'})
        self.assertEqual([s['store_id'] for s in result['stores']], ['3004', '3014'])
        self.assertFalse(result['stores'][0]['requested_poll_due'])
        self.assertTrue(result['stores'][1]['requested_poll_due'])

    def test_terminal_only_store_and_empty_input_have_no_wake(self):
        result = self.policy([self.plan(plan_status='called')])
        store = result['stores'][0]
        self.assertIsNone(store['requested_interval_seconds'])
        self.assertIsNone(store['next_recheck_target_at'])
        self.assertEqual(store['requested_queries_per_due'], 0)
        self.assertIsNone(self.policy([])['next_recheck_target_at'])

    def test_actual_start_anchors_cadence_without_resetting_on_each_evaluation(self):
        last = {'3004': '2026-10-06T18:39:50+08:00'}
        a = self.policy(last=last)['stores'][0]
        b = self.policy(as_of='2026-10-06T18:40:20+08:00', last=last)['stores'][0]
        self.assertEqual(a['next_poll_target_at'], '2026-10-06T10:40:50.000000Z')
        self.assertEqual(a['next_poll_target_at'], b['next_poll_target_at'])

    def test_background_sleep_wakes_at_thirty_minute_boundary(self):
        result = self.policy(as_of='2026-10-06T18:28:00+08:00',
                             last={'3004': '2026-10-06T18:28:00+08:00'})['stores'][0]
        self.assertEqual(result['next_poll_target_at'], '2026-10-06T10:33:00.000000Z')
        self.assertEqual(result['next_recheck_target_at'], '2026-10-06T10:30:00.000000Z')
        self.assertEqual(result['next_policy_transition_at'], result['next_recheck_target_at'])

    def test_fifteen_minute_boundary_can_preempt_sixty_second_wait(self):
        result = self.policy(as_of='2026-10-06T18:44:50+08:00',
                             last={'3004': '2026-10-06T18:44:40+08:00'})['stores'][0]
        self.assertEqual(result['next_recheck_target_at'], '2026-10-06T10:45:00.000000Z')
        self.assertEqual(result['next_poll_target_at'], '2026-10-06T10:45:40.000000Z')
        boundary = self.policy(as_of='2026-10-06T18:45:00+08:00',
                              last={'3004': '2026-10-06T18:44:40+08:00'})['stores'][0]
        self.assertEqual(boundary['next_poll_target_at'], '2026-10-06T10:45:10.000000Z')
        self.assertEqual(boundary['requested_interval_seconds'], 30)

    def test_long_gap_requests_one_current_query_and_no_catch_up(self):
        result = self.policy(last={'3004': '2026-10-01T00:00:00Z'})
        self.assertTrue(result['stores'][0]['requested_poll_due'])
        self.assertEqual(result['catch_up_requests'], 0)
        self.assertEqual(result['stores'][0]['requested_queries_per_due'], 1)
        self.assertEqual(result['stores'][0]['next_poll_target_at'], result['as_of'])

    def test_negative_offset_and_external_estimate_advance_shared_cadence(self):
        for extra in ({'call_offset_minutes': -10}, {'earliest_call_at': '2026-10-06T18:50:00+08:00'}):
            self.assertEqual(self.policy([self.plan(**extra)])['stores'][0]['requested_interval_seconds'], 30)
        self.assertEqual(self.policy([self.plan(call_offset_minutes=10)])['stores'][0]['requested_interval_seconds'], 60)

    def test_acceleration_has_no_additional_boundary_or_no_show_claim(self):
        result = self.policy([self.plan(accelerated_display_turnover=True)],
                             as_of='2026-10-06T17:00:00Z')
        self.assertIsNone(result['stores'][0]['next_policy_transition_at'])
        self.assertIsNone(result['true_no_show_rate'])

    def test_future_or_unknown_store_start_is_refused(self):
        for last in ({'3004': '2026-10-06T18:40:00.000001+08:00'}, {'2009': '2026-10-06T18:00:00+08:00'}):
            with self.subTest(last=last), self.assertRaises(SharedMonitoringError):
                self.policy(last=last)

    def test_three_store_and_128_plan_bounds(self):
        self.assertEqual(self.policy([self.plan()] * 128)['plan_count'], 128)
        for plans in ([self.plan()] * 129, [self.plan(str(i)) for i in (1, 2, 3, 4)]):
            with self.assertRaises(SharedMonitoringError):
                self.policy(plans)

    def test_schema_types_unknown_fields_and_invalid_store_ids(self):
        docs = [None, [], {}, {'schema_version': True, 'plans': []}, {'schema_version': 1, 'plans': [], 'token': 'secret'},
                {'schema_version': 1, 'plans': {}}, {'schema_version': 1, 'plans': [None]}]
        for doc in docs:
            with self.subTest(doc=doc), self.assertRaises(SharedMonitoringError):
                shared_polling_policy(doc, as_of='2026-10-06T18:40:00+08:00', base_interval=300)
        for store in ('03004', '0', '１２３', '../3004', '1'*13, 3004):
            with self.assertRaises(SharedMonitoringError):
                self.policy([self.plan(store)])
        with self.assertRaises(SharedMonitoringError):
            self.policy([self.plan(personal_ticket='synthetic-secret')])

    def test_timezones_and_missing_timezone(self):
        a = self.policy(last={'3004': '2026-10-06T10:39:50Z'})
        b = self.policy(as_of='2026-10-06T10:40:00Z', last={'3004': '2026-10-06T10:39:50Z'})
        self.assertEqual(a, b)
        with self.assertRaises(SharedMonitoringError):
            self.policy(as_of='2026-10-06T18:40:00')

    def test_overflow_and_year_one_boundary(self):
        with self.assertRaises(SharedMonitoringError):
            self.policy(as_of='9999-12-31T23:59:59Z', last={'3004': '9999-12-31T23:59:59Z'})
        result = self.policy([self.plan(arrival='0001-01-01T00:00:01Z')], as_of='0001-01-01T00:00:00Z')
        self.assertIsNone(result['stores'][0]['next_policy_transition_at'])

    def test_output_has_no_personal_plan_timestamps_or_details(self):
        result = self.policy([self.plan(arrival='2026-10-06T18:53:17.123456+08:00')])
        text = json.dumps(result)
        self.assertNotIn('desired_arrival_at', text)
        self.assertNotIn('18:53:17', text)
        self.assertFalse(result['personal_plan_details_in_output'])
        self.assertTrue(result['output_requires_private_handling'])

    def test_no_network_credentials_or_native_operations(self):
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch('socket.create_connection', side_effect=AssertionError('network')), \
                patch('sushiwait.credentials.read_credentials_file', side_effect=AssertionError('credentials')), \
                patch('sushiwait.surgeguard._command', side_effect=AssertionError('native')):
            result = self.policy()
        for field in ('network_performed', 'scheduler_applied', 'polling_performed', 'credentials_accessed',
                      'eta_available', 'notification_sent', 'business_operation_performed'):
            self.assertFalse(result[field])

    def test_private_plan_file_cli_and_redacted_failures(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw).resolve()
            directory.chmod(0o700)
            path = directory / 'plans.json'
            path.write_text(json.dumps({'schema_version': 1, 'plans': [self.plan(), self.plan()]}))
            path.chmod(0o600)
            args = ['monitor-stores', '--plans-file', str(path), '--as-of', '2026-10-06T18:40:00+08:00', '--base-interval', '300']
            output = io.StringIO()
            with patch('socket.socket', side_effect=AssertionError('network')), contextlib.redirect_stdout(output):
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.getvalue())['stores'][0]['requested_queries_per_due'], 1)
            path.write_text('{"schema_version":1,"schema_version":1,"plans":[],"private":"synthetic-secret"}')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(args), 1)
            self.assertNotIn('synthetic-secret', output.getvalue())
            self.assertNotIn(str(path), output.getvalue())

    def test_file_permissions_symlink_size_invalid_json_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw).resolve()
            directory.chmod(0o700)
            path = directory / 'plans.json'
            path.write_text('{}'); path.chmod(0o644)
            with self.assertRaises(SharedMonitoringError): read_plan_file(path)
            path.chmod(0o600)
            link = directory / 'link.json'; link.symlink_to(path)
            with self.assertRaises(SharedMonitoringError): read_plan_file(link)
            for content in ('x'*16385, 'not-json', '{"x":NaN}', '[]'):
                path.write_text(content)
                with self.assertRaises(SharedMonitoringError): read_plan_file(path)


if __name__ == '__main__':
    unittest.main()
