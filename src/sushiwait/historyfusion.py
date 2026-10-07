"""Cutoff-known reviewed interval claims into private empirical fusion plans.

No population fitting, authentic event labels, censoring correction, provider
calls, ticket actions or production ETA are supplied by this research bridge.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib

from .baseline import (BaselineError, _features, _forecast, _GROUPS, _matching,
                       _prepare, _us, validate_plan)
from .credentials import _read_private_file
from .fusion import FusionError, fuse, validate_plan as validate_fusion_plan
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet
from .reviews import ReviewError

POLICY = 'cutoff_reviewed_empirical_interval_fusion_v1'
MODEL = 'reviewed-history-intervals-v1'
_CODES = {'history_fusion_invalid_input', 'history_fusion_invalid_plan',
    'history_fusion_scope_mismatch', 'history_fusion_source_invalid',
    'history_fusion_support_limit', 'history_fusion_output_exists', 'history_fusion_failed'}


class HistoryFusionError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if type(code) is str and code in _CODES else 'history_fusion_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def read_context(path):
    try:
        return _json(_read_private_file(path))
    except Exception:
        raise HistoryFusionError('history_fusion_invalid_input') from None


def _fusion_template(plan, context, intervals, elapsed, model_version, blend):
    counts = Counter(intervals)
    if len(counts) > 128:
        raise HistoryFusionError('history_fusion_support_limit')
    candidate = {'candidate_id': 'history', 'interval_sample': {
        'elapsed_us': elapsed, 'intervals': [[lower, upper, n]
            for (lower, upper), n in sorted(counts.items())]}}
    value = {'schema_version': 2, 'data_origin': 'synthetic' if plan['data_origin'] == 'synthetic' else 'research',
        'model_version': model_version, 'prediction_target': 'new_join_total' if elapsed is None else 'remaining',
        'conditioning': 'new_join' if elapsed is None else 'call_not_observed_after_elapsed',
        'public_context': context, 'candidates': [candidate],
        'prior_weights_ppm': {'history': 1_000_000}, 'ai_blend_ppm': blend}
    try:
        return validate_fusion_plan(value)
    except FusionError:
        raise HistoryFusionError('history_fusion_invalid_plan') from None


def build_history_fusion(*, source, reviews, plan, context, model_version=MODEL,
                        ai_blend_ppm=0, max_revisions=10_000):
    """All three baseline modes; complete caller grid, no truncation of claims.

    The history scenario is real arithmetic on eligible reviewed claims, not
    authenticated training or the still-unfitted steady/fast/slow scenarios.
    """
    try:
        plan = validate_plan(plan)
        if (type(max_revisions) is not int or not 1 <= max_revisions <= 10_000
                or type(ai_blend_ppm) is not int or not 0 <= ai_blend_ppm <= 1_000_000):
            raise HistoryFusionError('history_fusion_invalid_plan')
        # Validate the public whitelist even when no historical sample exists.
        check = _fusion_template(plan, context, [(0, 0)], None, model_version, ai_blend_ppm)
        context = check['public_context']
        if (context['store_id'] != plan['store_id'] or context['queue_type'] != plan['queue_type']
                or _time(context['as_of']) != _time(plan['as_of'])):
            raise HistoryFusionError('history_fusion_scope_mismatch')
        audit, candidates = reviews._selection(source=source, as_of=plan['as_of'],
            data_origin=plan['data_origin'], api_profile=plan['api_profile'], max_revisions=max_revisions)
        pool, excluded = _prepare(candidates, plan)
        historical_hash = hashlib.sha256(_canonical({'policy': POLICY, 'plan': plan,
            'calendar_at_cutoff': _features(plan['as_of'], as_of=plan['as_of']),
            'pool': sorted(pool, key=lambda item: item['source_receipt_sha256'])}).encode()).hexdigest()
        if plan['mode'] == 'ideal_time':
            issue_times = plan['candidate_issue_times']; elapsed = None
        else:
            issue_times = [plan['issued_at'] if plan['mode'] == 'remaining' else plan['as_of']]
            elapsed = _us(_time(plan['as_of'])-_time(plan['issued_at'])) if plan['mode'] == 'remaining' else None
        entries = []
        target = (_time(plan['desired_arrival_at'])+timedelta(minutes=plan['call_offset_minutes'])
                  if plan['mode'] == 'ideal_time' else None)
        eligible = []
        for index, issued in enumerate(issue_times):
            forecast = _forecast(pool, plan, issued, elapsed_us=elapsed)
            template, fusion, distance = None, None, None
            if forecast['research_forecast_available']:
                keys = next(keys for group, keys in _GROUPS if group == forecast['matched_group'])
                matched = _matching(pool, forecast['target_date_features'], keys)
                intervals = [item['interval'] for item in matched]
                template = _fusion_template(plan, context, intervals, elapsed, model_version, ai_blend_ppm)
                fusion = fuse(template)
                # Retain all intervals/counts, not quantile-derived pseudo-atoms.
                if fusion['wait_quantile_envelopes_us'] != forecast['wait_quantile_envelopes_us']:
                    raise HistoryFusionError('history_fusion_failed')
                base = _time(plan['as_of']) if elapsed is not None else _time(issued)
                median = fusion['wait_quantile_envelopes_us']['p50']
                lo, hi = (base+timedelta(microseconds=median[key]) for key in ('lower_us', 'upper_us'))
                if target is not None:
                    distance = {'lower_us': max(0, _us(lo-target), _us(target-hi)),
                        'upper_us': max(abs(_us(lo-target)), abs(_us(hi-target)))}
                    eligible.append((distance['upper_us'], index))
            entries.append({'candidate_index': index, 'candidate_issue_at': issued,
                'forecast': forecast, 'fusion_plan': template,
                'history_only_fusion_quantiles_us': fusion['wait_quantile_envelopes_us'] if fusion else None,
                'median_distance_to_target_us': distance})
        selected = min(eligible)[1] if eligible else None
        return {'history_fusion_schema_version': 1, 'policy': POLICY, 'plan': plan,
            'historical_input_sha256': historical_hash, 'model_version': model_version,
            'public_context': context, 'eligible_static_cohort_samples': len(pool),
            'excluded_samples': excluded, 'candidates': entries,
            'research_candidate_available': any(e['fusion_plan'] is not None for e in entries),
            'selected_candidate_index': selected,
            'selection_method': 'minimum_worst_median_envelope_distance_then_earliest_candidate' if target is not None else None,
            'search_scope': 'caller_candidates_only' if target is not None else None,
            'global_optimality_verified': False, 'candidate_bookability_verified': False,
            'monotonicity_assumed': False, 'future_queue_observations_used': False,
            'scenario_ids_generated': ['history'] if any(e['fusion_plan'] is not None for e in entries) else [],
            'realtime_regime_models_fitted': False, 'provider_called': False,
            'ai_numerical_influence_applied': False,
            'audit_diagnostics_not_features': audit,
            'availability_basis': 'local_source_and_review_first_receipts',
            'authenticity_verified': False, 'verified_training_labels': 0,
            'training_eligible': False, 'right_censoring_modelled': False,
            'population_representativeness_verified': False,
            'coverage_calibrated': False, 'model_performance_verified': False,
            'eta_available': False, 'source_freshness': 'unknown',
            'network_performed': False, 'booking_or_cancellation_performed': False,
            'output_requires_private_handling': True}
    except HistoryFusionError:
        raise
    except BaselineError:
        raise HistoryFusionError('history_fusion_invalid_plan') from None
    except ReviewError:
        raise HistoryFusionError('history_fusion_source_invalid') from None
    except Exception:
        raise HistoryFusionError('history_fusion_failed') from None


def write_history_fusion(*, destination, **options):
    result = build_history_fusion(**options)
    body = _canonical(result).encode()
    if len(body) > 2_097_152:
        raise HistoryFusionError('history_fusion_support_limit')
    try:
        publication = _write_packet(body, destination)
    except PacketError as error:
        raise HistoryFusionError('history_fusion_output_exists' if error.error_code == 'packet_output_exists'
                                else 'history_fusion_failed', committed=error.committed) from None
    return {'artifact_written': True, 'committed': True,
        'durability_confirmed': publication['durability_confirmed'], 'mode': result['plan']['mode'],
        'candidate_count': len(result['candidates']),
        'research_candidate_available': result['research_candidate_available'],
        'selected_candidate_index': result['selected_candidate_index'],
        'scenario_ids_generated': result['scenario_ids_generated'],
        'realtime_regime_models_fitted': False, 'provider_called': False,
        'verified_training_labels': 0, 'eta_available': False, 'network_performed': False,
        'output_requires_private_handling': True}
