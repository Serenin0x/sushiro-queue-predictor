"""Chronological historical baseline replay, never an authenticated forecast log."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import re
from uuid import uuid4

from .baseline import BaselineError, MAX_WAIT_US, build_baseline
from .credentials import _read_private_file
from .evaluation import evaluate_intervals
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet
from .reviews import ReviewError

POLICY = 'reviewed_history_chronological_replay_v1'
_FIELDS = {'schema_version','as_of','data_origin','api_profile','store_id',
    'minimum_samples','elapsed_seconds','max_cases'}
_CODES = frozenset({'backtest_invalid_plan','backtest_invalid_input','backtest_case_limit',
    'backtest_source_invalid','backtest_output_exists','backtest_operation_failed'})


class BacktestError(ValueError):
    def __init__(self,code,*,committed=False):
        self.error_code=code if type(code) is str and code in _CODES else 'backtest_operation_failed'
        self.committed=committed is True
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def validate_backtest_plan(value,*,now=None):
    try:
        if (type(value) is not dict or set(value)!=_FIELDS
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['data_origin'] not in ('synthetic','self_reported')
                or value['api_profile'] not in ('legacy','miniapp_gateway')
                or type(value['store_id']) is not str or re.fullmatch('[1-9][0-9]{0,9}',value['store_id']) is None
                or int(value['store_id'])>2**31-1
                or type(value['minimum_samples']) is not int or not 1<=value['minimum_samples']<=1000
                or type(value['max_cases']) is not int or not 1<=value['max_cases']<=100):
            raise ValueError
        cutoff=_time(value['as_of'])
        if cutoff>(_clock() if now is None else now):
            raise ValueError
        elapsed=value['elapsed_seconds']
        if (type(elapsed) is not list or not 1<=len(elapsed)<=16
                or any(type(e) is not int or not 0<=e<=MAX_WAIT_US//1_000_000 for e in elapsed)
                or any(b<=a for a,b in zip(elapsed,elapsed[1:]))):
            raise ValueError
        return {**value,'as_of':_utc(cutoff)}
    except (ValueError,TypeError,KeyError,OverflowError):
        raise BacktestError('backtest_invalid_plan') from None


def read_backtest_plan(path):
    try:
        body=_read_private_file(path)
        if len(body)>16_384:
            raise ValueError
        return validate_backtest_plan(_json(body))
    except Exception:
        raise BacktestError('backtest_invalid_input') from None


def build_backtest(*,source,reviews,plan,max_revisions=10_000):
    plan=validate_backtest_plan(plan)
    if type(max_revisions) is not int or not 1<=max_revisions<=10_000:
        raise BacktestError('backtest_invalid_plan')
    try:
        audit,targets=reviews._selection(source=source,as_of=plan['as_of'],data_origin=plan['data_origin'],
            api_profile=plan['api_profile'],max_revisions=max_revisions)
        scoped=[t for t in targets if t['episode']['store_id']==plan['store_id']]
        work=[]
        excluded=Counter()
        excluded_by_elapsed=Counter()
        for target in scoped:
            ep=target['episode']
            events={v['event_type']:v for v in ep['events']}
            issued=_time(events['issued']['event_time_upper'])
            called=_time(events['called']['event_time_lower'])
            for elapsed in plan['elapsed_seconds']:
                made=issued+timedelta(seconds=elapsed)
                if made>=called:
                    excluded['call_not_definitely_future']+=1
                    excluded_by_elapsed[elapsed]+=1
                    continue
                work.append((made,elapsed,target))
        if len(work)>plan['max_cases']:
            raise BacktestError('backtest_case_limit')
        work.sort(key=lambda row:(row[0],row[2]['source_receipt_sha256'],row[1]))
        scores={e:[] for e in plan['elapsed_seconds']}
        diagnostics={e:Counter() for e in plan['elapsed_seconds']}
        cases=[]
        for made,elapsed,target in work:
            ep=target['episode']
            events={v['event_type']:v for v in ep['events']}
            request={'schema_version':1,'as_of':_utc(made),'data_origin':plan['data_origin'],
                'api_profile':plan['api_profile'],'store_id':plan['store_id'],'queue_type':ep['queue_type'],
                'party_size':ep['party_size'],'table_type':ep['table_type'],
                'mode':'new_join' if elapsed==0 else 'remaining','minimum_samples':plan['minimum_samples'],
                'target_episode_id':ep['episode_id']}
            if elapsed:
                request.update(issued_at=events['issued']['event_time_upper'],call_not_observed=True)
            result=build_baseline(source=source,reviews=reviews,plan=request,max_revisions=max_revisions)
            forecast=result['forecast']
            case={'episode_id':ep['episode_id'],'elapsed_seconds':elapsed,'reconstructed_prediction_at':_utc(made),
                'historical_input_sha256':result['historical_input_sha256'],
                'truth_source_receipt_sha256':target['source_receipt_sha256'],'truth_available_at':target['available_at'],
                'research_forecast_available':forecast['research_forecast_available'],'reason_code':forecast['reason_code'],
                'matched_group':forecast['matched_group'],'matching_samples':forecast['matching_samples'],
                'coarse_fallback':forecast['coarse_fallback'],'evaluation_record':None}
            if forecast['research_forecast_available']:
                span=forecast['empirical_p10_p90_call_span']
                record={'prediction_id':str(uuid4()),'prediction_made_at':_utc(made),
                    'point_call_at':forecast['representative_call_at'],
                    'predicted_call_lower':span['lower'],'predicted_call_upper':span['upper'],
                    'observed_call_lower':events['called']['event_time_lower'],
                    'observed_call_upper':events['called']['event_time_upper'],
                    'observation_received_at':target['available_at']}
                scores[elapsed].append(record)
                diagnostics[elapsed]['scored_cases']+=1
                case['evaluation_record']=record
            else:
                diagnostics[elapsed][forecast['reason_code']]+=1
            cases.append(case)
        evaluations=[{'elapsed_seconds':e,'attempted_cases':sum(diagnostics[e].values()),
            'excluded_call_not_definitely_future':excluded_by_elapsed[e],
            'availability_counts':dict(sorted(diagnostics[e].items())),
            'interval_evaluation':evaluate_intervals(scores[e],as_of=plan['as_of'],data_origin=plan['data_origin'])}
            for e in plan['elapsed_seconds']]
        fingerprint_cases=[{**c,'evaluation_record':{k:v for k,v in c['evaluation_record'].items()
            if k!='prediction_id'} if c['evaluation_record'] else None} for c in cases]
        fingerprint=hashlib.sha256(_canonical({'policy':POLICY,'plan':plan,'cases':fingerprint_cases}).encode()).hexdigest()
        return {'backtest_schema_version':1,'backtest_policy':POLICY,'plan':plan,
            'historical_replay_sha256':fingerprint,'reviewed_scope_episodes':len(scoped),
            'attempted_cases':len(cases),'scored_cases':sum(len(v) for v in scores.values()),
            'excluded_cases':dict(sorted(excluded.items())),'evaluations_by_elapsed_seconds':evaluations,
            'cases':cases,'audit_diagnostics_not_features':audit,
            'prediction_time_basis':'issued_interval_upper_plus_elapsed_reconstruction',
            'target_context_basis':'reviewed_outcome_reconstruction_not_historical_request',
            'historical_target_context_verified':False,
            'truth_policy':'latest_reviewed_claim_received_by_evaluation_cutoff',
            'fit_policy':'expanding_cutoff_known_reviewed_history_excluding_target_episode',
            'lead_groups_are_independent_samples':False,'population_representativeness_verified':False,
            'forecast_log_verified':False,'model_performance_verified':False,'authenticity_verified':False,
            'verified_training_labels':0,'coverage_calibrated':False,'eta_available':False,
            'right_censoring_modelled':False,'ideal_time_planning_evaluated':False,
            'output_requires_private_handling':True,'network_performed':False}
    except BacktestError:
        raise
    except (BaselineError,ReviewError):
        raise BacktestError('backtest_source_invalid') from None
    except Exception:
        raise BacktestError('backtest_operation_failed') from None


def write_backtest(*,source,reviews,plan,destination,max_revisions=10_000):
    result=build_backtest(source=source,reviews=reviews,plan=plan,max_revisions=max_revisions)
    body=_canonical(result).encode()
    if len(body)>2_097_152:
        raise BacktestError('backtest_operation_failed')
    try:
        publication=_write_packet(body,destination)
    except PacketError as error:
        raise BacktestError('backtest_output_exists' if error.error_code=='packet_output_exists'
            else 'backtest_operation_failed',committed=error.committed) from None
    return {'backtest_schema_version':1,'artifact_written':True,'committed':True,
        'durability_confirmed':publication['durability_confirmed'],'reviewed_scope_episodes':result['reviewed_scope_episodes'],
        'attempted_cases':result['attempted_cases'],'scored_cases':result['scored_cases'],
        'forecast_log_verified':False,'model_performance_verified':False,'verified_training_labels':0,
        'eta_available':False,'output_requires_private_handling':True,'network_performed':False}
