"""Public display trend advice shared across tickets, with a finite paid ledger.

Only this module's explicit aggregate whitelist is transmitted. No personal
ticket, issue time, account, request header or original response is accepted.
The browser mixes accepted weights with its own private calculation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import median
import time

from .deepseek import DeepSeekError, MODEL, _Ledger, _cost, _https_post, _key, _usage, _wire
from .displayreplay import SOURCE, _boundary, _iso, _points, _time
from .intake import _canonical
from .outcomes import _json

POLICY = 'public_display_scenarios_v1'
CANDIDATES = {'history', 'steady', 'fast', 'slow'}
REASONS = {'history_dominant', 'rapid_display_turnover', 'slower_display_turnover',
           'mixed_evidence', 'insufficient_evidence'}
FEATURES = {'rate_recent', 'rate_30', 'rate_60', 'history_rate_median', 'sample_count'}


def public_projection(value):
    """Do not serve unknown fields or a mistakenly configured credential file."""
    keys={'schema_version','policy','model','status','stores','updated_at'}
    if (type(value) is not dict or set(value)-keys or not keys-{'updated_at'}<=set(value)
            or value['schema_version']!=1 or value['policy']!=POLICY or value['model']!=MODEL
            or value['status'] not in ('waiting_for_configuration','waiting_for_open','waiting_for_observation','ready','finished','budget_exhausted')
            or type(value['stores']) is not dict or set(value['stores'])-{'3014','3004','2009','3015','2001'}):
        raise ValueError('invalid_public_model_projection')
    fields={'schema_version','policy','source','store_id','queue_type','local_date','sample_at','generated_at','expires_at',
            'model','thinking_mode','weights_ppm','reason_code','feature_ids','phase','public_input_sha256'}
    for store,queues in value['stores'].items():
        if type(queues) is not dict or set(queues)-{'ordinary','reservation'}:raise ValueError('invalid_model_queue')
        for queue,modes in queues.items():
            if type(modes) is not dict or set(modes)-{'nonthinking','thinking'}:raise ValueError('invalid_model_modes')
            for mode,row in modes.items():
                if type(row) is not dict or set(row)!={'state','advice'} or type(row['state']) is not str or len(row['state'])>80:raise ValueError('invalid_model_row')
                advice=row['advice']
                if advice is None:continue
                if (type(advice) is not dict or set(advice)!=fields or advice['schema_version']!=1 or advice['policy']!=POLICY
                        or advice['source']!=SOURCE or advice['store_id']!=store or advice['queue_type']!=queue
                        or advice['thinking_mode']!=mode or advice['model']!=MODEL or advice['phase']!='live_shared'
                        or advice['reason_code'] not in REASONS or set(advice['weights_ppm'])!=CANDIDATES
                        or any(type(n) is not int or not 0<=n<=1000000 for n in advice['weights_ppm'].values())
                        or sum(advice['weights_ppm'].values())!=1000000 or type(advice['feature_ids']) is not list
                        or not set(advice['feature_ids'])<=FEATURES):raise ValueError('invalid_model_advice')
                for key in ('sample_at','generated_at','expires_at'):_time(advice[key])
                datetime.strptime(advice['local_date'],'%Y-%m-%d')
    if len(_canonical(value).encode())>32768:raise ValueError('model_projection_too_large')
    return value


def public_context(detail, *, queue='ordinary', history=(), now=None, cadence=600, connection_check=False):
    now = now or datetime.now(timezone.utc)
    if queue not in ('ordinary', 'reservation') or type(cadence) is not int or not 180 <= cadence <= 600:
        raise ValueError('invalid_model_scope')
    field = 'mixedQueue' if queue == 'ordinary' else 'reservationQueue'
    points = _points(detail, field)
    segment, last_nonempty_segment = [], []
    for p in points:
        if p['reason']:
            segment = []; continue
        if segment and _boundary(segment[-1], p, 660):
            segment = []
        segment.append(p)
        last_nonempty_segment = segment[:]
    if connection_check and not segment:
        segment = last_nonempty_segment
    if not segment:
        raise ValueError('model_no_current_display')
    end = segment[-1]
    if end['at'] > now or not connection_check and (now-end['at']).total_seconds() > cadence+60:
        raise ValueError('model_display_stale')
    segment = [p for p in segment if p['at'] >= end['at']-timedelta(hours=1)]
    def rate(a, b):
        return (b['position']-a['position'])*60/(b['at']-a['at']).total_seconds()
    values = {'sample_count': len(segment), 'rate_recent': rate(*segment[-2:]) if len(segment)>1 else None,
              'rate_30': None, 'rate_60': None, 'history_rate_median': None}
    for minutes in (30, 60):
        rows = [p for p in segment if p['at'] >= end['at']-timedelta(minutes=minutes)]
        if 4 <= len(rows) <= 128 and (rows[-1]['at']-rows[0]['at']).total_seconds() >= 900:
            values['rate_'+str(minutes)] = median(rate(a,b) for i,a in enumerate(rows) for b in rows[i+1:])
    prior_rates = []
    for past in list(history)[:7]:
        if past.get('requested_store_id') != detail['requested_store_id'] or past.get('local_date') >= detail['local_date']:
            continue
        try:
            rows = _points(past, field)
        except (ValueError, KeyError):
            continue
        for a,b in zip(rows,rows[1:]):
            if not a['reason'] and not b['reason'] and not _boundary(a,b,660):
                prior_rates.append(rate(a,b))
    if prior_rates:
        values['history_rate_median'] = median(prior_rates)
    features = [{'feature_id':key, 'value':round(value,6) if value is not None else None}
                for key,value in sorted(values.items())]
    return {'schema_version':1, 'policy':POLICY, 'source':SOURCE,
        'store_id':detail['requested_store_id'], 'queue_type':queue, 'local_date':detail['local_date'],
        'sample_at':_iso(end['at']), 'observation_id':end['id'],
        'date_type':detail.get('summary',{}).get('date_type','unknown') if detail.get('summary') else 'unknown',
        'features':features, 'candidate_ids':sorted(CANDIDATES), 'cadence_seconds':cadence,
        'source_update_time_verified':False, 'phase':'historical_connection_check' if connection_check else 'live_shared'}


def _validate_context(value):
    fields = {'schema_version','policy','source','store_id','queue_type','local_date','sample_at',
              'observation_id','date_type','features','candidate_ids','cadence_seconds','source_update_time_verified','phase'}
    if (type(value) is not dict or set(value)!=fields or value['schema_version']!=1 or value['policy']!=POLICY
            or value['source']!=SOURCE or value['queue_type'] not in ('ordinary','reservation')
            or value['candidate_ids']!=sorted(CANDIDATES) or value['source_update_time_verified'] is not False
            or value['phase'] not in ('live_shared','historical_connection_check')
            or type(value['cadence_seconds']) is not int or not 180<=value['cadence_seconds']<=600
            or type(value['store_id']) is not str or not value['store_id'].isdigit()
            or type(value['observation_id']) is not str or not 1<=len(value['observation_id'])<=128
            or value['date_type'] not in ('ordinary_workday','ordinary_weekend','holiday','makeup_workday','unknown')):
        raise ValueError('invalid_public_model_context')
    _time(value['sample_at']);datetime.strptime(value['local_date'],'%Y-%m-%d')
    features=value['features']
    if (type(features) is not list or len(features)!=len(FEATURES)
            or {f.get('feature_id') for f in features}!=FEATURES):
        raise ValueError('invalid_public_model_features')
    for f in features:
        if (set(f)!={'feature_id','value'} or f['value'] is not None and
                (type(f['value']) not in (int,float) or not math.isfinite(f['value']) or not 0<=f['value']<=1000000)):
            raise ValueError('invalid_public_model_features')
    return value


def suggest_weights(context, *, budget_file, key_file, now=None, transport=None):
    """One durable attempt per public observation/mode. No automatic retries."""
    context=_validate_context(context);now=now or datetime.now(timezone.utc)
    if (_time(context['sample_at'])>now or context['phase']=='live_shared' and
            (now-_time(context['sample_at'])).total_seconds()>context['cadence_seconds']+60):
        raise ValueError('model_display_stale')
    digest=hashlib.sha256(_canonical(context).encode()).hexdigest()
    identity={k:context[k] for k in ('policy','source','store_id','queue_type','local_date','observation_id','sample_at','phase')}
    observation_key=hashlib.sha256(_canonical(identity).encode()).hexdigest()
    ledger=_Ledger(budget_file,now)
    mode=ledger.budget.get('thinking_mode','nonthinking')
    request={**context,'public_input_sha256':digest}
    try:
        # The old key included smooth cadence, which can change between two
        # queries. Recognize its bounded cadence variants without rewriting
        # any old successful, failed or unknown reservation.
        legacy_keys=None
        for call,result in ledger.calls:
            match=call['observation_key'] in (observation_key,digest)
            if not match:
                if legacy_keys is None:
                    legacy_keys={hashlib.sha256(_canonical({**context,'cadence_seconds':n}).encode()).hexdigest() for n in range(180,601)}
                match=call['observation_key'] in legacy_keys
            if match:
                return {'state':'cached' if result and result['advice'] else 'previous_attempt_unknown',
                        'advice':result['advice'] if result else None,'network_performed':False,
                        'attempts_used':len(ledger.calls),'thinking_mode':mode}
        body=_wire(request,ledger.budget['max_output_tokens'],mode)
        if len(body)+4096>ledger.budget['max_prompt_tokens']:
            raise DeepSeekError('deepseek_input_too_large')
        key=_key(key_file);ledger.check_budget(now);ledger.reserve(request,observation_key,now)
        began=time.monotonic();raw=(transport or _https_post)(body,key,ledger.budget['timeout_seconds'])
        response=_json(raw);usage=_usage(response);advice=None;error=None
        overrun=usage['prompt_tokens']>ledger.budget['max_prompt_tokens'] or usage['completion_tokens']>ledger.budget['max_output_tokens']
        ended=datetime.now(timezone.utc) if transport is None else now
        try:
            if overrun or mode=='nonthinking' and usage['reasoning_tokens'] or time.monotonic()-began>=ledger.budget['timeout_seconds']:
                raise ValueError('invalid_usage_or_timeout')
            choices=response['choices'];assert response['object']=='chat.completion' and len(choices)==1
            assert response['model'] in (MODEL,'deepseek-v4.1-flash','DeepSeek-V4.1-Flash')
            choice=choices[0];message=choice['message'];assert choice['finish_reason']=='stop' and choice['index']==0 and message['role']=='assistant'
            assert not message.get('tool_calls') and (mode=='thinking' or not message.get('reasoning_content'))
            answer=_json(message['content']);assert set(answer)=={'weights_ppm','feature_ids','reason_code'}
            weights=answer['weights_ppm'];assert set(weights)==CANDIDATES and all(type(v) is int and 0<=v<=1000000 for v in weights.values()) and sum(weights.values())==1000000
            known={f['feature_id'] for f in context['features'] if f['value'] is not None}
            assert type(answer['feature_ids']) is list and 1<=len(answer['feature_ids'])<=len(known) and len(set(answer['feature_ids']))==len(answer['feature_ids']) and set(answer['feature_ids'])<=known
            assert answer['reason_code'] in REASONS
            ledger.check_budget(ended)
            expiry=_time(context['sample_at'])+timedelta(seconds=context['cadence_seconds']+60)
            advice={'schema_version':1,'policy':POLICY,'source':SOURCE,'store_id':context['store_id'],
                'queue_type':context['queue_type'],'local_date':context['local_date'],'sample_at':context['sample_at'],
                'generated_at':_iso(ended),'expires_at':_iso(expiry),'model':MODEL,'thinking_mode':mode,
                'weights_ppm':weights,'reason_code':answer['reason_code'],'feature_ids':answer['feature_ids'],
                'phase':context['phase'],'public_input_sha256':digest}
        except Exception:
            error='model_response_not_accepted'
        ledger.publish(f'deepseek-result-{len(ledger.calls)}.json',{'public_input_sha256':digest,
            'transport_basis':'official_https' if transport is None else 'injected_transport','usage':usage,'advice':advice,'overrun':overrun})
        return {'state':'ready' if advice else 'fallback','advice':advice,'network_performed':transport is None,
            'attempts_used':len(ledger.calls),'thinking_mode':mode,'error_code':error,
            'usage':usage,'estimated_cost_microunits':_cost(usage['prompt_tokens'],usage['completion_tokens'],ledger.budget),
            'latency_seconds':round(time.monotonic()-began,3),'provider_response_model':response.get('model')}
    finally:
        ledger.close()
