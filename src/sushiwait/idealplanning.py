"""Complete issue-time grids scored by interval mass, never median subtraction.

Bounds describe caller-supplied empirical distributions, not population
coverage, authenticated labels or permission to book. All private calculations
retain the common observation cutoff and the missing candidates.
"""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from hashlib import sha256

from .baseline import validate_plan, _us
from .fusion import fuse, validate_plan as validate_fusion, PPM
from .historyfusion import build_history_fusion
from .intake import _canonical
from .outcomes import _time, _utc
from .packets import _write_packet, PacketError
from .queuebinding import require_bound_context

POLICY='complete_issue_grid_interval_window_mass_v1'
MAX_BYTES=2_097_152


class IdealPlanError(ValueError):
    def __init__(self, code='ideal_plan_invalid', *, committed=False):
        self.error_code=code if code in {'ideal_plan_invalid','ideal_plan_support_limit',
            'ideal_plan_output_exists','ideal_plan_failed'} else 'ideal_plan_failed'
        self.committed=committed is True
        super().__init__(self.error_code)


def _bounds(candidate, lower, upper):
    if 'atoms' in candidate:
        rows=[(r['lower_us'],r['upper_us'],r['mass_ppm']) for r in candidate['atoms']]
        total=PPM
    else:
        sample=candidate['interval_sample']
        if sample['elapsed_us'] is not None:raise IdealPlanError()
        rows=sample['intervals'];total=sum(n for _,_,n in rows)
    tests={
        'early':(lambda l,u:u is not None and u<lower,lambda l,u:l<lower),
        'within_window':(lambda l,u:l>=lower and u is not None and u<=upper,
                         lambda l,u:l<=upper and (u is None or u>=lower)),
        'late':(lambda l,u:l>upper,lambda l,u:u is None or u>upper)}
    return {key:tuple(Fraction(sum(n for l,u,n in rows if test(l,u)),total)
        for test in pair) for key,pair in tests.items()}


def _fraction(value):return {'numerator':value.numerator,'denominator':value.denominator}


