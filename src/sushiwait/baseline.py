"""Private interval-valued historical research baseline, never calibrated ETA.

Only review declarations received by the requested cutoff are considered.
Finite display numbers and raw queue counts never become event labels here.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import hashlib
import re

from .calendar import date_features
from .credentials import _read_private_file
from .intake import _canonical
from .outcomes import _json, _time, _utc, _uuid
from .packets import PacketError, _write_packet
from .reviews import ReviewError

POLICY = 'reviewed_interval_history_v1'
MAX_WAIT_US = 172_800_000_000
_COMMON = {'schema_version', 'as_of', 'data_origin', 'api_profile', 'store_id', 'queue_type',
    'party_size', 'table_type', 'mode', 'minimum_samples', 'target_episode_id'}
_EXTRA = {'new_join': set(), 'remaining': {'issued_at', 'call_not_observed'},
    'ideal_time': {'desired_arrival_at', 'call_offset_minutes', 'candidate_issue_times'}}
_GROUPS = (
    ('exact_date_hour', ('date_type','holiday_name','holiday_day_index','makeup_for','iso_weekday','month','calendar_season','hour')),
    ('date_type_hour', ('date_type','holiday_name','makeup_for','hour')),
    ('date_type', ('date_type','holiday_name','makeup_for')),
    ('store_queue_party_table', ()),
)
_CODES = frozenset({'baseline_invalid_plan', 'baseline_invalid_input', 'baseline_invalid_candidates',
    'baseline_invalid_intervals', 'baseline_source_invalid', 'baseline_output_exists',
    'baseline_operation_failed'})


class BaselineError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if type(code) is str and code in _CODES else 'baseline_operation_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def _us(delta):
    return (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds


def validate_plan(value, *, now=None):
    try:
        clock = _clock() if now is None else now
        if (type(value) is not dict or type(value.get('mode')) is not str or value['mode'] not in _EXTRA
                or set(value) != _COMMON | _EXTRA[value['mode']]
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['data_origin'] not in ('self_reported','synthetic')
                or value['api_profile'] not in ('legacy','miniapp_gateway')
                or not isinstance(value['store_id'],str) or not re.fullmatch('[1-9][0-9]{0,9}',value['store_id'])
                or int(value['store_id']) > 2**31-1 or value['queue_type'] not in ('ordinary','reservation')
                or value['table_type'] not in ('booth','counter','either','unknown')
                or value['party_size'] is not None and
                    (type(value['party_size']) is not int or not 1 <= value['party_size'] <= 2**31-1)
                or type(value['minimum_samples']) is not int or not 1 <= value['minimum_samples'] <= 1000):
            raise ValueError
        cutoff = _time(value['as_of'])
        if cutoff > clock:
            raise ValueError
        if value['target_episode_id'] is not None:
            _uuid(value['target_episode_id'])
        safe = {**value, 'as_of': _utc(cutoff)}
        if value['mode'] == 'remaining':
            issued = _time(value['issued_at'])
            if (value['call_not_observed'] is not True or value['target_episode_id'] is None
                    or not 0 <= _us(cutoff-issued) <= MAX_WAIT_US):
                raise ValueError
            safe['issued_at'] = _utc(issued)
        if value['mode'] == 'ideal_time':
            offset = value['call_offset_minutes']
            if type(offset) is not int or not -120 <= offset <= 120:
                raise ValueError
            arrival = _time(value['desired_arrival_at'])
            target = arrival+timedelta(minutes=offset)
            if not cutoff <= arrival <= cutoff+timedelta(days=1) or target < cutoff:
                raise ValueError
            times = value['candidate_issue_times']
            if type(times) is not list or not 1 <= len(times) <= 288:
                raise ValueError
            normalized = [_time(t) for t in times]
            if (any(not cutoff <= t <= cutoff+timedelta(days=1) for t in normalized)
                    or any(b <= a for a,b in zip(normalized,normalized[1:]))):
                raise ValueError
            safe.update(desired_arrival_at=_utc(arrival), candidate_issue_times=[_utc(t) for t in normalized])
        if len(_canonical(safe).encode()) > 16_384:
            raise ValueError
        return safe
    except (ValueError, TypeError, KeyError, OverflowError):
        raise BaselineError('baseline_invalid_plan') from None


def read_plan(path):
    try:
        body = _read_private_file(path)
        if len(body) > 16_384:
            raise ValueError
        return validate_plan(_json(body))
    except Exception:
        raise BaselineError('baseline_invalid_input') from None


def interval_quantiles(intervals, *, elapsed_us=None):
    """Empirical quantile envelopes, conditional on survival when elapsed is set.

    This is interval arithmetic on a finite reviewed-claim sample. It neither
    estimates right-censoring nor supplies a population coverage guarantee.
    """
    if (type(intervals) is not list or not 1 <= len(intervals) <= 10_000
            or any(type(pair) not in (tuple,list) or len(pair) != 2
                or any(type(t) is not int for t in pair) or not 0 <= pair[0] <= pair[1] <= MAX_WAIT_US
                for pair in intervals)
            or elapsed_us is not None and (type(elapsed_us) is not int or not 0 <= elapsed_us <= MAX_WAIT_US)):
        raise BaselineError('baseline_invalid_intervals')
    lower, upper = sorted(p[0] for p in intervals), sorted(p[1] for p in intervals)
    result = {}
    if elapsed_us is None:
        for name, num, den in (('p10',1,10),('p50',1,2),('p90',9,10)):
            index = (num*len(intervals)+den-1)//den-1
            result[name] = {'lower_us':lower[index],'upper_us':upper[index]}
        return {'quantiles':result,'definite_survivors':None,'possible_survivors':None,
                'conditioning':'new_join','quantile_method':'empirical_inverted_cdf_interval_envelopes'}
    e = elapsed_us
    definite = sorted(u for l,u in intervals if l > e)
    possible_lower = sorted(l for l,u in intervals if u > e)
    if not definite:
        return {'quantiles':None,'definite_survivors':0,'possible_survivors':len(possible_lower),
                'conditioning':'call_not_observed_after_elapsed','reason_code':'no_definite_survivors'}
    points = sorted({e, *(t for pair in intervals for t in pair if t > e)})

    def bounds(t):
        must_finished = bisect_right(definite,t)
        could_later = len(upper)-bisect_right(upper,t)
        could_finished = bisect_right(possible_lower,t)
        must_later = len(lower)-bisect_right(lower,t)
        return (must_finished,must_finished+could_later),(could_finished,could_finished+must_later)

    def inverse(num,den,side):
        # Upper CDF at e is its possible right limit: infimum bound, not an exact zero wait.
        lo,hi = 0,len(points)-1
        while lo < hi:
            mid=(lo+hi)//2
            n,d=bounds(points[mid])[side]
            if n*den >= num*d:
                hi=mid
            else:
                lo=mid+1
        return points[lo]-e

    for name,num,den in (('p10',1,10),('p50',1,2),('p90',9,10)):
        result[name]={'lower_us':inverse(num,den,1),'upper_us':inverse(num,den,0)}
    return {'quantiles':result,'definite_survivors':len(definite),'possible_survivors':len(possible_lower),
            'conditioning':'call_not_observed_after_elapsed',
            'quantile_method':'empirical_conditional_interval_cdf_bounds'}


def _features(at, *, as_of):
    value = date_features(at, as_of=as_of)
    holiday = value['holiday'] or {}
    return {k:value[k] for k in ('date_type','makeup_for','iso_weekday','month','calendar_season','calendar_status','local_date')} | {
        'holiday_name':holiday.get('name'),'holiday_day_index':holiday.get('day_index'),
        'hour':value['minute_of_day']//60,'calendar_provenance':value['calendar_provenance']}


def _prepare(candidates, plan):
    pool, excluded = [], {'target_episode':0,'static_scope':0,'unsupported_wait':0}
    for candidate in candidates:
        ep=candidate['episode']
        if ep['episode_id'] == plan['target_episode_id']:
            excluded['target_episode']+=1
            continue
        if any(ep[k] != plan[k] for k in ('store_id','queue_type','party_size','table_type')):
            excluded['static_scope']+=1
            continue
        events={v['event_type']:v for v in ep['events']}
        a,b=events['issued'],events['called']
        l=max(0,_us(_time(b['event_time_lower'])-_time(a['event_time_upper'])))
        u=_us(_time(b['event_time_upper'])-_time(a['event_time_lower']))
        if not 0 <= l <= u <= MAX_WAIT_US:
            excluded['unsupported_wait']+=1
            continue
        pool.append({'interval':(l,u),'lower_features':_features(a['event_time_lower'],as_of=plan['as_of']),
            'upper_features':_features(a['event_time_upper'],as_of=plan['as_of']),
            'source_receipt_sha256':candidate['source_receipt_sha256'],'available_at':candidate['available_at']})
    return pool,excluded


def _forecast(pool, plan, issued_at, *, elapsed_us=None):
    wanted = _features(issued_at, as_of=plan['as_of'])
    attempts = []
    for level, keys in _GROUPS:
        matched = [item for item in pool if not keys or (
            wanted['calendar_status'] == 'available' and
            all(item[endpoint]['calendar_status'] == 'available' and
                all(item[endpoint][key] == wanted[key] for key in keys)
                for endpoint in ('lower_features', 'upper_features')))]
        arithmetic = interval_quantiles([item['interval'] for item in matched], elapsed_us=elapsed_us) if matched else None
        survivors = arithmetic['definite_survivors'] if arithmetic else 0
        attempts.append({'group':level, 'matching_samples':len(matched),
            'definite_survivors':survivors if elapsed_us is not None else None})
        if len(matched) < plan['minimum_samples'] or elapsed_us is not None and survivors < plan['minimum_samples']:
            continue
        quantiles = arithmetic['quantiles']
        median = quantiles['p50']
        point = (median['lower_us']+median['upper_us'])//2
        base = _time(plan['as_of']) if elapsed_us is not None else _time(issued_at)
        stamps = lambda lo,hi: {'lower':_utc(base+timedelta(microseconds=lo)),
                               'upper':_utc(base+timedelta(microseconds=hi))}
        return {'research_forecast_available':True, 'reason_code':None, 'matched_group':level,
            'coarse_fallback':level != _GROUPS[0][0], 'matching_samples':len(matched),
            'minimum_samples':plan['minimum_samples'], 'minimum_is_quality_guarantee':False,
            'group_attempts':attempts, 'target_date_features':wanted,
            'sample_issue_endpoint_dates':sorted({item[endpoint]['local_date'] for item in matched
                for endpoint in ('lower_features','upper_features')}),
            'quantile_method':arithmetic['quantile_method'], 'conditioning':arithmetic['conditioning'],
            'definite_survivors':arithmetic['definite_survivors'], 'possible_survivors':arithmetic['possible_survivors'],
            'wait_quantile_envelopes_us':quantiles, 'representative_wait_us':point,
            'representative_method':'midpoint_of_estimated_median_envelope_not_individual_labels',
            'representative_call_at':_utc(base+timedelta(microseconds=point)),
            'median_call_envelope':stamps(median['lower_us'],median['upper_us']),
            'empirical_p10_p90_call_span':stamps(quantiles['p10']['lower_us'],quantiles['p90']['upper_us']),
            'coverage_calibrated':False, 'forecast_error_measured':False}
    return {'research_forecast_available':False,
        'reason_code':'insufficient_definite_survivors' if elapsed_us is not None and
            len(pool) >= plan['minimum_samples'] else 'insufficient_matching_history',
        'matched_group':None, 'coarse_fallback':None, 'matching_samples':0,
        'minimum_samples':plan['minimum_samples'], 'minimum_is_quality_guarantee':False,
        'group_attempts':attempts, 'target_date_features':wanted, 'wait_quantile_envelopes_us':None,
        'representative_wait_us':None, 'representative_call_at':None,
        'median_call_envelope':None, 'empirical_p10_p90_call_span':None,
        'coverage_calibrated':False, 'forecast_error_measured':False}


def build_baseline(*, source, reviews, plan, max_revisions=10_000):
    """Build a private, deterministic research candidate from cutoff-known claims."""
    plan = validate_plan(plan)
    if type(max_revisions) is not int or not 1 <= max_revisions <= 10_000:
        raise BaselineError('baseline_invalid_plan')
    try:
        audit, candidates = reviews._selection(source=source, as_of=plan['as_of'],
            data_origin=plan['data_origin'], api_profile=plan['api_profile'], max_revisions=max_revisions)
        pool, excluded = _prepare(candidates, plan)
        fingerprint = hashlib.sha256(_canonical({'policy':POLICY,'plan':plan,
            'calendar_at_cutoff':_features(plan['as_of'],as_of=plan['as_of']),
            'pool':sorted(pool,key=lambda item:item['source_receipt_sha256'])}).encode()).hexdigest()
        cutoff = _time(plan['as_of'])
        if plan['mode'] == 'new_join':
            forecast = _forecast(pool,plan,plan['as_of'])
        elif plan['mode'] == 'remaining':
            forecast = _forecast(pool,plan,plan['issued_at'], elapsed_us=_us(cutoff-_time(plan['issued_at'])))
        else:
            target = _time(plan['desired_arrival_at'])+timedelta(minutes=plan['call_offset_minutes'])
            grid, eligible = [], []
            for index, issued in enumerate(plan['candidate_issue_times']):
                value = _forecast(pool,plan,issued)
                candidate = {'candidate_index':index, 'candidate_issue_at':issued, 'forecast':value,
                    'median_distance_to_target_us':None}
                if value['research_forecast_available']:
                    bounds = value['median_call_envelope']
                    lo,hi = _time(bounds['lower']),_time(bounds['upper'])
                    distance = {'lower_us':max(0,_us(lo-target),_us(target-hi)),
                        'upper_us':max(abs(_us(lo-target)),abs(_us(hi-target)))}
                    candidate['median_distance_to_target_us'] = distance
                    eligible.append((distance['upper_us'],index))
                grid.append(candidate)
            chosen = min(eligible)[1] if eligible else None
            forecast = {'research_forecast_available':chosen is not None,
                'reason_code':None if chosen is not None else 'insufficient_matching_history',
                'desired_arrival_at':plan['desired_arrival_at'], 'target_call_at':_utc(target),
                'call_offset_minutes':plan['call_offset_minutes'], 'candidates':grid,
                'selected_candidate_index':chosen,
                'candidate_issue_at':grid[chosen]['candidate_issue_at'] if chosen is not None else None,
                'selection_method':'minimum_worst_median_envelope_distance_then_earliest_candidate',
                'search_scope':'caller_candidates_only','global_optimality_verified':False,
                'candidate_bookability_verified':False,'monotonicity_assumed':False}
        return {'baseline_schema_version':1,'baseline_policy':POLICY,'plan':plan,
            'historical_input_sha256':fingerprint,'eligible_static_cohort_samples':len(pool),
            'excluded_samples':excluded,'forecast':forecast,
            'audit_diagnostics_not_features':audit,
            'availability_basis':'local_source_and_review_first_receipts',
            'authenticity_verified':False,'verified_training_labels':0,'training_eligible':False,
            'historical_availability_verified':False,'production_forecast_log':False,
            'eta_available':False,'uses_realtime_queue_data':False,'source_freshness':'unknown',
            'population_representativeness_verified':False,'right_censoring_modelled':False,
            'network_performed':False,'booking_or_cancellation_performed':False,
            'output_requires_private_handling':True}
    except BaselineError:
        raise
    except ReviewError:
        raise BaselineError('baseline_source_invalid') from None
    except Exception:
        raise BaselineError('baseline_operation_failed') from None


def write_baseline(*, source, reviews, plan, destination, max_revisions=10_000):
    result = build_baseline(source=source,reviews=reviews,plan=plan,max_revisions=max_revisions)
    body = _canonical(result).encode()
    if len(body) > 2_097_152:
        raise BaselineError('baseline_operation_failed')
    try:
        publication = _write_packet(body,destination)
    except PacketError as error:
        raise BaselineError('baseline_output_exists' if error.error_code == 'packet_output_exists'
            else 'baseline_operation_failed', committed=error.committed) from None
    return {'baseline_schema_version':1,'baseline_policy':POLICY,'mode':plan['mode'],
        'artifact_written':True,'committed':True,'durability_confirmed':publication['durability_confirmed'],
        'research_forecast_available':result['forecast']['research_forecast_available'],
        'eligible_static_cohort_samples':result['eligible_static_cohort_samples'],
        'authenticity_verified':False,'verified_training_labels':0,'eta_available':False,
        'network_performed':False,'output_requires_private_handling':True}
