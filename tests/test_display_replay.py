"""Meaningful temporal and measurement boundaries for shadow forecasting."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sushiwait.displayreplay import replay


def archive(values=tuple(range(100, 300, 10)), step=5):
    start = datetime(2026, 10, 13, 3, tzinfo=timezone.utc)
    points = []
    for i, value in enumerate(values):
        at = (start + timedelta(minutes=step*i)).isoformat()
        points.append({'request_started_at': at, 'queue_received_at': at,
            'observation_id': str(i), 'pair_ok': True, 'scheduled_pause': False,
            'success_semantics': 'one_official_detail_response_not_two_query_pair',
            'error_codes': {}, 'queue_field_presence': {'mixedQueue': 'present', 'reservationQueue': 'present'},
            'queues': {'mixedQueue': [] if value is None else [str(value)], 'reservationQueue': []},
            'continuity': {'writer_id': 'writer', 'predecessor_writer_id': 'writer',
                           'predecessor_id': str(i-1)}})
    return {'source': 'sapi_miniapp_gateway', 'api_profile': 'miniapp_gateway',
            'daily_schema_version': 1, 'network_performed_by_read': False,
            'first_label_is_confirmed_call': False, 'eta_available': False,
            'call_reference_semantics': 'user_assumed_first_displayed_label',
            'graph_truncated': False, 'returned_graph_points': len(points),
            'local_date': '2026-10-13', 'requested_store_id': '900001',
            'generated_at': (start+timedelta(hours=12)).isoformat(), 'points': points}


class DisplayReplayTests(unittest.TestCase):
    def test_linear_sparse_reference_and_bounds(self):
        result = replay(archive(), offsets=(20,))
        case = result['cases'][1]
        self.assertEqual(case['target'], 150)
        self.assertEqual(case['outcome'], {'state': 'sampled_bracket', 'lower_minutes': 5, 'upper_minutes': 10})
        for value in case['forecasts'].values():
            self.assertEqual(value['minutes'], 10)
            self.assertEqual(value['bracket_error_lower_minutes'], 0)
            self.assertEqual(value['bracket_error_upper_minutes'], 5)
        self.assertFalse(result['actual_call_verified'])
        self.assertIsNone(result['target_was_issued'])
        self.assertEqual(result['tickets_created'], 0)

    def test_future_change_never_changes_prior_prediction_or_target(self):
        original = archive()
        altered = copy.deepcopy(original)
        for row in altered['points'][4:]:
            row['queues']['mixedQueue'] = ['600']
        before, after = (replay(a, offsets=(20,))['cases'][1] for a in (original, altered))
        self.assertEqual(before['target'], after['target'])
        for method in before['forecasts']:
            self.assertEqual(before['forecasts'][method]['minutes'], after['forecasts'][method]['minutes'])
        self.assertNotEqual(before['outcome'], after['outcome'])

    def test_cold_start_and_censored_origins_are_kept(self):
        report = replay(archive(), offsets=(1000,))
        self.assertEqual(len(report['cases']), 7)
        self.assertEqual(report['cases'][0]['forecasts']['median_30']['reason'], 'insufficient_history')
        self.assertEqual(report['summary']['paired_cases'], 0)
        self.assertEqual(report['summary']['outcome_states'], {'archive_end_censored': 7})

    def test_empty_is_not_zero_and_does_not_certify_passed(self):
        value = archive((100, 110, 120, 130, None, 300, 310))
        result = replay(value, offsets=(20,))
        self.assertEqual(result['invalid_samples'], {'empty_display': 1})
        case = result['cases'][1]
        self.assertEqual(case['outcome']['state'], 'boundary_censored')
        self.assertEqual(case['outcome']['reason'], 'empty_display')
        self.assertNotIn('bracket_error_lower_minutes', case['forecasts']['median_30'])

    def test_long_gap_does_not_become_two_hour_observed_label(self):
        value = archive()
        for row in value['points'][4:]:
            for key in ('queue_received_at', 'request_started_at'):
                row[key] = (datetime.fromisoformat(row[key])+timedelta(hours=2)).isoformat()
        case = replay(value, offsets=(20,))['cases'][1]
        self.assertEqual(case['outcome']['state'], 'horizon_censored')
        self.assertIsNone(case['outcome']['upper_minutes'])

    def test_reversal_writer_switch_and_broken_link_censor(self):
        for field in ('reversal', 'writer', 'link'):
            with self.subTest(field=field):
                value = archive()
                row = value['points'][4]
                if field == 'reversal': row['queues']['mixedQueue'] = ['2']
                if field == 'writer': row['continuity']['writer_id'] = 'new'
                if field == 'link': row['continuity']['predecessor_id'] = 'missing'
                case = replay(value, offsets=(20,))['cases'][1]
                self.assertEqual(case['outcome']['state'], 'boundary_censored')
                self.assertIsNone(case['outcome']['upper_minutes'])

    def test_ten_minute_cadence_is_research_supported_not_dense_certified(self):
        result = replay(archive(step=10), offsets=(20,))
        self.assertGreater(result['summary']['methods']['median_60']['available'], 0)
        self.assertFalse(result['eta_available'])
        strict = replay(archive(step=10), offsets=(20,), max_gap_seconds=120)
        self.assertEqual(strict['summary']['methods']['median_60']['available'], 0)

    def test_failures_missing_field_and_reservation_are_separate(self):
        value = archive()
        value['points'][4]['error_codes'] = {'queue': 'timeout'}
        value['points'][5]['queue_field_presence']['mixedQueue'] = 'absent'
        result = replay(value)
        self.assertEqual(result['invalid_samples'], {'failed_or_paused': 1, 'field_missing': 1})
        result = replay(archive(), queue='reservation')
        self.assertEqual(result['cases'], [])
        self.assertEqual(result['invalid_samples'], {'empty_display': 20})

    def test_invalid_scope_order_truncation_duplicate_rejected(self):
        for mutation in ('source', 'truncated', 'order', 'duplicate', 'future'):
            with self.subTest(mutation=mutation):
                value = archive()
                if mutation == 'source': value['source'] = 'crm_remote_v1_1'
                if mutation == 'truncated': value['graph_truncated'] = True
                if mutation == 'order': value['points'].reverse()
                if mutation == 'duplicate': value['points'][1]['observation_id'] = '0'
                if mutation == 'future': value['generated_at'] = value['points'][0]['queue_received_at']
                with self.assertRaises(ValueError): replay(value)

    def test_invalid_options_rejected(self):
        for options in ({'offsets': (True,)}, {'offsets': (1, 1)}, {'max_gap_seconds': 3600},
                        {'queue': 'all'}, {'stride_minutes': 0}, {'horizon_minutes': 999}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                replay(archive(), **options)

    def test_cli_reports_input_hash_and_rejects_duplicate_day(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'public-day.json'
            path.write_text(json.dumps(archive()))
            command = [sys.executable, str(root/'scripts/replay_display.py'), str(path)]
            env = {**os.environ, 'PYTHONPATH': str(root/'src')}
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(len(report['input_sha256'][0]['sha256']), 64)
            self.assertEqual(report['reports'][0]['tickets_created'], 0)
            duplicate = subprocess.run(command+[str(path)], env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(duplicate.stdout, '')
