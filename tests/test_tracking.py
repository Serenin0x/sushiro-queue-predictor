"""Private episodes, display uncertainty and concurrent-version publication."""
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.cli import main
from sushiwait.fusion import context_from_history, public_request
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.reviews import OutcomeReviewStore
from sushiwait.remoteservice import LiveRemoteView
from sushiwait.outcomes import _time, _utc
from sushiwait.tracking import (TrackingError, TrackingSession, validate_ticket, create_session,
    observe_session, prepare_prediction, calculate_prediction, publish_prediction, end_session, session_status)
import test_baseline as baseline
import test_collection_monitor as monitoring

BASE = baseline.BASE
stamp = baseline.stamp


def ticket(**changes):
    return {'schema_version': 1, 'episode_id': str(uuid4()), 'data_origin': 'synthetic',
        'api_profile': 'miniapp_gateway', 'store_id': '900001', 'queue_type': 'ordinary',
        'number': '13', 'issued_at': stamp(-300), 'party_size': 2, 'table_type': 'unknown',
        'checked_in': None, 'created_at': stamp(-1), 'deadline_at': stamp(7200),
        'desired_arrival_at': stamp(3000), 'call_offset_minutes': 0,
        'minimum_samples': 1, 'max_updates': 100, **changes}


def record(second=0, labels=('10', '11'), fail=None):
    row = monitoring.record(labels=labels, fail=fail)
    for query in row['queries'].values():
        if query['started_at'] is not None:
            query['started_at'] = stamp(second)
        if query['received_at'] is not None:
            query['received_at'] = stamp(second)
    return row


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.state = self.root/'state'; self.state.mkdir(mode=0o700)
        self.input_dir = self.root/'input'; self.input_dir.mkdir(mode=0o700)
        self.ticket = ticket()
        create_session(self.ticket, directory=self.state, now=BASE)
        self.view = LiveRemoteView(['900001'], stale_after_seconds=120)

    def projections(self, second=0, labels=('10', '11'), fail=None, saved=False, alive=True):
        self.view.publish(record(second, labels, fail), saved_history=saved)
        at = BASE+timedelta(seconds=second+1)
        view = self.view.snapshot('900001', now=at, service_state='running', worker_alive=alive)
        history = self.view.monitor_history('900001', now=at, service_state='running', worker_alive=alive)
        context = context_from_history(history, queue_type=self.ticket['queue_type'], now=at,
            max_local_age_seconds=120, ttl_seconds=60)
        return view, context, at

    def observe(self, second=0, labels=('10', '11'), **kw):
        view, context, at = self.projections(second, labels, **kw)
        result = observe_session(directory=self.state, view=view, context=context, as_of=context['as_of'], now=at)
        return result, prepare_prediction(directory=self.state, version=result['tracking_version'], now=at), at

    def prediction(self, preparation, at, **options):
        return calculate_prediction(preparation, now=at, **options)

    def candidate(self, preparation, fast=False):
        obs = preparation['receipt']['observation']; e = int((_time(obs['as_of'])-
            _time(self.ticket['issued_at'])).total_seconds()*1_000_000)
        ids = ['history', 'fast'] if fast else ['history']
        return {'schema_version': 2, 'data_origin': 'synthetic', 'model_version': 'tracking-synthetic-v1',
            'prediction_target': 'remaining', 'conditioning': 'call_not_observed_after_elapsed',
            'public_context': obs['public_context'], 'ai_blend_ppm': 1_000_000 if fast else 0,
            'candidates': [{'candidate_id': key, 'interval_sample': {'elapsed_us': e,
                'intervals': [[600_000_000, 660_000_000, 1]] if key == 'history' else [[400_000_000, 420_000_000, 1]]}}
                for key in ids], 'prior_weights_ppm': {key: 1_000_000 if key == 'history' else 0 for key in ids}}

    def test_ticket_and_receipts_private_and_bounded_no_identifier_summary(self):
        result, prep, at = self.observe()
        self.assertEqual((self.state/'ticket.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state/'observation-0001.json').stat().st_mode & 0o777, 0o600)
        text = json.dumps(result)
        for private in (self.ticket['episode_id'], self.ticket['issued_at'], str(self.state)):
            self.assertNotIn(private, text)
        self.assertNotIn('number', result); self.assertFalse(result['eta_available'])

    def test_ticket_unknown_time_and_checkin_stay_unknown(self):
        value = ticket(issued_at=None, checked_in=None)
        self.assertIsNone(validate_ticket(value, now=BASE)['issued_at'])
        self.assertIsNone(validate_ticket(value, now=BASE)['checked_in'])

    def test_invalid_ticket_types_fields_times_or_personal_extras_rejected(self):
        for change in ({'schema_version': True}, {'number': '<private>'}, {'party_size': True},
                {'checked_in': 1}, {'max_updates': 2001}, {'deadline_at': stamp(100000)},
                {'created_at': stamp(1)}, {'issued_at': stamp(1)}, {'phone': 'private'},
                {'store_id': '01'}, {'queue_type': 'storeQueue'}, {'call_offset_minutes': 1.0}):
            with self.subTest(change=change), self.assertRaises(TrackingError):
                validate_ticket(ticket(**change), now=BASE)

    def test_update_and_deadline_caps_cannot_reset_on_reopen(self):
        cfg = self.state/'ticket.json'; value = json.loads(cfg.read_bytes()); value['max_updates'] = 1
        cfg.write_text(json.dumps(value)); self.observe()
        with self.assertRaises(TrackingError) as caught: self.observe(30)
        self.assertEqual(caught.exception.error_code, 'tracking_update_limit')
        view, context, at = self.projections(40)
        with self.assertRaises(TrackingError) as caught:
            observe_session(directory=self.state, view=view, context=context, as_of=stamp(41),
                now=BASE+timedelta(seconds=7200))
        self.assertEqual(caught.exception.error_code, 'tracking_deadline')
        self.assertEqual(session_status(directory=self.state, now=at)['tracking_version'], 1)

    def test_initialize_does_not_overwrite_an_existing_session(self):
        before = (self.state/'ticket.json').read_bytes()
        with self.assertRaises(TrackingError): create_session(ticket(), directory=self.state, now=BASE)
        self.assertEqual(before, (self.state/'ticket.json').read_bytes())

    def test_display_exact_match_is_not_called_or_no_show(self):
        _, prep, at = self.observe(labels=('13', '13', '14'))
        report = prep['receipt']['display']
        self.assertEqual(report['state'], 'number_in_upcoming_display')
        self.assertEqual(report['selected_display_numbers'], ['13', '13', '14'])
        self.assertFalse(report['called_verified'] or report['no_show_verified'])
        self.assertIsNone(report['exact_front_tables'])

    def test_selected_queue_never_uses_another_queue_to_mark_present(self):
        row = record(labels=('10',)); row['queries']['groupqueues']['payload']['queues']['reservationQueue'] = ['13']
        self.view.publish(row)
        at = BASE+timedelta(seconds=1)
        view = self.view.snapshot('900001', now=at, service_state='running', worker_alive=True)
        context = context_from_history(self.view.monitor_history('900001', now=at,
            service_state='running', worker_alive=True), queue_type='ordinary', now=at)
        r = observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        p = prepare_prediction(directory=self.state, version=r['tracking_version'], now=at)
        self.assertFalse(p['receipt']['display']['own_number_in_selected_display'])

    def test_ordinary_numeric_distances_preserve_order_and_are_not_front_tables(self):
        _, prep, _ = self.observe(labels=('14', '10', '10'))
        report = prep['receipt']['display']
        self.assertEqual([x['arithmetic_difference'] for x in report['candidate_number_differences']], [-1, 3, 3])
        self.assertFalse(report['difference_is_front_tables'] or report['queue_order_verified'])

    def test_suffix_or_different_prefix_has_no_fake_numeric_distance(self):
        _, prep, _ = self.observe(labels=('12-1', 'A12', '0012'))
        self.assertEqual([x['arithmetic_difference'] for x in prep['receipt']['display']['candidate_number_differences']], [None, None, 1])
        self.assertEqual(prep['receipt']['display']['selected_display_numbers'][-1], '0012')

    def test_never_seen_absence_cannot_end_an_episode(self):
        _, prep, at = self.observe(labels=('5000', '5001'))
        self.assertEqual(prep['receipt']['display']['state'], 'not_locally_seen')
        self.assertFalse(session_status(directory=self.state, now=at)['terminal'])

    def test_disappearance_requests_status_check_but_never_invents_an_event(self):
        self.observe(labels=('13',))
        _, prep, at = self.observe(30, labels=('1000',))
        report = prep['receipt']['display']
        self.assertEqual(report['state'], 'previously_seen_not_currently_displayed')
        self.assertTrue(report['status_check_suggested'])
        self.assertEqual(report['first_locally_seen_at'], _utc(BASE))
        self.assertFalse(report['called_verified'] or session_status(directory=self.state, now=at)['terminal'])

    def test_same_receipt_read_later_does_not_move_seen_time(self):
        self.observe(labels=('13',))
        view, context, at = self.projections(labels=('13',))
        r = observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        self.assertTrue(r['idempotent']); self.assertEqual(r['tracking_version'], 1)
        context['as_of'], context['expires_at'] = stamp(2), stamp(62)
        r = observe_session(directory=self.state, view=view, context=context, as_of=stamp(2), now=BASE+timedelta(seconds=2))
        p = prepare_prediction(directory=self.state, version=2, now=BASE+timedelta(seconds=2))
        self.assertEqual(p['receipt']['display']['last_locally_seen_at'], _utc(BASE))

    def test_failed_latest_keeps_old_success_unavailable_not_present(self):
        self.observe(labels=('13',))
        _, prep, _ = self.observe(30, labels=('13',), fail='groupqueues?')
        report = prep['receipt']['display']
        self.assertEqual(report['state'], 'data_unavailable')
        self.assertIsNone(report['own_number_in_selected_display'])
        self.assertIsNone(report['selected_display_numbers'])

    def test_saved_history_or_stopped_worker_cannot_be_current_evidence(self):
        for options in ({'saved': True}, {'alive': False}):
            with self.subTest(options=options):
                view, context, at = self.projections(**options)
                from sushiwait.tracking import normalize_observation
                out = normalize_observation(self.ticket, view, context, as_of=stamp(1), now=at)
                self.assertFalse(out['current_display_evidence']); self.assertIsNone(out['queues'])

    def test_locally_stale_view_is_unknown_not_empty_queue(self):
        view, context, at = self.projections()
        at = BASE+timedelta(seconds=121)
        context['as_of'], context['expires_at'] = stamp(121), stamp(181)
        for f in context['features']: f['available_at'] = stamp(121)
        out = observe_session(directory=self.state, view=view, context=context, as_of=stamp(121), now=at)
        prep = prepare_prediction(directory=self.state, version=out['tracking_version'], now=at)
        result = self.prediction(prep, at)
        self.assertEqual(prep['receipt']['display']['state'], 'data_unavailable')
        self.assertEqual(result['unavailable_reason'], 'current_display_evidence_unavailable')

    def test_partial_count_failure_does_not_discard_valid_queue_response(self):
        _, prep, _ = self.observe(fail='storequeuecount?')
        self.assertTrue(prep['receipt']['observation']['current_display_evidence'])
        self.assertIsNone(prep['receipt']['observation']['public_context']['latest_count_received_at'])

    def test_separate_reads_with_different_queue_receipts_are_rejected(self):
        view, context, at = self.projections()
        context['latest_queue_received_at'] = stamp(-1)
        with self.assertRaises(TrackingError) as caught:
            observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        self.assertEqual(caught.exception.error_code, 'tracking_observation_conflict')
        self.assertEqual(session_status(directory=self.state, now=at)['tracking_version'], 0)

    def test_store_queue_or_cutoff_mismatch_rejected(self):
        view, context, at = self.projections()
        for change in ({'store_id': '900002'}, {'queue_type': 'reservation'}, {'as_of': stamp()}):
            with self.assertRaises(TrackingError):
                observe_session(directory=self.state, view=view, context={**context, **change}, as_of=stamp(1), now=at)

    def test_process_revision_reset_with_new_response_is_allowed(self):
        self.observe(); view, context, at = self.projections(30)
        context['observation_revision'] = 0
        r = observe_session(directory=self.state, view=view, context=context, as_of=stamp(31), now=at)
        self.assertEqual(r['tracking_version'], 2)

    def test_same_timestamp_changed_numbers_is_conflict(self):
        self.observe()
        view, context, at = self.projections(labels=('12',))
        context['as_of'], context['expires_at'] = stamp(2), stamp(62)
        with self.assertRaises(TrackingError) as caught:
            observe_session(directory=self.state, view=view, context=context, as_of=stamp(2), now=BASE+timedelta(seconds=2))
        self.assertEqual(caught.exception.error_code, 'tracking_observation_conflict')

    def test_older_asof_rejected_without_overwriting_new_state(self):
        self.observe(30)
        view, context, at = self.projections(30)
        context['as_of'], context['expires_at'] = stamp(30), stamp(90)
        with self.assertRaises(TrackingError):
            observe_session(directory=self.state, view=view, context=context, as_of=stamp(30), now=at)
        self.assertEqual(session_status(directory=self.state, now=at)['tracking_version'], 1)

    def test_unknown_issue_time_has_no_fake_wait_or_zero(self):
        _, prep, at = self.observe(labels=('13',))
        prep['ticket']['issued_at'] = None
        prep['receipt']['ticket_sha256'] = __import__('sushiwait.tracking', fromlist=['_hash'])._hash(prep['ticket'])
        prep['expected_receipt_sha256'] = __import__('sushiwait.tracking', fromlist=['_hash'])._hash(prep['receipt'])
        value = self.prediction(prep, at)
        self.assertEqual(value['unavailable_reason'], 'issued_time_unknown')
        self.assertIsNone(value['fusion']); self.assertIsNone(value['research_call_time_envelopes'])

    def test_absent_history_keeps_missing_state_and_arrival_polling_request(self):
        _, prep, at = self.observe()
        value = self.prediction(prep, at)
        self.assertEqual(value['unavailable_reason'], 'history_not_supplied')
        self.assertEqual(value['monitoring_policy']['requested_interval_seconds'], 300)
        self.assertFalse(value['scheduler_applied'])

    def test_display_membership_and_disappearance_request_30_seconds_without_history(self):
        _, prep, at = self.observe(labels=('13',))
        result = self.prediction(prep, at)
        self.assertEqual(result['polling_request']['requested_interval_seconds'], 30)
        self.assertEqual(result['polling_request']['reason'], 'own_number_in_upcoming_display')
        self.assertFalse(result['research_prediction_available'])
        _, prep, at = self.observe(30, labels=('14',))
        result = self.prediction(prep, at)
        self.assertEqual(result['polling_request']['requested_interval_seconds'], 30)
        self.assertEqual(result['polling_request']['reason'], 'previously_seen_status_uncertain')
        self.assertFalse(result['polling_request']['missed_call_prevention_guaranteed'])

    def test_unknown_arrival_is_not_fabricated_to_request_polling(self):
        _, prep, at = self.observe(labels=('13',))
        from sushiwait.tracking import _hash
        prep['ticket']['desired_arrival_at'] = None
        prep['receipt']['ticket_sha256'] = _hash(prep['ticket']); prep['expected_receipt_sha256'] = _hash(prep['receipt'])
        result = self.prediction(prep, at)
        self.assertIsNone(result['monitoring_policy'])
        self.assertEqual(result['polling_request']['requested_interval_seconds'], 30)

    def test_conditional_candidate_calls_and_early_polling_are_research_only(self):
        _, prep, at = self.observe()
        value = self.prediction(prep, at, fusion_plan=self.candidate(prep))
        self.assertEqual(value['fusion']['wait_quantile_envelopes_us']['p50'], {'lower_us': 299_000_000, 'upper_us': 359_000_000})
        self.assertEqual(value['research_call_time_envelopes']['p50']['lower_us'], _utc(BASE+timedelta(seconds=300)))
        self.assertEqual(value['monitoring_policy']['requested_interval_seconds'], 30)
        self.assertFalse(value['eta_available'] or value['earliest_call_estimate_verified'])

    def test_two_available_scenarios_public_advice_changes_numbers_without_private_leak(self):
        self.observe(0, labels=('10',)); _, prep, at = self.observe(30, labels=('11',))
        plan = self.candidate(prep, fast=True)
        request = public_request(plan, now=at)
        advice = {'schema_version': 1, 'public_input_sha256': request['public_input_sha256'],
            'observation_revision': request['context']['observation_revision'], 'model_version': plan['model_version'],
            'generated_at': stamp(31), 'expires_at': stamp(50), 'weights_ppm': {'history': 0, 'fast': 1_000_000},
            'feature_ids': ['ordinary_removed_labels'], 'reason_code': 'rapid_display_turnover'}
        value = self.prediction(prep, at, fusion_plan=plan, advice=advice)
        self.assertTrue(value['fusion']['ai_numerical_influence_applied'])
        self.assertEqual(value['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'], 69_000_000)
        for item in (self.ticket['episode_id'], self.ticket['issued_at'], self.ticket['desired_arrival_at']):
            self.assertNotIn(item, json.dumps(value['fusion']['public_request']))
        publish_prediction(directory=self.state, preparation=prep, result=value, now=at)

    def test_wrong_elapsed_or_private_context_candidate_is_rejected(self):
        _, prep, at = self.observe()
        for key in ('elapsed', 'context'):
            p = self.candidate(prep)
            if key == 'elapsed': p['candidates'][0]['interval_sample']['elapsed_us'] += 1
            else: p['public_context']['observation_revision'] += 1
            with self.assertRaises(TrackingError): self.prediction(prep, at, fusion_plan=p)

    def test_new_observation_rejects_entire_old_result_including_missing_fallback(self):
        _, prep, at = self.observe(); old = self.prediction(prep, at)
        self.observe(30)
        with self.assertRaises(TrackingError) as caught:
            publish_prediction(directory=self.state, preparation=prep, result=old, now=BASE+timedelta(seconds=31))
        self.assertEqual(caught.exception.error_code, 'tracking_superseded')
        self.assertFalse((self.state/'prediction-0001.json').exists())

    def test_manual_terminal_during_computation_rejects_result_no_business_action(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at)
        out = end_session(directory=self.state, status='called', declared_at=stamp(2), now=BASE+timedelta(seconds=2))
        with self.assertRaises(TrackingError): publish_prediction(directory=self.state, preparation=prep, result=result, now=at)
        self.assertEqual(out['verified_training_labels'], 0); self.assertEqual(out['business_writes'], 0)

    def test_altered_preparation_or_quantiles_cannot_publish(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at, fusion_plan=self.candidate(prep))
        bad = deepcopy(result); bad['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'] = 0
        with self.assertRaises(TrackingError): publish_prediction(directory=self.state, preparation=prep, result=bad, now=at)
        bad = deepcopy(prep); bad['receipt']['display']['state'] = 'called'
        with self.assertRaises(TrackingError): publish_prediction(directory=self.state, preparation=bad, result=result, now=at)

    def test_durable_prediction_idempotence_and_no_overwrite(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at)
        first = publish_prediction(directory=self.state, preparation=prep, result=result, now=at)
        second = publish_prediction(directory=self.state, preparation=prep, result=result, now=at)
        self.assertTrue(first['durability_confirmed'] and second['idempotent'])
        different = deepcopy(result); different['computed_at'] = stamp(2)
        with self.assertRaises(TrackingError):
            publish_prediction(directory=self.state, preparation=prep, result=different, now=BASE+timedelta(seconds=2))

    def test_context_expiry_before_publication_is_rejected(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at, fusion_plan=self.candidate(prep))
        with self.assertRaises(TrackingError) as caught:
            publish_prediction(directory=self.state, preparation=prep, result=result, now=BASE+timedelta(seconds=61))
        self.assertEqual(caught.exception.error_code, 'tracking_superseded')

    def test_publication_clock_is_program_assigned_and_idempotence_keeps_first_receipt(self):
        from sushiwait.tracking import _hash, _unpack_prediction
        _, prep, at = self.observe(); value = self.prediction(prep, at)
        first = BASE+timedelta(seconds=2)
        with patch('sushiwait.tracking._now', return_value=first):
            publish_prediction(directory=self.state, preparation=prep, result=value)
        body = (self.state/'prediction-0001.json').read_bytes()
        result, receipt = _unpack_prediction(json.loads(body), now=first)
        self.assertEqual(result, value)
        self.assertEqual(receipt['first_received_at'], _utc(first))
        self.assertEqual(receipt['result_sha256'], _hash(value))
        self.assertFalse(receipt['independent_time_attestation'])
        publish_prediction(directory=self.state, preparation=prep, result=value, now=BASE+timedelta(seconds=3))
        self.assertEqual((self.state/'prediction-0001.json').read_bytes(), body)
        self.assertNotIn('publication_receipt', value)

    def test_caller_publication_receipt_cannot_override_program_clock(self):
        _, prep, at = self.observe(); value = self.prediction(prep, at)
        value['publication_receipt'] = {'first_received_at': stamp(-100)}
        with self.assertRaises(TrackingError):
            publish_prediction(directory=self.state, preparation=prep, result=value, now=at)
        self.assertFalse((self.state/'prediction-0001.json').exists())

    def test_legacy_prediction_idempotence_does_not_backfill_publication_time(self):
        from sushiwait.tracking import _unpack_prediction
        _, prep, at = self.observe(); value = self.prediction(prep, at)
        with TrackingSession(self.state) as session:
            session._write('prediction-0001.json', value)
        original = (self.state/'prediction-0001.json').read_bytes()
        result = publish_prediction(directory=self.state, preparation=prep, result=value, now=at)
        self.assertTrue(result['idempotent'])
        self.assertEqual((self.state/'prediction-0001.json').read_bytes(), original)
        self.assertIsNone(_unpack_prediction(json.loads(original), now=at)[1])

    def test_altered_publication_receipt_or_result_is_rejected(self):
        from sushiwait.tracking import _unpack_prediction
        _, prep, at = self.observe(); value = self.prediction(prep, at)
        publish_prediction(directory=self.state, preparation=prep, result=value, now=at)
        original = json.loads((self.state/'prediction-0001.json').read_bytes())
        for change in ('future', 'fake_digest', 'auth_claim', 'changed_result'):
            bad = deepcopy(original)
            if change == 'future': bad['publication_receipt']['first_received_at'] = stamp(500)
            if change == 'fake_digest': bad['publication_receipt']['result_sha256'] = '0'*64
            if change == 'auth_claim': bad['publication_receipt']['independent_time_attestation'] = True
            if change == 'changed_result': bad['base_interval_seconds'] = 600
            with self.assertRaises(TrackingError): _unpack_prediction(bad, now=at)

    def test_valid_advice_can_expire_before_context_and_block_publication(self):
        self.observe(0, labels=('10',)); _, prep, at = self.observe(30, labels=('11',))
        plan = self.candidate(prep, fast=True); request = public_request(plan, now=at)
        advice = {'schema_version': 1, 'public_input_sha256': request['public_input_sha256'],
            'observation_revision': request['context']['observation_revision'], 'model_version': plan['model_version'],
            'generated_at': stamp(31), 'expires_at': stamp(40), 'weights_ppm': {'history': 0, 'fast': 1_000_000},
            'feature_ids': ['ordinary_removed_labels'], 'reason_code': 'rapid_display_turnover'}
        result = self.prediction(prep, at, fusion_plan=plan, advice=advice)
        with self.assertRaises(TrackingError) as caught:
            publish_prediction(directory=self.state, preparation=prep, result=result, now=BASE+timedelta(seconds=40))
        self.assertEqual(caught.exception.error_code, 'tracking_superseded')

    def test_actual_thread_new_observation_can_advance_while_calculation_pending(self):
        _, prep, at = self.observe(); ready = threading.Event(); release = threading.Event(); errors = []
        def worker():
            result = self.prediction(prep, at, fusion_plan=self.candidate(prep))
            ready.set(); release.wait(2)
            try: publish_prediction(directory=self.state, preparation=prep, result=result, now=BASE+timedelta(seconds=31))
            except TrackingError as error: errors.append(error.error_code)
        thread = threading.Thread(target=worker); thread.start(); self.assertTrue(ready.wait(2))
        self.observe(30); release.set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, ['tracking_superseded'])
        self.assertFalse((self.state/'prediction-0001.json').exists())

    def test_invalid_advice_file_is_local_numeric_fallback_not_failed_update(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at,
            fusion_plan=self.candidate(prep), advice_error='fusion_invalid_advice')
        self.assertFalse(result['fusion']['advice_accepted'])
        self.assertEqual(result['fusion']['fallback_reason'], 'fusion_invalid_advice')
        publish_prediction(directory=self.state, preparation=prep, result=result, now=at)

    def test_status_expires_numerical_file_and_new_observation_has_no_current_result(self):
        _, prep, at = self.observe(); result = self.prediction(prep, at, fusion_plan=self.candidate(prep))
        publish_prediction(directory=self.state, preparation=prep, result=result, now=at)
        self.assertEqual(session_status(directory=self.state, now=at)['prediction_state'], 'research_only')
        self.assertEqual(session_status(directory=self.state, now=BASE+timedelta(seconds=61))['prediction_state'], 'expired')
        self.observe(70)
        self.assertFalse(session_status(directory=self.state, now=BASE+timedelta(seconds=71))['current_prediction_file_present'])

    def test_directory_lock_excludes_second_writer(self):
        view, context, at = self.projections()
        with TrackingSession(self.state):
            with self.assertRaises(TrackingError) as caught:
                observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        self.assertEqual(caught.exception.error_code, 'tracking_ledger_busy')

    def test_config_change_corrupt_chain_or_missing_receipt_fails_without_reset(self):
        self.observe()
        p = self.state/'observation-0001.json'; original = p.read_bytes()
        for mutation in ('hash', 'gap'):
            if mutation == 'hash':
                value = json.loads(original); value['observation_sha256'] = '0'*64; p.write_text(json.dumps(value))
            else: p.rename(self.state/'observation-0002.json')
            with self.assertRaises(TrackingError): session_status(directory=self.state, now=BASE+timedelta(seconds=1))
            if mutation == 'gap': (self.state/'observation-0002.json').rename(p)
            p.write_bytes(original)
        cfg = self.state/'ticket.json'; v = json.loads(cfg.read_bytes()); v['number'] = '15'; cfg.write_text(json.dumps(v))
        with self.assertRaises(TrackingError): session_status(directory=self.state, now=BASE+timedelta(seconds=1))

    def test_public_permissions_symlinks_hardlinks_and_unknown_files_fail(self):
        p = self.state/'ticket.json'; p.chmod(0o644)
        with self.assertRaises(TrackingError): session_status(directory=self.state, now=BASE)
        p.chmod(0o600); os.link(p, self.input_dir/'copy')
        with self.assertRaises(TrackingError): session_status(directory=self.state, now=BASE)
        (self.input_dir/'copy').unlink()
        (self.state/'leftover.tmp').write_text('private')
        with self.assertRaises(TrackingError): session_status(directory=self.state, now=BASE)

    def test_postcommit_sync_failure_preserves_receipt_and_safe_durability_flag(self):
        view, context, at = self.projections()
        with patch.object(TrackingSession, '_confirm_durable', return_value=False):
            out = observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        self.assertTrue(out['committed']); self.assertFalse(out['durability_confirmed'])
        retry = observe_session(directory=self.state, view=view, context=context, as_of=stamp(1), now=at)
        self.assertTrue(retry['idempotent'] and retry['durability_confirmed'])
        self.assertEqual(retry['tracking_version'], 1)

    def test_terminal_declaration_cannot_revert_or_create_training_label(self):
        self.observe(); end_session(directory=self.state, status='cancelled', declared_at=stamp(2), now=BASE+timedelta(seconds=2))
        with self.assertRaises(TrackingError): self.observe(30)
        with self.assertRaises(TrackingError): end_session(directory=self.state, status='called', declared_at=stamp(3), now=BASE+timedelta(seconds=3))
        out = session_status(directory=self.state, now=BASE+timedelta(seconds=3))
        self.assertEqual(out['prediction_state'], 'terminal'); self.assertEqual(out['verified_training_labels'], 0)

    def test_cli_all_five_paths_private_socket_zero_and_no_personal_stdout(self):
        state = self.root/'cli'; state.mkdir(mode=0o700)
        view, context, at = self.projections()
        paths = []
        for name, value in [('ticket', self.ticket), ('view', view), ('context', context)]:
            p = self.input_dir/(name+'.json'); p.write_text(json.dumps(value)); p.chmod(0o600); paths.append(p)
        commands = [['ticket-track-init','--ticket-file',str(paths[0]),'--state-dir',str(state)],
            ['ticket-track-observe','--view-file',str(paths[1]),'--context-file',str(paths[2]),'--as-of',stamp(1),'--state-dir',str(state)],
            ['ticket-track-predict','--version','1','--state-dir',str(state)],
            ['ticket-track-status','--state-dir',str(state)],
            ['ticket-track-end','--status','ended','--declared-at',stamp(2),'--state-dir',str(state)]]
        with patch('sushiwait.tracking._now',return_value=BASE+timedelta(seconds=2)), \
                patch('socket.socket',side_effect=AssertionError('tracking_socket')) as sockets, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('tracking_credentials')) as auth:
            for command in commands:
                output = io.StringIO()
                with contextlib.redirect_stdout(output): self.assertEqual(main(command), 0)
                value = json.loads(output.getvalue()); self.assertFalse(value['eta_available'])
                for marker in (self.ticket['episode_id'], self.ticket['issued_at'], str(self.root)):
                    self.assertNotIn(marker, output.getvalue())
        self.assertEqual(sockets.call_count, 0); self.assertEqual(auth.call_count, 0)


