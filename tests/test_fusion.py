"""Mixture math, uncertainty envelopes, public data isolation and late advice."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import io
from itertools import product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.fusion import FusionError, context_from_history, fuse, public_request, validate_plan
from sushiwait.remoteservice import LiveRemoteView, RemoteASGI, RemoteQueueService
from sushiwait.queuebinding import POLICY as QUEUE_BINDING_POLICY
import test_collection_monitor as monitoring

BASE = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)


def stamp(second=0):
    return (BASE+timedelta(seconds=second)).isoformat()


def atom(lower, upper, mass=1_000_000):
    return {'lower_us': lower, 'upper_us': upper, 'mass_ppm': mass}


def plan():
    return {'schema_version': 1, 'data_origin': 'synthetic', 'model_version': 'synthetic-v1',
        'prediction_target': 'new_join_total', 'conditioning': 'new_join', 'ai_blend_ppm': 500_000,
        'public_context': {'schema_version': 2, 'queue_binding_policy': QUEUE_BINDING_POLICY,
            'source': 'crm_remote_v1_1', 'store_id': '900001',
            'queue_type': 'ordinary', 'observation_revision': 3, 'as_of': stamp(),
            'expires_at': stamp(60), 'window_seconds': 300, 'max_local_age_seconds': 90,
            'latest_queue_received_at': stamp(-5), 'latest_count_received_at': stamp(-4),
            'features': [{'feature_id': 'ordinary_removed_labels', 'value': 3, 'available_at': stamp(-5)},
                         {'feature_id': 'reported_count_raw', 'value': None, 'available_at': stamp(-4)}],
            'source_freshness': 'unknown', 'count_unit': 'unknown', 'store_identity_verified': False,
            'collector_running': True, 'latest_queue_origin': 'worker_commit'},
        'candidates': [{'candidate_id': 'history', 'atoms': [atom(100, 100)]},
                       {'candidate_id': 'fast', 'atoms': [atom(0, 0)]}],
        'prior_weights_ppm': {'history': 1_000_000, 'fast': 0}}


def advice(value=None):
    p = plan() if value is None else value
    return {'schema_version': 1, 'public_input_sha256': public_request(p, now=BASE)['public_input_sha256'],
        'observation_revision': p['public_context']['observation_revision'], 'model_version': p['model_version'],
        'generated_at': stamp(1), 'expires_at': stamp(50),
        'weights_ppm': {'history': 0, 'fast': 1_000_000},
        'feature_ids': ['ordinary_removed_labels'], 'reason_code': 'rapid_display_turnover'}


class FusionMathTests(unittest.TestCase):
    def test_mix_cdf_then_quantiles_not_average_of_quantiles(self):
        result = fuse(plan(), advice=advice(), now=BASE+timedelta(seconds=2))
        self.assertTrue(result['advice_accepted'])
        self.assertTrue(result['ai_numerical_influence_applied'])
        self.assertEqual(result['wait_quantile_envelopes_us']['p50'], {'lower_us': 0, 'upper_us': 0})
        self.assertEqual(result['wait_quantile_envelopes_us']['p90'], {'lower_us': 100, 'upper_us': 100})
        self.assertEqual(result['effective_weight_numerators'], {'fast': 500_000_000_000, 'history': 500_000_000_000})
        self.assertFalse(result['eta_available'])
        self.assertFalse(result['coverage_calibrated'])
        self.assertFalse(result['provider_called'])

    def test_interval_envelopes_cover_every_small_latent_assignment(self):
        p = plan()
        p['candidates'] = [{'candidate_id': 'history', 'atoms': [atom(0, 1, 500_000), atom(2, 4, 500_000)]},
                           {'candidate_id': 'fast', 'atoms': [atom(1, 3)]}]
        p['prior_weights_ppm'] = {'history': 250_000, 'fast': 750_000}
        bounds = fuse(p, now=BASE)['wait_quantile_envelopes_us']
        for assignment in product(range(2), range(2, 5), range(1, 4)):
            distribution = sorted(zip(assignment, [Fraction(1, 8), Fraction(1, 8), Fraction(3, 4)]))
            for name, q in [('p10', Fraction(1, 10)), ('p50', Fraction(1, 2)), ('p90', Fraction(9, 10))]:
                truth = next(t for t, _ in distribution if sum(m for s, m in distribution if s <= t) >= q)
                self.assertLessEqual(bounds[name]['lower_us'], truth)
                self.assertGreaterEqual(bounds[name]['upper_us'], truth)

    def test_unbounded_support_stays_unknown_instead_of_artificial_cutoff(self):
        p = plan()
        p['candidates'][0]['atoms'] = [atom(0, 0, 300_000), atom(100, None, 700_000)]
        result = fuse(p, now=BASE)
        self.assertEqual(result['wait_quantile_envelopes_us']['p10'], {'lower_us': 0, 'upper_us': 0})
        self.assertEqual(result['wait_quantile_envelopes_us']['p50'], {'lower_us': 100, 'upper_us': None})
        self.assertTrue(result['unbounded_upper_support'])

    def test_zero_weight_candidate_does_not_change_quantiles(self):
        p = plan()
        p['candidates'][1]['atoms'] = [atom(0, None)]
        result = fuse(p, now=BASE)
        self.assertEqual(result['wait_quantile_envelopes_us']['p90']['upper_us'], 100)
        self.assertFalse(result['unbounded_upper_support'])

    def test_explicit_zero_ai_blend_accepts_advice_but_retains_prior(self):
        p = plan(); p['ai_blend_ppm'] = 0
        result = fuse(p, advice=advice(p), now=BASE+timedelta(seconds=2))
        self.assertTrue(result['advice_accepted'])
        self.assertFalse(result['ai_numerical_influence_applied'])
        self.assertEqual(result['wait_quantile_envelopes_us']['p50']['lower_us'], 100)

    def test_full_research_blend_can_use_scenario_distribution(self):
        p = plan(); p['ai_blend_ppm'] = 1_000_000
        result = fuse(p, advice=advice(p), now=BASE+timedelta(seconds=2))
        self.assertEqual(result['wait_quantile_envelopes_us']['p90']['upper_us'], 0)
        self.assertFalse(result['model_performance_verified'])

    def test_order_and_equivalent_timezone_do_not_change_public_binding_or_quantiles(self):
        p = plan(); q = deepcopy(p)
        q['candidates'].reverse(); q['public_context']['features'].reverse()
        q['public_context']['as_of'] = '2026-10-08T12:00:00+08:00'
        self.assertEqual(public_request(p, now=BASE), public_request(q, now=BASE))
        self.assertEqual(fuse(p, now=BASE)['wait_quantile_envelopes_us'], fuse(q, now=BASE)['wait_quantile_envelopes_us'])


class FusionAdviceTests(unittest.TestCase):
    def calculate(self, a, p=None, second=2, **kwargs):
        return fuse(plan() if p is None else p, advice=a, now=BASE+timedelta(seconds=second), **kwargs)

    def test_absent_advice_falls_back(self):
        result = self.calculate(None)
        self.assertEqual(result['fallback_reason'], 'fusion_advice_missing')
        self.assertEqual(result['wait_quantile_envelopes_us']['p50']['lower_us'], 100)

    def test_bad_sum_negative_float_bool_or_unknown_candidate_falls_back(self):
        for weights in [{'history': 0, 'fast': 999999}, {'history': -1, 'fast': 1000001},
                        {'history': 0.0, 'fast': 1000000}, {'history': False, 'fast': 1000000},
                        {'history': 0, 'fast': 1000000, 'untrusted': 0}]:
            a = advice(); a['weights_ppm'] = weights
            result = self.calculate(a)
            self.assertEqual(result['fallback_reason'], 'fusion_invalid_advice')
            self.assertFalse(result['ai_numerical_influence_applied'])

    def test_digest_revision_model_mismatch_or_boolean_revision_falls_back(self):
        for key, replacement in [('public_input_sha256', '0'*64), ('observation_revision', 2),
                                 ('observation_revision', True), ('model_version', 'other')]:
            a = advice(); a[key] = replacement
            self.assertEqual(self.calculate(a)['fallback_reason'], 'fusion_advice_input_mismatch')

    def test_expiry_boundary_future_generation_or_excess_expiry_falls_back(self):
        a = advice()
        self.assertEqual(self.calculate(a, second=50)['fallback_reason'], 'fusion_advice_expired')
        for key, value in [('generated_at', stamp(3)), ('generated_at', stamp(-1)), ('expires_at', stamp(61))]:
            a = advice(); a[key] = value
            self.assertEqual(self.calculate(a)['fallback_reason'], 'fusion_advice_clock_invalid')

    def test_stale_or_missing_queue_does_not_accept_ai(self):
        for received in (None, stamp(-91)):
            p = plan(); p['public_context']['latest_queue_received_at'] = received
            self.assertEqual(self.calculate(advice(p), p)['fallback_reason'], 'fusion_stale_queue')

    def test_unknown_null_duplicate_or_insufficient_evidence_falls_back(self):
        for refs in [[], ['reported_count_raw'], ['untrusted'], ['ordinary_removed_labels']*2]:
            a = advice(); a['feature_ids'] = refs
            self.assertEqual(self.calculate(a)['fallback_reason'], 'fusion_advice_insufficient_evidence')
        a = advice(); a['reason_code'] = 'insufficient_evidence'
        self.assertEqual(self.calculate(a)['fallback_reason'], 'fusion_advice_insufficient_evidence')

    def test_advice_cannot_override_unknown_source_or_add_free_text(self):
        for key, value in [('source_freshness', 'verified'), ('actual_called_at', stamp()),
                           ('reason', 'private-marker')]:
            a = advice(); a[key] = value
            result = self.calculate(a)
            self.assertEqual(result['fallback_reason'], 'fusion_invalid_advice')
            self.assertEqual(result['source_freshness'], 'unknown')
            self.assertNotIn('private-marker', json.dumps(result))

    def test_new_input_rejects_entire_older_result_including_prior_fallback(self):
        for params in [{'current_observation_revision': 4}, {'current_public_input_sha256': '0'*64}]:
            with self.assertRaises(FusionError) as caught:
                self.calculate(None, **params)
            self.assertEqual(str(caught.exception), 'fusion_superseded_input')
        request = public_request(plan(), now=BASE)
        result = self.calculate(advice(), current_observation_revision=3,
                                current_public_input_sha256=request['public_input_sha256'])
        self.assertTrue(result['current_input_guard_applied'])
        self.assertFalse(result['input_currentness_verified'])


class FusionInputTests(unittest.TestCase):
    def test_public_request_excludes_private_distribution_values_and_target(self):
        p = plan(); p['candidates'][0]['atoms'] = [atom(123456789, 123456789)]
        body = json.dumps(public_request(p, now=BASE))
        for marker in ('123456789', 'atoms', 'ai_blend_ppm', 'prior_weights_ppm', 'prediction_target', 'conditioning'):
            self.assertNotIn(marker, body)

    def test_private_number_or_itinerary_fields_cannot_enter_public_context(self):
        for key in ('own_number', 'issued_at', 'desired_arrival_at', 'authorization'):
            p = plan(); p['public_context'][key] = 'private-marker'
            with self.assertRaises(FusionError) as caught:
                validate_plan(p, now=BASE)
            self.assertEqual(str(caught.exception), 'fusion_invalid_plan')

    def test_future_context_response_or_feature_is_invalid(self):
        for location in ('as_of', 'latest_queue_received_at', 'feature'):
            p = plan()
            if location == 'feature': p['public_context']['features'][0]['available_at'] = stamp(1)
            else: p['public_context'][location] = stamp(1)
            with self.assertRaises(FusionError): validate_plan(p, now=BASE)

    def test_remaining_distribution_requires_explicit_conditional_semantics(self):
        p = plan(); p['prediction_target'] = 'remaining'
        with self.assertRaises(FusionError): validate_plan(p, now=BASE)
        p['conditioning'] = 'caller_supplied_conditional'
        result = fuse(p, now=BASE)
        self.assertEqual(result['plan']['conditioning'], 'caller_supplied_conditional')
        self.assertFalse(result['eta_available'])

    def test_bad_mass_bounds_empty_candidates_or_probability_types_are_invalid(self):
        mutations = [lambda p: p.update(candidates=[]),
                     lambda p: p['candidates'][0].update(atoms=[atom(2, 1)]),
                     lambda p: p['candidates'][0].update(atoms=[atom(True, 3)]),
                     lambda p: p['candidates'][0].update(atoms=[atom(0, 0, 999999)]),
                     lambda p: p.update(ai_blend_ppm=0.5),
                     lambda p: p['public_context'].update(source_freshness='verified')]
        for change in mutations:
            p = plan(); change(p)
            with self.assertRaises(FusionError): validate_plan(p, now=BASE)


class FusionContextTests(unittest.TestCase):
    def history(self, records, second=65, *, state='running', alive=True, saved=False):
        view = LiveRemoteView(['900001'], stale_after_seconds=120)
        for record in records: view.publish(record, saved_history=saved)
        at = monitoring.helpers.BASE+timedelta(seconds=second)
        return view.monitor_history('900001', now=at, service_state=state, worker_alive=alive), at

    def context(self, history, at, **kwargs):
        return context_from_history(history, queue_type='ordinary', now=at, **kwargs)

    def test_real_projection_aggregates_distinct_removals_without_private_labels(self):
        history, at = self.history([monitoring.record(labels=('10', '10', '11-1')),
                                   monitoring.record(30, labels=('5000',)), monitoring.record(60, labels=('5001',))])
        context = self.context(history, at)
        fields = {f['feature_id']: f['value'] for f in context['features']}
        self.assertEqual(fields['ordinary_removed_labels'], 3)
        self.assertEqual(fields['ordinary_comparable_pairs'], 2)
        self.assertEqual(fields['longest_gap_seconds'], 30)
        self.assertEqual(context['observation_revision'], 3)
        body = json.dumps(context)
        self.assertNotIn('5000', body); self.assertNotIn('11-1', body)
        self.assertNotIn('queries', body)
        self.assertEqual(context['source_freshness'], 'unknown')
        p = plan(); p['public_context'] = context
        request = public_request(p, now=at)
        self.assertIn('iso_weekday', request['calendar_at_observation'])
        self.assertFalse(request['calendar_at_observation']['eta_available'])

    def test_failure_gap_and_partial_pair_are_not_inferred_as_turnover(self):
        history, at = self.history([monitoring.record(), monitoring.record(30, fail='groupqueues?'),
                                   monitoring.record(60, labels=('7000',), fail='storequeuecount?')])
        context = self.context(history, at)
        fields = {f['feature_id']: f['value'] for f in context['features']}
        self.assertIsNone(fields['ordinary_removed_labels']); self.assertEqual(fields['ordinary_comparable_pairs'], 0)
        self.assertIsNone(fields['reported_count_raw'])
        self.assertEqual(fields['groupqueues_failures'], 1)
        self.assertEqual(fields['storequeuecount_failures'], 1)
        self.assertIsNotNone(context['latest_queue_received_at'])
        self.assertIsNone(context['latest_count_received_at'])

    def test_both_endpoints_must_be_in_window(self):
        history, at = self.history([monitoring.record(), monitoring.record(60)], second=65)
        context = self.context(history, at, window_seconds=30)
        fields = {f['feature_id']: f['value'] for f in context['features']}
        self.assertEqual(fields['ordinary_comparable_pairs'], 0)
        self.assertIsNone(fields['ordinary_removed_labels'])

    def test_empty_or_latest_failed_queue_is_unavailable_not_zero(self):
        for records in [[], [monitoring.record(), monitoring.record(30, fail='groupqueues?')]]:
            history, at = self.history(records)
            context = self.context(history, at)
            self.assertIsNone(context['latest_queue_received_at'])
            self.assertEqual(context['latest_queue_origin'], 'unavailable')
            p = plan(); p['public_context'] = context
            self.assertEqual(validate_plan(p, now=at)['public_context']['observation_revision'], len(records))

    def test_stopped_or_saved_history_cannot_be_ai_current_evidence(self):
        for state, alive, saved, reason in [('completed', False, False, 'fusion_collector_not_running'),
                                         ('running', True, True, 'fusion_saved_history_only')]:
            history, at = self.history([monitoring.record(60)], state=state, alive=alive, saved=saved)
            p = plan(); p['public_context'] = self.context(history, at)
            a = advice(p)
            a['generated_at'] = (at+timedelta(seconds=1)).isoformat()
            a['expires_at'] = (at+timedelta(seconds=50)).isoformat()
            a['feature_ids'] = ['ordinary_comparable_pairs']
            result = fuse(p, advice=a, now=at+timedelta(seconds=2))
            self.assertEqual(result['fallback_reason'], reason)

    def test_future_response_or_bad_projection_counter_is_rejected(self):
        history, at = self.history([monitoring.record(60)])
        future, counter = deepcopy(history), deepcopy(history)
        future['points'][0]['queries']['groupqueues']['received_at'] = (at+timedelta(seconds=1)).isoformat()
        counter['retained_points'] = 5
        for bad in [future, counter]:
            with self.assertRaises(FusionError): self.context(bad, at)

    def test_nonincreasing_count_response_does_not_become_current_value(self):
        first, second = monitoring.record(), monitoring.record(30, count=999)
        for field in ('started_at', 'received_at'):
            first['queries']['storequeuecount'][field] = (monitoring.helpers.BASE+timedelta(seconds=30)).isoformat()
        history, at = self.history([first, second])
        context = self.context(history, at)
        self.assertIsNone(context['latest_count_received_at'])
        self.assertIsNone(next(f['value'] for f in context['features'] if f['feature_id'] == 'reported_count_raw'))

    def test_long_gap_stays_a_gap_and_not_a_removal_rate(self):
        history, at = self.history([monitoring.record(), monitoring.record(121, labels=('5000',))], second=125)
        context = self.context(history, at)
        fields = {f['feature_id']: f['value'] for f in context['features']}
        self.assertEqual(fields['longest_gap_seconds'], 121)
        self.assertEqual(fields['ordinary_comparable_pairs'], 0)
        self.assertIsNone(fields['ordinary_removed_labels'])

    def test_read_only_asgi_context_does_not_touch_database_transport_or_credentials(self):
        import asyncio
        with tempfile.TemporaryDirectory() as directory:
            service = RemoteQueueService(db=Path(directory)/'never.sqlite3', task_file=Path(directory)/'never.json',
                                         store_ids=['900001'])
            service.view.publish(monitoring.record())
            app = RemoteASGI(service)
            with asyncio.Runner() as runner:
                with patch('socket.socket', side_effect=AssertionError('network')), \
                     patch('sqlite3.connect', side_effect=AssertionError('db')), \
                     patch('sushiwait.remoteservice.RemoteClient', side_effect=AssertionError('upstream')), \
                     patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('auth')):
                    code, body, _ = runner.run(monitoring.raw_request(app, '/api/v1/stores/900001/fusion-context'))
                    self.assertEqual(code, 200)
                    result = json.loads(body)
                    self.assertEqual(set(result['contexts']), {'ordinary', 'reservation'})
                    self.assertFalse(result['contexts']['ordinary']['collector_running'])
                    self.assertFalse(result['network_performed_by_read'] or result['eta_available'])
                    self.assertEqual(runner.run(monitoring.raw_request(app, '/api/v1/stores/900002/fusion-context'))[0], 404)
                    self.assertEqual(runner.run(monitoring.raw_request(app, '/api/v1/stores/900001/fusion-context', query=b'x=1'))[0], 400)
            self.assertEqual(list(Path(directory).iterdir()), [])


class FusionCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve(); self.folder.chmod(0o700)
        self.input = self.folder/'plan.json'; self.reply = self.folder/'advice.json'
        self.output = self.folder/'result.json'
        self.input.write_text(json.dumps(plan())); self.input.chmod(0o600)
        self.reply.write_text(json.dumps(advice())); self.reply.chmod(0o600)

    def invoke(self, extra=()):
        output = io.StringIO()
        with patch('sushiwait.fusion._clock', return_value=BASE+timedelta(seconds=2)), \
             patch('socket.socket', side_effect=AssertionError('socket')) as sockets, \
             patch('subprocess.Popen', side_effect=AssertionError('child')) as children, \
             patch('sushiwait.cli.client_for', side_effect=AssertionError('client')) as clients, \
             patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')) as auth, \
             contextlib.redirect_stdout(output):
            code = main(['fusion-research', '--input', str(self.input), '--output', str(self.output), *extra])
        self.assertFalse(sockets.called or children.called or clients.called or auth.called)
        self.assertNotIn(str(self.folder), output.getvalue())
        return code, json.loads(output.getvalue())

    def test_private_output_real_command_and_input_bytes_preserved(self):
        before = self.input.read_bytes(), self.reply.read_bytes()
        code, summary = self.invoke(['--advice', str(self.reply)])
        self.assertEqual(code, 0); self.assertTrue(summary['ai_numerical_influence_applied'])
        self.assertFalse(summary['provider_called']); self.assertFalse(summary['eta_available'])
        self.assertNotIn('wait_quantile_envelopes_us', summary)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, (self.input.read_bytes(), self.reply.read_bytes()))
        result = json.loads(self.output.read_text())
        self.assertEqual(result['wait_quantile_envelopes_us']['p50']['lower_us'], 0)

    def test_duplicate_nan_empty_or_missing_advice_falls_back_with_redacted_error(self):
        for body in ['{"schema_version":1,"schema_version":2}', '{"private-marker":NaN}', '']:
            self.reply.write_text(body)
            if self.output.exists(): self.output.unlink()
            code, summary = self.invoke(['--advice', str(self.reply)])
            self.assertEqual(code, 0)
            self.assertEqual(summary['fallback_reason'], 'fusion_invalid_advice')
            self.assertFalse(summary['ai_numerical_influence_applied'])
            self.assertNotIn('private-marker', json.dumps(summary))
        self.reply.unlink(); self.output.unlink()
        self.assertEqual(self.invoke(['--advice', str(self.reply)])[1]['fallback_reason'], 'fusion_invalid_advice')

    def test_no_advice_and_no_overwrite(self):
        self.assertEqual(self.invoke()[0], 0)
        before = self.output.read_bytes()
        code, summary = self.invoke()
        self.assertEqual(code, 1); self.assertEqual(summary['error_code'], 'fusion_output_exists')
        self.assertEqual(before, self.output.read_bytes())

    def test_private_file_permissions_and_conflicting_paths_fail_without_output(self):
        self.input.chmod(0o644)
        self.assertEqual(self.invoke()[1]['error_code'], 'fusion_invalid_input')
        self.assertFalse(self.output.exists())
        self.input.chmod(0o600)
        self.assertEqual(self.invoke(['--advice', str(self.input)])[1]['error_code'], 'fusion_invalid_input')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
