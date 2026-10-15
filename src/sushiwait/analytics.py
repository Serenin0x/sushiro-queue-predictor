"""Read saved public archives only; separate activity from actual customer flow."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
import hashlib
import json
import math
import re
import time
from statistics import median

from .calendar import date_features
from .cadence import CHINA

POLICY = 'store_readiness_v1'
SOURCES = {'sapi_miniapp_gateway': '/api/v1/official', 'crm_remote_v1_1': '/api/v1'}
CITIES = json.loads(files('sushiwait').joinpath('data/screenshot-cities.json').read_text())


def selection(value, now=None):
    now = now or datetime.now(timezone.utc)
    if type(value) is not dict or set(value) != {'from', 'to', 'source', 'queue', 'store_id'}:
        raise ValueError('invalid_analysis_selection')
    for key in ('from', 'to'):
        if type(value[key]) is not str or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', value[key]):
            raise ValueError('invalid_analysis_date')
    start, end = (date.fromisoformat(value[k]) for k in ('from', 'to'))
    if not start <= end <= now.astimezone(CHINA).date() or (end-start).days > 365:
        raise ValueError('invalid_analysis_range')
    if (value['source'] not in SOURCES or value['queue'] not in {'ordinary', 'reservation'}
            or type(value['store_id']) is not str or value['store_id'] and not re.fullmatch(r'[1-9]\d{0,9}',value['store_id'])):
        raise ValueError('invalid_analysis_scope')
    return dict(value)


def _time(value):
    if not isinstance(value, str): return None
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return at if at.utcoffset() is not None else None
    except ValueError: return None


def opening(day, now):
    local = datetime.combine(day, datetime.min.time(), CHINA)
    holiday = date_features(local.isoformat(), as_of=now.isoformat())['date_type'] == 'holiday'
    early = day.weekday() >= 5 or holiday
    return local.replace(hour=10 if early else 11, minute=30 if early else 0), min(local.replace(hour=22), now.astimezone(CHINA))


def summarize_day(detail, queue, now):
    """Coverage is occupied 15-minute slots, not proof of complete collection."""
    day = date.fromisoformat(detail['local_date'])
    start, end = opening(day, now)
    expected = max(0, math.ceil((end-start).total_seconds()/900))
    field = 'mixedQueue' if queue == 'ordinary' else 'reservationQueue'
    bins, unique, hours, states, gaps, rates = set(), set(), {}, Counter(), [], []
    previous, previous_time = None, None
    raw = detail.get('points') or []
    points = sorted(raw, key=lambda p:p.get('queue_received_at') or p.get('request_started_at') or p.get('slot_started_at') or '')
    times = [_time(p.get('queue_received_at') or p.get('request_started_at') or p.get('slot_started_at')) for p in points]
    diffs = [(b-a).total_seconds() for a,b in zip(times,times[1:]) if a and b and b>a]
    # Older campaigns intentionally used 15-minute sampling. Never call that an outage.
    gap_limit = min(1800, max(660, median(diffs)*1.8 if diffs else 660))
    for p, at in zip(points,times):
        if at is None or at > now or at.astimezone(CHINA).date() != day: continue
        labels = (p.get('queues') or {}).get(field)
        state = ('paused' if p.get('scheduled_pause') else 'failed' if p.get('pair_ok') is not True
            else 'missing' if not isinstance(labels,list) else 'empty' if not labels else 'visible')
        if (p.get('queue_field_presence') or {}).get(field) in {'missing','absent'} and state in ('visible','empty'):state='missing'
        if state == 'visible' and any(not isinstance(n,str) or not re.fullmatch(r'\d{1,7}',n) for n in labels): state='missing'
        states[state] += 1
        hour = at.astimezone(CHINA).hour
        h = hours.setdefault(hour, {'observations':0, 'visible':0, 'empty':0, 'failed':0, 'changes':0, 'compared_minutes':0., 'advance':0, 'rates':[]})
        h['observations'] += 1
        if state in ('visible','empty'):
            h[state] += 1
            if start <= at < end: bins.add(int((at-start).total_seconds()//900))
        elif state in ('failed','missing'): h['failed'] += 1
        if previous_time and at > previous_time and (at-previous_time).total_seconds()>gap_limit:
            gaps.append({'kind':'sampling_gap','from':previous_time.isoformat(),'to':at.isoformat(),'minutes':round((at-previous_time).total_seconds()/60,1)})
        if state == 'visible':
            unique.update(labels)
            if previous and previous['state']=='visible' and 0 < (at-previous['at']).total_seconds() <= gap_limit:
                prior=previous['p']; link=p.get('continuity') or {}; old_link=prior.get('continuity') or {}
                official = detail.get('source') == 'sapi_miniapp_gateway'
                comparable = (link.get('writer_id') and link.get('writer_id')==old_link.get('writer_id')
                    and link.get('predecessor_writer_id')==old_link.get('writer_id') and link.get('predecessor_id')==prior.get('observation_id')) if official else False
                delta=int(labels[0])-int(previous['labels'][0]); minutes=(at-previous['at']).total_seconds()/60
                if comparable and delta>=0 and not detail.get('graph_truncated'):
                    rate=delta/minutes; rates.append(rate); h['rates'].append(rate)
                    h['compared_minutes']+=minutes; h['advance']+=delta
                    h['changes']+=int(labels != previous['labels'])
                    if delta==0: gaps.append({'kind':'no_progress','from':previous['at'].isoformat(),'to':at.isoformat(),'minutes':round(minutes,1)})
                elif comparable and delta<0: states['reversal']+=1
            previous={'state':state,'p':p,'at':at,'labels':labels}
        else:
            previous={'state':state,'p':p,'at':at,'labels':labels}
            if state=='empty': gaps.append({'kind':'empty_display','from':at.isoformat(),'to':at.isoformat(),'minutes':0})
        previous_time=at
    packed=[]
    for hour,h in sorted(hours.items()):
        packed.append({'hour':hour,**{k:v for k,v in h.items() if k!='rates'},
            'rate':round(median(h['rates']),2) if h['rates'] else None})
    return {'date':day.isoformat(),'observations':sum(states[s] for s in ('visible','empty','failed','missing','paused')),
        'successful':states['visible']+states['empty'],'visible':states['visible'],'empty':states['empty'],
        'failed':states['failed'],'missing':states['missing'],'paused':states['paused'],'reversals':states['reversal'],
        'covered_slots':len(bins),'expected_slots':expected,'unique_displayed':len(unique),
        'rate':round(median(rates),2) if rates else None,'comparable_intervals':len(rates),
        'hours':packed,'gaps':gaps,'truncated':bool(detail.get('graph_truncated')),
        'last_sample_at':previous_time.isoformat() if previous_time else None}


def build_report(reader, names, value, *, now=None, time_limit=25):
    now = now or datetime.now(timezone.utc); value=selection(value,now)
    began=time.monotonic(); base=SOURCES[value['source']]
    names={k:v for k,v in names.items() if not value['store_id'] or k==value['store_id']}
    if not names: raise ValueError('analysis_store_not_available')
    start,end=(date.fromisoformat(value[k]) for k in ('from','to'))
    # Read only dates listed by each saved source. Missing ranges remain missing.
    months=[];cursor=start.replace(day=1)
    while cursor<=end:
        months.append(cursor.strftime('%Y-%m'));cursor=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
    available={}; errors=[]; incomplete=False
    for month in months:
        try:
            index=reader(f'{base}/months/{month}')
            if index.get('source')!=value['source']: raise ValueError('source_mismatch')
            for missing in index.get('unavailable_store_ids',[]):
                if missing in names:errors.append({'date':month,'store_id':missing,'kind':'index_unavailable'})
            for day, stores in index.get('days',{}).items():
                if value['from']<=day<=value['to']:
                    for store in stores:
                        if store in names: available.setdefault(store,[]).append(day)
        except Exception: errors.append({'date':month,'kind':'index_unavailable'})
        if time.monotonic()-began>time_limit: incomplete=True;break
    rows=[]
    for store,name in names.items():
        days=[]; unread=0; unread_dates=[]
        for day in sorted(set(available.get(store,[]))):
            if time.monotonic()-began>time_limit: incomplete=True;unread+=1;unread_dates.append(day);continue
            try:
                detail=reader(f'{base}/stores/{store}/days/{day}')
                if detail.get('source')!=value['source'] or detail.get('requested_store_id')!=store or detail.get('local_date')!=day:
                    raise ValueError('analysis_detail_scope')
                days.append(summarize_day(detail,value['queue'],now))
            except Exception: unread+=1;unread_dates.append(day)
        total={k:sum(d[k] for d in days) for k in ('observations','successful','visible','empty','failed','missing','paused','unique_displayed','covered_slots','comparable_intervals','reversals')}
        expected=0;coverage_start=min((date.fromisoformat(d['date']) for d in days if d['observations']),default=start);cursor=max(start,coverage_start)
        while cursor<=end:
            a,b=opening(cursor,now);expected+=max(0,math.ceil((b-a).total_seconds()/900));cursor+=timedelta(days=1)
        coverage=round(100*total['covered_slots']/expected,1) if expected else None
        success=round(100*total['successful']/total['observations'],1) if total['observations'] else None
        observed_days=sum(d['observations']>0 for d in days)
        weekday_days=len({date.fromisoformat(d['date']).weekday() for d in days if d['successful']})
        parts={'coverage':round((coverage or 0)*.4,1),'history':round(min(observed_days/14,1)*25,1),
            'continuity':round(min(total['comparable_intervals']/200,1)*20,1),'weekday_diversity':round(weekday_days/7*15,1)}
        score=round(sum(parts.values())); gaps=[{'date':d['date'],**g} for d in days for g in d['gaps']]
        hours=[]
        for hour in range(10,22):
            hs=[h for d in days for h in d['hours'] if h['hour']==hour]
            htotal={k:sum(h[k] for h in hs) for k in ('observations','visible','empty','failed','changes','compared_minutes','advance')}
            hours.append({'hour':hour,**htotal,'rate':round(htotal['advance']/htotal['compared_minutes'],2) if htotal['compared_minutes'] else None,
                'active_ratio':round(100*htotal['visible']/(htotal['visible']+htotal['empty']),1) if htotal['visible']+htotal['empty'] else None,
                'days':len(hs)})
        peaks=sorted((h for h in hours if h['rate'] is not None),key=lambda h:h['rate'],reverse=True)[:3]
        suggestions=[]
        if unread or errors: suggestions.append({'priority':'high','code':'archive_read','text':'先恢复缺失归档的读取，再比较门店。'})
        if (coverage or 0)<80: suggestions.append({'priority':'high','code':'coverage','text':'优先补齐营业时段覆盖，保留每次空响应与失败。'})
        if observed_days<14: suggestions.append({'priority':'high','code':'history','text':'继续积累至少两周，覆盖工作日与周末。'})
        if total['comparable_intervals']<200: suggestions.append({'priority':'medium','code':'continuity','text':'积累连续可比的叫号片段，先修复中断再调整频率。'})
        suggestions.append({'priority':'medium','code':'wait_labels','text':'补充正常取号时间与展示到号区间，区分签到状态并开展前瞻误差验证。'})
        if weekday_days<7: suggestions.append({'priority':'medium','code':'weekday','text':'补齐不同星期的样本，节假日单独统计。'})
        rows.append({'store_id':store,'store_name':name,'city':CITIES.get(store,'城市待核'),**total,
            'coverage_from':coverage_start.isoformat(),'unread_dates':unread_dates,'expected_slots':expected,'coverage_percent':coverage,'success_percent':success,'days_observed':observed_days,
            'days_missing':(end-start).days+1-observed_days,'read_failures':unread,'readiness_score':score,'score_parts':parts,
            'readiness_level':'待积累' if score<40 else '可做初步趋势' if score<70 else '可开展预测验证',
            'prediction_accuracy':None,'daily':[{k:v for k,v in d.items() if k!='gaps'} for d in days],'hourly':hours,'peak_hours':[h['hour'] for h in peaks],
            'gaps':gaps[-200:],'gap_count':len(gaps),'sampling_gaps':sum(g['kind']=='sampling_gap' for g in gaps),
            'suggestions':suggestions,'last_sample_at':max((d['last_sample_at'] for d in days if d['last_sample_at']),default=None)})
    report={'schema_version':1,'policy':POLICY,'selection':value,'generated_at':now.isoformat(),'stores':rows,
        'archive_start':min((d for ds in available.values() for d in ds),default=None),'archive_end':max((d for ds in available.values() for d in ds),default=None),
        'errors':errors,'incomplete':incomplete or bool(errors) or any(r['read_failures'] or any(d['truncated'] for d in r['daily']) for r in rows),
        'semantics':{'activity':'sampled_display_activity_not_customer_count','coverage':'occupied_15_minute_business_slots',
            'readiness':'heuristic_data_readiness_not_prediction_accuracy','hours':'user_assumed','no_new_source_requests':True}}
    identity={k:v for k,v in report.items() if k not in {'generated_at','report_id'}}
    report['report_id']=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return report


def ai_context(report):
    """Explicit public aggregate allowlist. Never accepts personal research rows."""
    if report.get('policy')!=POLICY or report.get('incomplete'): raise ValueError('analysis_incomplete')
    rows=[]
    for row in report['stores']:
        if not row['observations']: continue
        rows.append({k:row[k] for k in ('store_id','city','observations','successful','empty','failed','missing',
            'coverage_percent','days_observed','comparable_intervals','readiness_score','peak_hours','sampling_gaps')})
        rows[-1]['hourly_summary']=[[h['hour'],h['observations'],h['active_ratio'],h['rate'],h['days']] for h in row['hourly']]
        rows[-1]['daily_summary']=[[d['date'],d['observations'],d['covered_slots'],d['expected_slots']] for d in row['daily'][-14:]]
    if not rows: raise ValueError('analysis_no_observations')
    # Larger historical fleets use the lowest coverage stores as explicit focus;
    # all stores remain available in the statistical dashboard.
    rows=sorted(rows,key=lambda r:(r['readiness_score'],r['store_id']))[:24]
    while len(rows)>1 and len(json.dumps(rows,ensure_ascii=False).encode())>8500:rows.pop()
    return {'policy':POLICY,'report_id':report['report_id'],'selection':report['selection'],'stores':rows,
        'represented_stores':len(rows),'total_stores_with_observations':sum(r['observations']>0 for r in report['stores']),
        'scope':'public_aggregates_only','prediction_accuracy_known':False,'actual_customer_counts_known':False}