class TrackingHistoryTests(TrackingTests):
    """Override loader below to run only the new integration case, not inherited tests."""
    def test_reviewed_history_remains_conditional_and_updates_after_new_observation(self):
        fixture = baseline.ReviewedBaselineTests(methodName='test_exact_date_hour_matching_and_honest_research_state')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        _, prep, at = self.observe(3)
        with OutcomeIntakeStore(fixture.source_path, read_only=True) as source, \
                OutcomeReviewStore(fixture.review_path, read_only=True) as reviews:
            first = self.prediction(prep, at, source=source, reviews=reviews)
        self.assertTrue(first['research_prediction_available'])
        self.assertEqual(first['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'], 296_000_000)
        publish_prediction(directory=self.state, preparation=prep, result=first, now=at)
        _, prep, at = self.observe(30)
        with OutcomeIntakeStore(fixture.source_path, read_only=True) as source, \
                OutcomeReviewStore(fixture.review_path, read_only=True) as reviews:
            second = self.prediction(prep, at, source=source, reviews=reviews)
        self.assertEqual(second['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'], 269_000_000)
        self.assertEqual(first['research_call_time_envelopes'], second['research_call_time_envelopes'])
        self.assertFalse(second['eta_available'])


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(TrackingTests)
    suite.addTest(TrackingHistoryTests('test_reviewed_history_remains_conditional_and_updates_after_new_observation'))
    return suite
