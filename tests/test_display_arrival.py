"""First-two appearances and unobserved-ticket crossing labels."""
from copy import deepcopy
import unittest

from sushiwait.displayarrival import classify_display, label_ticket_arrival
from test_display_replay import archive


def sample(rows, *, queue='mixedQueue', step=5):
    value = archive(tuple(range(100, 100+len(rows))), step=step)
    for point, labels in zip(value['points'], rows):
        point['queues'][queue] = labels
    return value


def label(value, **options):
    return label_ticket_arrival(value, store_id='900001', number='150',
        issued_at='2026-10-13T02:55:00Z', **options)


class DisplayArrivalTests(unittest.TestCase):
    def test_first_and_second_are_arrival_but_third_is_upcoming(self):
        for numbers, state in ((['150', '151', '160'], 'display_top_two'),
                (['148', '150', '160'], 'display_top_two'),
                (['148', '149', '150'], 'display_third')):
            with self.subTest(numbers=numbers):
                self.assertEqual(classify_display('150', numbers), state)

    def test_unsorted_positions_are_not_sorted_before_matching(self):
        self.assertEqual(classify_display('150', ['160', '170', '150']), 'display_third')
        self.assertEqual(classify_display('0150', ['160', '150', '149']), 'display_top_two')

    def test_passing_needs_three_strictly_greater_numbers(self):
        for labels in (['151'], ['151', '152'], ['140', '160', '170'], ['151', '152', '150']):
            self.assertNotEqual(classify_display('150', labels), 'display_all_three_passed')
        self.assertEqual(classify_display('150', ['170', '151', '160']), 'display_all_three_passed')

    def test_empty_missing_and_unsupported_do_not_pass(self):
        for labels in ([], None, ['A151', 'A152', 'A153'], ['0', '160', '170'],
                       [151, 152, 153], ['151', '152', '153', '154']):
            self.assertNotIn(classify_display('150', labels), ('display_top_two', 'display_all_three_passed'))

    def test_direct_arrival_wait_is_from_issue_time_and_interval_kept(self):
        value = sample([['140', '145', '150'], ['148', '150', '153']])
        result = label(value, checked_in=True)
        self.assertEqual(result['state'], 'arrived')
        self.assertEqual(result['display_position'], 2)
        self.assertEqual(result['wait_minutes'], {'lower': 5, 'upper': 10})
        self.assertEqual(result['arrival_interval']['basis'], 'adjacent_samples')
        self.assertEqual(result['detected_at'], '2026-10-13T03:05:00Z')

    def test_unchecked_ticket_can_get_inferred_label_without_ever_appearing(self):
        result = label(sample([['140', '145', '148'], ['152', '156', '160']]), checked_in=False)
        self.assertEqual(result['state'], 'inferred_arrived')
        self.assertEqual(result['label_kind'], 'display_all_three_passed')
        self.assertEqual(result['wait_minutes'], {'lower': 5, 'upper': 10})
        self.assertIsNone(result['display_position'])
        self.assertFalse(result['actual_call_verified'] or result['no_show_verified'] or result['network_performed'])

    def test_three_minute_and_ten_minute_intervals_are_not_exact_labels(self):
        for step in (3, 10):
            result = label(sample([['140'], ['151', '152', '153']], step=step))
            self.assertEqual(result['wait_minutes']['upper']-result['wait_minutes']['lower'], step)

    def test_first_sample_already_passed_is_coarse_not_exact(self):
        result = label(sample([['151', '152', '153']]))
        self.assertEqual(result['wait_minutes'], {'lower': 0, 'upper': 5})
        self.assertEqual(result['arrival_interval']['basis'], 'issue_to_detection')
        self.assertIn('no_adjacent_before_arrival_sample', result['quality_flags'])

    def test_gap_empty_failure_or_missing_widens_but_keeps_later_label(self):
        for kind in ('empty', 'failure', 'missing', 'gap', 'writer'):
            with self.subTest(kind=kind):
                value = sample([['140'], ['145'], ['151', '152', '153']], step=15 if kind == 'gap' else 5)
                if kind == 'empty': value['points'][1]['queues']['mixedQueue'] = []
                if kind == 'failure': value['points'][1]['pair_ok'] = False
                if kind == 'missing': value['points'][1]['queue_field_presence']['mixedQueue'] = 'absent'
                if kind == 'writer': value['points'][2]['continuity']['writer_id'] = 'new'
                result = label(value)
                self.assertEqual(result['state'], 'inferred_arrived')
                self.assertEqual(result['wait_minutes']['lower'], 0)
                self.assertEqual(result['arrival_interval']['basis'], 'issue_to_detection')

    def test_detected_reset_suspends_numeric_inference_even_across_gap(self):
        for step in (5, 15):
            value = sample([['140'], ['1'], ['200', '201', '202']], step=step)
            result = label(value)
            self.assertFalse(result['result_available'])
            self.assertEqual(result['state'], 'number_cycle_uncertain')
            self.assertIn('number_reversal', result['quality_flags'])

    def test_partial_position_backfill_does_not_suspend_all_three_crossing(self):
        value = sample([['130','140','145'], ['125','140','146'], ['152','156','160']])
        result = label(value)
        self.assertEqual(result['state'], 'inferred_arrived')
        self.assertIn('display_position_backfill', result['quality_flags'])
        self.assertNotIn('number_reversal', result['quality_flags'])

    def test_exact_match_after_reversal_still_records_display_evidence(self):
        result = label(sample([['140'], ['1'], ['150', '151', '152']]))
        self.assertEqual(result['state'], 'arrived')
        self.assertIn('number_reversal', result['quality_flags'])
        self.assertEqual(result['wait_minutes']['lower'], 0)

    def test_reported_no_checkin_is_not_silently_overwritten(self):
        result = label(sample([['150']]), checked_in=False)
        self.assertEqual(result['state'], 'arrived')
        self.assertFalse(result['checked_in_reported'])
        self.assertIn('display_match_despite_reported_no_checkin', result['quality_flags'])

    def test_reserved_and_ordinary_are_never_mixed(self):
        value = sample([['140'], ['150']], queue='reservationQueue')
        self.assertFalse(label(value)['result_available'])
        self.assertTrue(label(value, queue='reservation')['result_available'])

    def test_before_issue_is_ignored_and_no_future_data_is_invented(self):
        value = sample([['150'], ['140']])
        result = label_ticket_arrival(value, store_id='900001', number='150',
            issued_at='2026-10-13T03:01:00Z')
        self.assertFalse(result['result_available'])
        self.assertEqual(result['state'], 'waiting')
        self.assertIsNone(result['arrival_interval'])

    def test_later_data_never_changes_earliest_observed_label(self):
        before = sample([['140'], ['149', '150', '151']])
        after = sample([['140'], ['149', '150', '151'], ['155', '156', '160']])
        self.assertEqual(label(before), label(after))

    def test_bad_ticket_scope_and_types_rejected(self):
        value = sample([['150']])
        for change in ({'store_id': '900002'}, {'issued_at': '2026-10-12T03:00:00Z'},
                {'issued_at': '2026-10-13T23:00:00Z'}, {'number': 'A150'}, {'number': '0'},
                {'number': True}, {'queue': 'all'}, {'checked_in': 'false'}, {'max_gap_seconds': True}):
            args = {'store_id': '900001', 'number': '150', 'issued_at': '2026-10-13T02:55:00Z', **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                label_ticket_arrival(value, **args)

    def test_input_and_archive_scope_preserved(self):
        value = sample([['140'], ['150']]); original = deepcopy(value)
        label(value)
        self.assertEqual(value, original)
        value['graph_truncated'] = True
        with self.assertRaises(ValueError): label(value)

    def test_absent_outcome_is_retained_not_a_zero_wait(self):
        result = label(sample([['140'], [], ['145']]))
        self.assertFalse(result['result_available'])
        self.assertIsNone(result['wait_minutes'])
        self.assertEqual(result['last_observed_at'], '2026-10-13T03:10:00Z')