def score_issue_grid(plan, entries, *, early_minutes=0, late_minutes=10,
        minimum_window_mass_ppm=600_000, maximum_early_mass_ppm=200_000,
        maximum_late_mass_ppm=200_000, advice=None, now=None):
    """Evaluate every explicit candidate, including misses and nonmonotone results.

    Only new-join total-wait distributions belong to this inverse calculation.
    The same optional public advice is revalidated for every scenario set.
    Missing/invalid candidates are never silently removed from the search.
    """
    clock=datetime.now(timezone.utc) if now is None else now
    try:
        plan=validate_plan(plan,now=clock)
        if (plan['mode']!='ideal_time' or type(entries) is not list
                or len(entries)!=len(plan['candidate_issue_times'])
                or any(type(v) is not int or not 0<=v<=120 for v in (early_minutes,late_minutes))
                or any(type(v) is not int or not 0<=v<=PPM for v in
                    (minimum_window_mass_ppm,maximum_early_mass_ppm,maximum_late_mass_ppm))
                or len(_canonical(entries).encode())>MAX_BYTES):raise IdealPlanError()
        if advice is not None and len(_canonical(advice).encode())>16_384:raise IdealPlanError()
        preferences={'early_minutes':early_minutes,'late_minutes':late_minutes,
            'minimum_window_mass_ppm':minimum_window_mass_ppm,'maximum_early_mass_ppm':maximum_early_mass_ppm,
            'maximum_late_mass_ppm':maximum_late_mass_ppm}
        normalized=[];common=None
        for i,entry in enumerate(entries):
            if (type(entry) is not dict or set(entry)!={'candidate_index','candidate_issue_at','fusion_plan'}
                    or type(entry['candidate_index']) is not int or entry['candidate_index']!=i
                    or _time(entry['candidate_issue_at'])!=_time(plan['candidate_issue_times'][i])):
                raise IdealPlanError()
            value=entry['fusion_plan']
            if value is not None:
                value=validate_fusion(value,now=clock);context=value['public_context'];require_bound_context(context)
                if (value['prediction_target']!='new_join_total' or value['conditioning']!='new_join'
                        or value['data_origin']!=('synthetic' if plan['data_origin']=='synthetic' else 'research')
                        or context['store_id']!=plan['store_id'] or context['queue_type']!=plan['queue_type']
                        or _time(context['as_of'])!=_time(plan['as_of'])
                        or any('conditional_interval_sample' in c or
                            'interval_sample' in c and c['interval_sample']['elapsed_us'] is not None
                            for c in value['candidates'])):raise IdealPlanError()
                if common is not None and context!=common:raise IdealPlanError()
                common=context
            normalized.append(value)
        target=_time(plan['desired_arrival_at'])+timedelta(minutes=plan['call_offset_minutes'])
        lo,hi=target-timedelta(minutes=early_minutes),target+timedelta(minutes=late_minutes)
        context_current=bool(common and target>=clock and common['collector_running']
            and common['latest_queue_origin']=='worker_commit' and clock<_time(common['expires_at'])
            and common['latest_queue_received_at'] is not None
            and (clock-_time(common['latest_queue_received_at'])).total_seconds()<=common['max_local_age_seconds'])
        rows=[];eligible=[];influence=False
        for i,(issued,value) in enumerate(zip(plan['candidate_issue_times'],normalized)):
            row={'candidate_index':i,'candidate_issue_at':issued,'state':'insufficient_model',
                'empirical_mass_bounds':None,'meets_preferences':False,'call_quantile_envelopes':None,
                'issue_time_not_before_compute':_time(issued)>=clock,
                'advice_accepted':False,'ai_numerical_influence_applied':False}
            if value is not None:
                result=fuse(value,advice=advice,now=clock);lower=_us(lo-_time(issued));upper=_us(hi-_time(issued))
                combined={k:[Fraction(0),Fraction(0)] for k in ('early','within_window','late')}
                for candidate in value['candidates']:
                    weight=Fraction(result['effective_weight_numerators'][candidate['candidate_id']],
                                    result['effective_weight_denominator'])
                    if not weight:continue
                    for key,pair in _bounds(candidate,lower,upper).items():
                        for side in (0,1):combined[key][side]+=weight*pair[side]
                meets=(combined['within_window'][0]>=Fraction(minimum_window_mass_ppm,PPM)
                    and combined['early'][1]<=Fraction(maximum_early_mass_ppm,PPM)
                    and combined['late'][1]<=Fraction(maximum_late_mass_ppm,PPM))
                quantiles={key:{side.removesuffix('_us'):_utc(_time(issued)+timedelta(microseconds=number))
                    if number is not None else None for side,number in bounds.items()}
                    for key,bounds in result['wait_quantile_envelopes_us'].items()}
                row.update(state='scored',empirical_mass_bounds={k:{'lower':_fraction(v[0]),
                    'upper':_fraction(v[1])} for k,v in combined.items()},meets_preferences=meets,
                    call_quantile_envelopes=quantiles,advice_accepted=result['advice_accepted'],
                    ai_numerical_influence_applied=result['ai_numerical_influence_applied'])
                influence|=result['ai_numerical_influence_applied']
                if meets:eligible.append((-combined['within_window'][0],combined['early'][1],combined['late'][1],i))
            rows.append(row)
        ranked=[r[-1] for r in sorted(eligible)]
        future=[i for i in ranked if rows[i]['issue_time_not_before_compute']]
        selected=future[0] if future and context_current else None
        return {'ideal_plan_schema_version':1,'policy':POLICY,'plan':plan,'computed_at':_utc(clock),
            'input_sha256':sha256(_canonical({'plan':plan,'entries':entries,'advice':advice,
                'preferences':preferences}).encode()).hexdigest(),
            'target_call_at':_utc(target),'acceptable_call_window':{'lower':_utc(lo),'upper':_utc(hi)},
            'preferences':preferences,'candidates':rows,'feasible_candidate_indices':ranked,
            'future_feasible_candidate_indices':future,'selected_candidate_index':selected,
            'research_suggestion_available':selected is not None,'public_context_current':context_current,
            'unavailable_reason':None if selected is not None else
                'issue_times_passed' if ranked and not future else 'current_context_unavailable' if ranked
                else 'preferences_not_met' if any(normalized) else 'insufficient_models',
            'selection_method':'maximum_window_lower_mass_then_minimum_early_upper_then_late_upper_then_earliest',
            'search_scope':'complete_caller_grid_only','continuous_issue_window_verified':False,
            'global_optimality_verified':False,'candidate_bookability_verified':False,'monotonicity_assumed':False,
            'future_queue_observations_used':False,'ai_numerical_influence_applied':influence,
            'population_probabilities_calibrated':False,'coverage_calibrated':False,'actual_call_verified':False,
            'verified_training_labels':0,'eta_available':False,'provider_called':False,'network_performed':False,
            'booking_or_cancellation_performed':False,'notification_sent':False,'output_requires_private_handling':True}
    except IdealPlanError:raise
    except Exception:raise IdealPlanError() from None


def build_ideal_plan(*, source, reviews, plan, context, model_version='reviewed-ideal-window-v1',
        ai_blend_ppm=0, max_revisions=10_000, **options):
    history=build_history_fusion(source=source,reviews=reviews,plan=plan,context=context,
        model_version=model_version,ai_blend_ppm=ai_blend_ppm,max_revisions=max_revisions)
    entries=[{k:r[k] for k in ('candidate_index','candidate_issue_at','fusion_plan')} for r in history['candidates']]
    result=score_issue_grid(history['plan'],entries,**options)
    result.update(historical_input_sha256=history['historical_input_sha256'],
        eligible_static_cohort_samples=history['eligible_static_cohort_samples'],
        excluded_samples=history['excluded_samples'],models_generated=['history'],realtime_regime_models_fitted=False)
    return result


def write_ideal_plan(*, destination, **options):
    try:
        result=build_ideal_plan(**options);body=_canonical(result).encode()
        if len(body)>MAX_BYTES:raise IdealPlanError('ideal_plan_support_limit')
        publication=_write_packet(body,destination)
        return {'artifact_written':True,'committed':True,'durability_confirmed':publication['durability_confirmed'],
            'candidate_count':len(result['candidates']),'feasible_candidate_count':len(result['feasible_candidate_indices']),
            'research_suggestion_available':result['research_suggestion_available'],
            'selected_candidate_index':result['selected_candidate_index'],'verified_training_labels':0,
            'eta_available':False,'network_performed':False,'provider_called':False,
            'booking_or_cancellation_performed':False,'notification_sent':False,
            'output_requires_private_handling':True}
    except PacketError as e:
        raise IdealPlanError('ideal_plan_output_exists' if e.error_code=='packet_output_exists'
                            else 'ideal_plan_failed',committed=e.committed) from None
