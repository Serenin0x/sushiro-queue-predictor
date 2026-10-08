"""Chronological, paired replay of reviewed history and realtime neighbors.

Historical first receipts constrain input availability. Reconstructed target
requests and reviewed claims are not authenticated production forecast logs.
No saved observation is promoted to a running collector or sent to a provider.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
from uuid import uuid4

from .baseline import _us
from .credentials import _read_private_file
from .evaluation import evaluate_intervals
from .features import FeatureError, build_feature_dataset, validate_feature_plan
from .fusion import fuse
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet
from .realtimefusion import MODEL, RealtimeFusionError, build_realtime_fusion
from .remote import SOURCE

POLICY = 'cutoff_known_paired_realtime_replay_v1'
VARIANTS = ('history_only', 'realtime_only', 'prior_fusion')
_OPTIONS = {'minimum_samples', 'neighbors', 'minimum_episodes',
    'maximum_elapsed_difference_seconds', 'maximum_standardized_distance', 'realtime_weight_ppm'}
_FEATURE_FIELDS = {'schema_version', 'as_of', 'data_origin', 'queue_data_origin',
    'api_profile', 'store_id', 'elapsed_seconds', 'max_cases', 'window_seconds', 'max_gap_seconds'}


class RealtimeBacktestError(ValueError):
    def __init__(self, code='realtime_backtest_failed', *, committed=False):
        self.error_code = code if code in {'realtime_backtest_invalid_plan',
            'realtime_backtest_invalid_input', 'realtime_backtest_source_invalid',
            'realtime_backtest_output_exists', 'realtime_backtest_failed'} else 'realtime_backtest_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def validate_plan(value, *, now=None):
    try:
        if type(value) is not dict or set(value) != _FEATURE_FIELDS | _OPTIONS:
            raise ValueError
        feature = validate_feature_plan({k:value[k] for k in _FEATURE_FIELDS}, now=now)
        if (any(type(value[k]) is not int for k in _OPTIONS)
                or not 1 <= value['minimum_samples'] <= 1000
                or not 1 <= value['minimum_episodes'] <= value['neighbors'] <= 100
                or not 0 <= value['maximum_elapsed_difference_seconds'] <= 3600
                or not 1 <= value['maximum_standardized_distance'] <= 20
                or not 0 <= value['realtime_weight_ppm'] <= 1_000_000):
            raise ValueError
        return {**feature, **{k:value[k] for k in _OPTIONS}}
    except (FeatureError, ValueError, TypeError, KeyError):
        raise RealtimeBacktestError('realtime_backtest_invalid_plan') from None


def read_plan(path):
    try:
        body = _read_private_file(path)
        if len(body) > 16_384:
            raise ValueError
        return validate_plan(_json(body))
    except Exception:
        raise RealtimeBacktestError('realtime_backtest_invalid_input') from None


def _context(row, plan):
    """Derived cutoff aggregates retain saved-history provenance and stopped state."""
    at = _time(row['reconstructed_prediction_at'])
    q = row['features']['queue_observations']
    def recent(endpoint):
        available = q[endpoint+'_availability'] == 'recent_responses'
        age = q[endpoint+'_response_age_seconds']
        return _utc(at-timedelta(microseconds=int(Fraction(str(age))*1_000_000))) if available else None
    values = {}
    for name, key in (('ordinary','storeQueue'), ('reservation','reservationQueue')):
        stats = q['display_window_statistics'][key]['whole']
        pairs = stats['comparable_pairs']
        values.update({name+'_removed_labels':stats['removed_labels'] if pairs else None,
            name+'_comparable_pairs':pairs,
            name+'_observed_milliseconds':int(Fraction(str(stats['observed_seconds']))*1000) if pairs else None})
    for name, key in (('groupqueues','queue'), ('storequeuecount','count')):
        counts = q[key+'_response_counts']
        values[name+'_failures'] = sum(counts.get(k,0) for k in
            ('failed_responses','skipped_querys','out_of_order_responses'))
    values['reported_count_raw'] = q['reported_count_raw'] if q['count_availability']=='recent_responses' else None
    queue_at = recent('queue')
    return {'schema_version':1, 'source':SOURCE, 'store_id':plan['store_id'],
        'queue_type':row['features']['queue_type'], 'observation_revision':0,
        'as_of':_utc(at), 'expires_at':_utc(at+timedelta(seconds=60)),
        'window_seconds':plan['window_seconds'], 'max_local_age_seconds':plan['max_gap_seconds'],
        'latest_queue_received_at':queue_at, 'latest_count_received_at':recent('count'),
        'features':[{'feature_id':k,'value':v,'available_at':_utc(at)} for k,v in sorted(values.items())],
        'source_freshness':'unknown', 'count_unit':'unknown', 'store_identity_verified':False,
        'collector_running':False, 'latest_queue_origin':'saved_history' if queue_at else 'unavailable'}


def _evaluation(row, quantiles):
    # This deterministic midpoint is a decision rule for a median envelope,
    # never a midpoint label for training. Infinite bounds remain unscored.
    p10,p50,p90 = (quantiles[k] for k in ('p10','p50','p90'))
    if any(q[k] is None for q in (p10,p50,p90) for k in ('lower_us','upper_us')):
        return None
    at = _time(row['reconstructed_prediction_at'])
    actual = row['target_remaining_call_interval_us']
    def stamp(us):return _utc(at+timedelta(microseconds=us))
    return {'prediction_id':str(uuid4()), 'prediction_made_at':_utc(at),
        'point_call_at':stamp((p50['lower_us']+p50['upper_us'])//2),
        'predicted_call_lower':stamp(p10['lower_us']), 'predicted_call_upper':stamp(p90['upper_us']),
        'observed_call_lower':stamp(actual['lower']), 'observed_call_upper':stamp(actual['upper']),
        'observation_received_at':row['truth_available_at']}


def _paired(records, comparator):
    """Exact bounds for |candidate-T|-|history-T| on the SAME unknown call T."""
    a,b,deltas = [],[],[]
    for case in records:
        base = case['variants']['history_only']['evaluation_record']
        other = case['variants'][comparator]['evaluation_record']
        if base is None or other is None:
            continue
        a.append(base);b.append(other)
        lo,hi = (_time(base[k]) for k in ('observed_call_lower','observed_call_upper'))
        p,q = _time(other['point_call_at']),_time(base['point_call_at'])
        points = {lo,hi,max(lo,min(hi,p)),max(lo,min(hi,q))}
        values = [_us(abs(p-t))-_us(abs(q-t)) for t in points]
        deltas.append((min(values),max(values)))
    return a,b,{'paired_cases':len(deltas),
        'mean_absolute_error_change_seconds':{
            'lower':round(sum(x[0] for x in deltas)/(len(deltas)*1_000_000),6) if deltas else None,
            'upper':round(sum(x[1] for x in deltas)/(len(deltas)*1_000_000),6) if deltas else None},
        'definitely_lower_absolute_error_cases':sum(hi<0 for lo,hi in deltas),
        'definitely_higher_absolute_error_cases':sum(lo>0 for lo,hi in deltas),
        'equal_or_uncertain_absolute_error_cases':sum(lo<=0<=hi for lo,hi in deltas),
        'difference_direction':'comparator_minus_history_negative_is_lower_error',
        'same_unknown_call_time_used_for_both_errors':True,
        'statistical_significance_verified':False}


def build_realtime_backtest(*, source, reviews, remote, plan,
        max_revisions=10_000, max_observations=10_000, now=None):
    clock = _clock() if now is None else now
    plan = validate_plan(plan,now=clock)
    try:
        feature_plan = {k:plan[k] for k in _FEATURE_FIELDS}
        dataset = build_feature_dataset(source=source,reviews=reviews,remote=remote,
            plan=feature_plan,max_revisions=max_revisions,max_observations=max_observations)
        cases = []
        for row in dataset['rows']:
            at = _time(row['reconstructed_prediction_at'])
            elapsed = row['features']['elapsed_seconds']
            target = {'schema_version':1,'as_of':_utc(at),'data_origin':plan['data_origin'],
                'api_profile':plan['api_profile'],'store_id':plan['store_id'],
                **{k:row['features'][k] for k in ('queue_type','party_size','table_type')},
                'mode':'remaining' if elapsed else 'new_join','minimum_samples':plan['minimum_samples'],
                'target_episode_id':row['episode_id']}
            if elapsed:
                target.update(issued_at=_utc(at-timedelta(seconds=elapsed)),call_not_observed=True)
            fitted = build_realtime_fusion(source=source,reviews=reviews,remote=remote,
                plan=target,feature_plan={**feature_plan,'as_of':_utc(at)},context=_context(row,plan),
                **{k:plan[k] for k in _OPTIONS-{'minimum_samples'}},
                max_revisions=max_revisions,max_observations=max_observations,now=at,sealed_replay=True)
            history = fitted['history']['candidates'][0]['fusion_plan']
            combined = fitted['fusion_plan']
            realtime = None
            if fitted['research_realtime_model_fitted']:
                candidate = next((c for c in combined['candidates'] if c['candidate_id']=='realtime'),None)
                # If the full CDF is identical, the fitted model is still a
                # legitimate equal comparator, without an artificial scenario.
                realtime = {**combined,'candidates':[candidate],
                    'prior_weights_ppm':{'realtime':1_000_000}} if candidate else history
            variants = {}
            for name,value in zip(VARIANTS,(history,realtime,combined)):
                quantiles = fuse(value,now=at)['wait_quantile_envelopes_us'] if value else None
                record = _evaluation(row,quantiles) if quantiles else None
                variants[name] = {'research_forecast_available':value is not None,
                    'quantiles_us':quantiles,'evaluation_record':record,
                    'score_unavailable_reason':None if record else
                        'unbounded_quantile_envelope' if quantiles else 'no_eligible_distribution'}
            cases.append({'episode_id':row['episode_id'],'elapsed_seconds':elapsed,
                'reconstructed_prediction_at':_utc(at),'truth_available_at':row['truth_available_at'],
                'truth_source_receipt_sha256':row['truth_source_receipt_sha256'],
                'admitted_queue_inputs_sha256':row['admitted_queue_inputs_sha256'],
                'model_input_sha256':fitted['model_input_sha256'],
                'historical_input_sha256':fitted['history']['historical_input_sha256'],
                'realtime_model_fitted':fitted['research_realtime_model_fitted'],
                'realtime_unavailable_reason':fitted['unavailable_reason'],
                'selected_training_episode_ids':sorted(r['episode_id'] for r in fitted['selected_private_rows']),
                'selected_training_truth_available_at':[r['truth_available_at'] for r in fitted['selected_private_rows']],
                'current_context_basis':fitted['current_context_basis'],
                'context_collector_running':False,'variants':variants})
        by_elapsed = []
        for elapsed in plan['elapsed_seconds']:
            group = [c for c in cases if c['elapsed_seconds']==elapsed]
            variants,paired = {},{}
            for name in VARIANTS:
                items = [c['variants'][name] for c in group]
                records = [v['evaluation_record'] for v in items if v['evaluation_record']]
                variants[name] = {'attempted_cases':len(group),
                    'available_cases':sum(v['research_forecast_available'] for v in items),
                    'score_unavailable_counts':dict(sorted(Counter(v['score_unavailable_reason']
                        for v in items if v['score_unavailable_reason']).items())),
                    'interval_evaluation':evaluate_intervals(records,as_of=plan['as_of'],data_origin=plan['data_origin'])}
            for name in VARIANTS[1:]:
                a,b,comparison = _paired(group,name)
                paired[name] = {**comparison,
                    'history_on_common_cases':evaluate_intervals(a,as_of=plan['as_of'],data_origin=plan['data_origin']),
                    'comparator_on_common_cases':evaluate_intervals(b,as_of=plan['as_of'],data_origin=plan['data_origin'])}
            by_elapsed.append({'elapsed_seconds':elapsed,'independent_target_episodes':len(group),
                'variants':variants,'paired_against_history':paired})
        fingerprint_cases = [{**c,'variants':{k:{**v,'evaluation_record':
            {x:y for x,y in v['evaluation_record'].items() if x!='prediction_id'} if v['evaluation_record'] else None}
            for k,v in c['variants'].items()}} for c in cases]
        return {'realtime_backtest_schema_version':1,'policy':POLICY,'plan':plan,
            'replay_sha256':hashlib.sha256(_canonical({'policy':POLICY,'plan':plan,'cases':fingerprint_cases}).encode()).hexdigest(),
            'reviewed_scope_episodes':dataset['reviewed_scope_episodes'],'attempted_cases':len(cases),
            'excluded_call_not_definitely_future':dataset['excluded_call_not_definitely_future'],
            'evaluations_by_elapsed_seconds':by_elapsed,'cases':cases,
            'fit_policy':'expanding_cutoff_known_reviewed_history_excluding_whole_target_episode',
            'variant_policy':'same_case_fixed_parameters_history_realtime_prior_no_provider',
            'current_context_basis':'cutoff_known_sealed_replay',
            'prediction_time_basis':'issued_interval_upper_plus_elapsed_reconstruction',
            'target_context_basis':'reviewed_outcome_reconstruction_not_historical_request',
            'parameters_tuned_on_test_outcomes':False,'best_variant_selected':False,
            'elapsed_groups_are_independent_samples':False,'population_representativeness_verified':False,
            'right_censoring_modelled':False,'historical_target_context_verified':False,
            'historical_availability_verified':False,'durable_availability_verified':False,
            'forecast_log_verified':False,'model_performance_verified':False,'coverage_calibrated':False,
            'ideal_time_planning_evaluated':False,'ai_variant_evaluated':False,
            'verified_training_labels':0,'eta_available':False,'provider_called':False,
            'network_performed':False,'business_writes':0,'output_requires_private_handling':True}
    except RealtimeBacktestError:raise
    except (FeatureError,RealtimeFusionError):
        raise RealtimeBacktestError('realtime_backtest_source_invalid') from None
    except Exception:
        raise RealtimeBacktestError() from None


def write_realtime_backtest(*, destination, **options):
    result = build_realtime_backtest(**options)
    body = _canonical(result).encode()
    if len(body)>2_097_152:
        raise RealtimeBacktestError()
    try:publication = _write_packet(body,destination)
    except PacketError as error:
        raise RealtimeBacktestError('realtime_backtest_output_exists' if error.error_code=='packet_output_exists'
            else 'realtime_backtest_failed',committed=error.committed) from None
    return {'artifact_written':True,'committed':True,'durability_confirmed':publication['durability_confirmed'],
        'attempted_cases':result['attempted_cases'],'reviewed_scope_episodes':result['reviewed_scope_episodes'],
        'paired_comparison_available':any(x['paired_cases'] for e in result['evaluations_by_elapsed_seconds']
            for x in e['paired_against_history'].values()),'forecast_log_verified':False,
        'model_performance_verified':False,'verified_training_labels':0,'eta_available':False,
        'provider_called':False,'network_performed':False,'output_requires_private_handling':True}
