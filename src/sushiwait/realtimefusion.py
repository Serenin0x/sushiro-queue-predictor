"""Episode-balanced landmark neighbors, using only cutoff-known reviewed claims.

This fits an interpretable research distribution, not authenticated training,
censoring correction, a reliable position model or a calibrated production ETA.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from fractions import Fraction
import hashlib

from .baseline import _features, _GROUPS, _us, validate_plan
from .features import build_feature_dataset, FeatureError, validate_feature_plan
from .fusion import _cdf_bounds, validate_plan as validate_fusion, fuse, FusionError
from .historyfusion import build_history_fusion, HistoryFusionError
from .intake import _canonical
from .outcomes import _time
from .packets import _write_packet, PacketError
from .queuebinding import field_for_queue, require_bound_context

POLICY = 'cutoff_episode_balanced_landmark_neighbors_v2'
MODEL = 'reviewed-landmark-neighbors-v2'


class RealtimeFusionError(ValueError):
    def __init__(self, code='realtime_model_failed', *, committed=False):
        self.error_code = code if code in {'realtime_model_invalid_input',
            'realtime_model_invalid_options', 'realtime_model_scope_mismatch',
            'realtime_model_source_invalid', 'realtime_model_output_exists',
            'realtime_model_failed'} else 'realtime_model_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def _median(values):
    values = sorted(values);n = len(values)
    return (values[(n-1)//2]+values[n//2])/2


def _scales(vectors, elapsed_fallback):
    # Median absolute deviation, then observed range, then a fixed unit.
    # All scales come from one representative per episode, never its label.
    out = []
    for index,column in enumerate(zip(*vectors)):
        middle = _median(column)
        mad = _median([abs(v-middle) for v in column])
        out.append(mad or max(column)-min(column) or (elapsed_fallback if index==0 else Fraction(1)))
    return out


def _historical_vector(row, queue, window):
    f = row['features'];q = f['queue_observations']
    if q['queue_availability'] != 'recent_responses':
        return None
    stats = q['display_window_statistics'][queue]['whole']
    pairs = stats['comparable_pairs']
    covered = Fraction(str(stats['observed_seconds']))*1_000_000
    if pairs < 2 or covered < window*500_000 or covered > window*1_000_000:
        return None
    count = q['reported_count_raw'] if q['count_availability'] == 'recent_responses' else None
    vector = [Fraction(f['elapsed_seconds']*1_000_000),
              Fraction(stats['removed_labels']*60_000_000, covered)]
    if count is not None:
        vector.append(Fraction(count))
    return vector, covered/pairs, count is not None


def _current_vector(plan, context, *, now, sealed_replay=False):
    at = _time(context['as_of']);received = context['latest_queue_received_at']
    provenance_ok = (not context['collector_running'] and context['latest_queue_origin'] == 'saved_history'
        and now == at) if sealed_replay else (
        context['collector_running'] and context['latest_queue_origin'] == 'worker_commit')
    if (not provenance_ok
            or received is None or now >= _time(context['expires_at'])
            or (now-_time(received)).total_seconds() > context['max_local_age_seconds']):
        return None
    values = {f['feature_id']:f['value'] for f in context['features']}
    queue = plan['queue_type'];pairs = values.get(queue+'_comparable_pairs')
    milliseconds = values.get(queue+'_observed_milliseconds')
    removed = values.get(queue+'_removed_labels')
    if (pairs is None or pairs < 2 or milliseconds is None
            or milliseconds < context['window_seconds']*500 or removed is None
            or values.get('groupqueues_failures') != 0):
        return None
    elapsed = _us(at-_time(plan['issued_at'])) if plan['mode'] == 'remaining' else 0
    count = values.get('reported_count_raw')
    count_time = context['latest_count_received_at']
    if count_time is None or (now-_time(count_time)).total_seconds() > context['max_local_age_seconds']:
        count = None
    vector = [Fraction(elapsed),Fraction(removed*60_000, milliseconds)]
    if count is not None:
        vector.append(Fraction(count))
    return vector, Fraction(milliseconds*1000,pairs), count is not None, elapsed


def _same_distribution(one, two):
    points = {0}
    for c in (one,two):
        direct = 'conditional_interval_sample' in c
        sample = c['conditional_interval_sample' if direct else 'interval_sample']
        elapsed = 0 if direct else sample['elapsed_us'] or 0
        for lo,hi,_ in sample['intervals']:
            points.add(max(0,lo-elapsed))
            if hi is not None:points.add(max(0,hi-elapsed))
    return all(_cdf_bounds(one,t)==_cdf_bounds(two,t) for t in points)


def build_realtime_fusion(*, source, reviews, remote, plan, feature_plan, context,
        neighbors=20, minimum_episodes=8, maximum_elapsed_difference_seconds=300,
        maximum_standardized_distance=3, realtime_weight_ppm=500_000,
        model_version=MODEL, ai_blend_ppm=0, max_revisions=10_000,
        max_observations=10_000, now=None, sealed_replay=False):
    """Fit/query independent-episode empirical neighbors plus static history.

    Data preparation audits complete sealed sources. Only a completed episode
    whose result AND review were received by this forecast cutoff is usable.
    Repeated landmarks never become independent probability mass.
    """
    from .features import _clock
    clock = _clock() if now is None else now
    try:
        plan = validate_plan(plan, now=clock)
        feature_plan = validate_feature_plan(feature_plan, now=clock)
        require_bound_context(context)
        if (type(sealed_replay) is not bool or sealed_replay and ai_blend_ppm != 0
                or plan['mode'] not in ('new_join','remaining')
                or any(type(v) is not int for v in (neighbors, minimum_episodes,
                    maximum_elapsed_difference_seconds, maximum_standardized_distance, realtime_weight_ppm))
                or not 1 <= minimum_episodes <= neighbors <= 100
                or not 0 <= maximum_elapsed_difference_seconds <= 3600
                or not 1 <= maximum_standardized_distance <= 20
                or not 0 <= realtime_weight_ppm <= 1_000_000):
            raise RealtimeFusionError('realtime_model_invalid_options')
        if (any(feature_plan[k]!=plan[k] for k in ('as_of','data_origin','api_profile','store_id'))
                or feature_plan['window_seconds'] != context['window_seconds']):
            raise RealtimeFusionError('realtime_model_scope_mismatch')
        history = build_history_fusion(source=source, reviews=reviews, plan=plan, context=context,
            model_version=model_version, ai_blend_ppm=ai_blend_ppm, max_revisions=max_revisions)
        # A history miss still validates and canonicalizes the public context.
        context = history['public_context']
        if sealed_replay and (context['collector_running']
                or context['latest_queue_origin'] not in ('saved_history','unavailable')
                or _time(context['as_of']) != clock):
            raise RealtimeFusionError('realtime_model_scope_mismatch')
        dataset = build_feature_dataset(source=source, reviews=reviews, remote=remote,
            plan=feature_plan, max_revisions=max_revisions, max_observations=max_observations)
        current = _current_vector(plan,context,now=clock,sealed_replay=sealed_replay)
        queue = field_for_queue(plan['queue_type'])
        eligible = []
        if current is not None:
            vector,cadence,count_known,elapsed = current
            for row in dataset['rows']:
                f = row['features'];target = row['target_remaining_call_interval_us']
                if (row['episode_id']==plan['target_episode_id']
                        or any(f[k]!=plan[k] for k in ('store_id','queue_type','party_size','table_type'))
                        or _time(row['truth_available_at'])>_time(plan['as_of'])
                        or abs(f['elapsed_seconds']*1_000_000-elapsed) >
                            (maximum_elapsed_difference_seconds*1_000_000 if elapsed else 0)
                        or not 0 < target['lower'] <= target['upper'] <= 172_800_000_000
                        or target['lower']+f['elapsed_seconds']*1_000_000-elapsed <= 0
                        or target['upper']+f['elapsed_seconds']*1_000_000-elapsed > 172_800_000_000):
                    continue
                historic = _historical_vector(row,queue,context['window_seconds'])
                if (historic is None or historic[2]!=count_known
                        or not Fraction(4,5)*cadence <= historic[1] <= Fraction(5,4)*cadence):
                    continue
                eligible.append((row,historic[0],_features(row['reconstructed_prediction_at'],as_of=row['reconstructed_prediction_at'])))
        wanted = _features(plan['as_of'],as_of=plan['as_of'])
        matched_group,selected,attempts,scales = None,[],[],[]
        if current is not None:
            for group,keys in _GROUPS:
                subset = [r for r in eligible if not keys or wanted['calendar_status']=='available'
                    and r[2]['calendar_status']=='available' and all(r[2][k]==wanted[k] for k in keys)]
                episodes = {}
                for row,v,date in subset:
                    key = (abs(v[0]-vector[0]),row['reconstructed_prediction_at'],row['features_sha256'])
                    previous = episodes.get(row['episode_id'])
                    if previous is None or key < previous[0]:episodes[row['episode_id']] = (key,row,v)
                representatives = list(episodes.values())
                if representatives:
                    scales = _scales([r[2] for r in representatives],
                        Fraction(max(1,maximum_elapsed_difference_seconds)*1_000_000))
                    ranked = []
                    for _,row,v in representatives:
                        distances = [abs(a-b)/s for a,b,s in zip(v,vector,scales)]
                        if max(distances) <= maximum_standardized_distance:
                            ranked.append((sum(distances),row['features_sha256'],row))
                    # Include all ties at the kth distance; do not select on a
                    # receipt/hash-dependent endpoint when distances are equal.
                    ranked.sort(key=lambda r:(r[0],r[1]))
                    bound = ranked[min(neighbors,len(ranked))-1][0] if ranked else None
                    picked = [r[2] for r in ranked if r[0] <= bound] if ranked else []
                else:picked = []
                attempts.append({'group':group,'independent_episodes':len(representatives),
                    'within_support_episodes':len(picked)})
                if len(picked) >= minimum_episodes:
                    matched_group,selected = group,picked;break
        historical = history['candidates'][0]['fusion_plan']
        fusion_plan = historical
        candidate = None
        redundant = False
        if selected:
            counts = Counter((r['target_remaining_call_interval_us']['lower']+r['features']['elapsed_seconds']*1_000_000-elapsed,
                              r['target_remaining_call_interval_us']['upper']+r['features']['elapsed_seconds']*1_000_000-elapsed)
                              for r in selected)
            sample = {'intervals':[[a,b,n] for (a,b),n in sorted(counts.items())]}
            sample['conditioned_elapsed_us' if plan['mode']=='remaining' else 'elapsed_us'] = elapsed if plan['mode']=='remaining' else None
            candidate = {'candidate_id':'realtime',
                'conditional_interval_sample' if plan['mode']=='remaining' else 'interval_sample':sample}
            redundant = historical is not None and _same_distribution(historical['candidates'][0],candidate)
            if not redundant:
                fusion_plan = {**(historical or {'schema_version':2,
                    'data_origin':'synthetic' if plan['data_origin']=='synthetic' else 'research',
                    'prediction_target':'remaining' if plan['mode']=='remaining' else 'new_join_total',
                    'conditioning':'call_not_observed_after_elapsed' if plan['mode']=='remaining' else 'new_join',
                    'model_version':model_version,'public_context':context,'ai_blend_ppm':ai_blend_ppm}),
                    'candidates':(historical['candidates'] if historical else [])+[candidate],
                    'prior_weights_ppm':{'history':1_000_000-realtime_weight_ppm,'realtime':realtime_weight_ppm}
                        if historical else {'realtime':1_000_000}}
                fusion_plan = validate_fusion(fusion_plan,now=clock)
        prediction = fuse(fusion_plan,now=clock) if fusion_plan is not None else None
        return {'realtime_model_schema_version':1,'policy':POLICY,'model_version':model_version,
            'plan':plan,'feature_plan':feature_plan,'history':history,'fusion_plan':fusion_plan,
            'research_quantiles_us':prediction['wait_quantile_envelopes_us'] if prediction else None,
            'research_prediction_available':prediction is not None,
            'research_realtime_model_fitted':bool(selected),'realtime_candidate_added':candidate is not None and not redundant,
            'unavailable_reason':'current_trend_unavailable' if current is None else
                'insufficient_independent_neighbor_episodes' if not selected else
                'same_distribution_as_history' if redundant else None,
            'matched_group':matched_group,'matching_attempts':attempts,'selected_independent_episodes':len(selected),
            'selected_private_rows':selected,'feature_dataset_sha256':dataset['feature_dataset_sha256'],
            'scale_method':'episode_mad_then_range_then_elapsed_band_or_unit',
            'survival_basis':'definite_historical_survival_after_age_transport',
            'nearby_landmark_age_transport_applied':plan['mode']=='remaining' and bool(selected),
            'current_context_basis':'cutoff_known_sealed_replay' if sealed_replay else 'live_writer_projection',
            'landmark_covariates_at_exact_target_elapsed_verified':False,
            'neighbor_ranking_uses_outcome_values':False,'ties_included':True,
            'options':{'neighbors':neighbors,'minimum_episodes':minimum_episodes,
                'maximum_elapsed_difference_seconds':maximum_elapsed_difference_seconds,
                'maximum_standardized_distance':maximum_standardized_distance,'realtime_weight_ppm':realtime_weight_ppm},
            'model_input_sha256':hashlib.sha256(_canonical({'policy':POLICY,'plan':plan,
                'context':context,'model_version':model_version,'ai_blend_ppm':ai_blend_ppm,
                'sealed_replay':sealed_replay,
                'options':[neighbors,minimum_episodes,maximum_elapsed_difference_seconds,
                    maximum_standardized_distance,realtime_weight_ppm],
                'selected':selected,'scales':[[s.numerator,s.denominator] for s in scales]}).encode()).hexdigest(),
            'right_censoring_modelled':False,'model_performance_verified':False,'coverage_calibrated':False,
            'verified_training_labels':0,'training_eligible':False,'eta_available':False,
            'provider_called':False,'network_performed':False,'business_writes':0,
            'output_requires_private_handling':True}
    except RealtimeFusionError:raise
    except (FeatureError,HistoryFusionError):
        raise RealtimeFusionError('realtime_model_source_invalid') from None
    except (FusionError,ValueError,TypeError,KeyError,OverflowError):
        raise RealtimeFusionError('realtime_model_invalid_input') from None


def write_realtime_fusion(*, destination, **options):
    result = build_realtime_fusion(**options)
    body = _canonical(result).encode()
    if len(body)>2_097_152:raise RealtimeFusionError()
    try:publication = _write_packet(body,destination)
    except PacketError as e:
        raise RealtimeFusionError('realtime_model_output_exists' if e.error_code=='packet_output_exists'
            else 'realtime_model_failed',committed=e.committed) from None
    return {'artifact_written':True,'committed':True,'durability_confirmed':publication['durability_confirmed'],
        'research_prediction_available':result['research_prediction_available'],
        'research_realtime_model_fitted':result['research_realtime_model_fitted'],
        'selected_independent_episodes':result['selected_independent_episodes'],
        'realtime_candidate_added':result['realtime_candidate_added'],
        'verified_training_labels':0,'eta_available':False,'provider_called':False,
        'network_performed':False,'business_writes':0,'output_requires_private_handling':True}
