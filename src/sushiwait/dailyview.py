"""Writer-owned daily projections; source observations remain in private SQLite.

Summary counters cover every committed row in this campaign. A bounded graph
contains at most 2048 points per store/day and the first three displayed labels
per source array. These are observations, never confirmed call/no-show events.
"""
from __future__ import annotations
from collections import deque
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import re
import threading
from zoneinfo import ZoneInfo

from .businesshours import BusinessHours
from .remote import MAX_RECORD, QUEUE_NAMES, SOURCE, validate_record, _time
from .remotetasks import _json, _now, RemoteTaskError

MAX_DAY_POINTS=2048
MAX_DAYS=16
MAX_BODY=2*1024*1024

class DailyView:
    def __init__(self, stores, *, hours, base_interval):
        self.stores=tuple(stores);self.hours=BusinessHours(hours);self.interval=base_interval
        self.lock=threading.RLock();self.days={};self.seen=set();self.prior={}
        self.current_database=None;self.current_run=None
        self.cohort=hashlib.sha256(json.dumps(list(stores),separators=(',',':')).encode()).hexdigest()

    def restore(self,database):
        # Only the owning writer, or its verified closed archive reader, calls
        # this function. HTTP handlers never touch the database.
        database._guard()
        for identifier,run,store,ok,body in database.db.execute(
            'SELECT id,run_id,store_id,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
            'THEN payload_json END FROM remote_samples ORDER BY id LIMIT 362881',(MAX_RECORD,)):
            safe=validate_record(_json(body))
            if safe['requested_store_id']!=store or int(safe['ok'])!=ok:
                raise RemoteTaskError('daily_view_record_conflict')
            self.publish(safe,key=(str(database.path),identifier),run=run)
        self.current_database=str(database.path);self.current_run=database.run_id

    def committed(self,event):
        self.publish(event['record'],key=(self.current_database,event['id']),run=self.current_run)

    def publish(self,record,*,key,run):
        safe=validate_record(record);store=safe['requested_store_id']
        if store not in self.stores:raise RemoteTaskError('daily_view_store_scope')
        group,count=safe['queries']['groupqueues'],safe['queries']['storequeuecount']
        stamp=group['started_at'];local=_time(stamp).astimezone(ZoneInfo('Asia/Shanghai'))
        day=local.date().isoformat()
        with self.lock:
            if key in self.seen:return
            if len(self.days)>=MAX_DAYS and day not in self.days:
                raise RemoteTaskError('daily_view_date_bound')
            self.seen.add(key)
            bucket=self.days.setdefault(day,{})
            if store not in bucket:
                spans,metadata=self.hours.intervals_for(store,local.date(),as_of=_time(stamp))
                bucket[store]={'points':deque(maxlen=MAX_DAY_POINTS),'covered':set(),
                    'spans':spans,'metadata':metadata,'totals':{
                    'observations':0,'successful_pairs':0,'failed_pairs':0,'scheduled_pause_slots':0,
                    'recorded_http_attempts':0,'group_successes':0,'long_gap_boundaries':0,
                    'run_boundaries':0,'first_observation_at':None,'last_observation_at':None,
                    'display_removed_labels':{name:0 for name in QUEUE_NAMES}}}
            b=bucket[store];t=b['totals'];t['observations']+=1
            paused=any(q['error_code']=='business_window_closed' for q in safe['queries'].values())
            t['scheduled_pause_slots' if paused else 'successful_pairs' if safe['ok'] else 'failed_pairs']+=1
            t['recorded_http_attempts']+=sum(q['attempted'] for q in safe['queries'].values())
            t['first_observation_at']=t['first_observation_at'] or stamp;t['last_observation_at']=stamp
            removed=None;elapsed=None;comparison='insufficient'
            previous=self.prior.get(store)
            if previous and previous[0]==day:
                t['run_boundaries']+=int(previous[1]!=run)
                prior=previous[2]['queries']['groupqueues']
                if group['ok'] and prior['ok']:
                    elapsed=(_time(group['received_at'])-_time(prior['received_at'])).total_seconds()
                    if elapsed>2*self.interval:
                        comparison='gap';t['long_gap_boundaries']+=1
                    elif previous[1]!=run:comparison='run_boundary'
                    elif elapsed<=0:comparison='time_order_or_duplicate'
                    else:
                        comparison='comparable_display_sets'
                        removed={q:len(set(prior['payload']['queues'][q])-set(group['payload']['queues'][q])) for q in QUEUE_NAMES}
                        for q,n in removed.items():t['display_removed_labels'][q]+=n
            self.prior[store]=(day,run,safe)
            if group['ok']:
                t['group_successes']+=1
                started=_time(stamp)
                for i,(start,end) in enumerate(b['spans']):
                    if start<=started<end:b['covered'].add((i,int((started-start).total_seconds()//self.interval)))
            point={'request_started_at':stamp,'queue_received_at':group['received_at'],
                'count_received_at':count['received_at'],'pair_ok':safe['ok'],'scheduled_pause':paused,
                'queues':{q:group['payload']['queues'][q][:3] for q in QUEUE_NAMES} if group['ok'] else None,
                'call_reference_labels':{q:(group['payload']['queues'][q][0] if group['payload']['queues'][q] else None)
                    for q in QUEUE_NAMES} if group['ok'] else None,
                'display_sizes':{q:len(group['payload']['queues'][q]) for q in QUEUE_NAMES} if group['ok'] else None,
                'count_raw':count['payload']['raw_count'] if count['ok'] else None,
                'comparison_state':comparison,'interval_seconds':elapsed,'removed_labels':removed,
                'error_codes':{q:r['error_code'] for q,r in safe['queries'].items() if r['error_code'] is not None}}
            b['points'].append(json.dumps(point,separators=(',',':'),ensure_ascii=False))

    def _summary(self,day,store,b,now):
        through=min(now,_time(day+'T16:00:00Z')) # local end of this date
        spans=b['spans'];planned=sum(math.ceil((end-start).total_seconds()/self.interval) for start,end in spans)
        elapsed=sum(math.ceil(max(0,(min(end,through)-start).total_seconds())/self.interval) for start,end in spans if through>start)
        return {**deepcopy(b['totals']),**b['metadata'],'store_id':store,'local_date':day,
            'declared_intervals':[[ _now(a),_now(z)] for a,z in spans],
            'expected_background_slots_full_day':planned,'expected_background_slots_so_far':elapsed,
            'observed_background_slots':len(b['covered']),
            'observed_slot_fraction_so_far':min(1,len(b['covered'])/elapsed) if elapsed else None,
            'slot_fraction_semantics':'successful_queue_response_in_declared_background_bin',
            'returned_graph_points':len(b['points']),'graph_truncated':len(b['points'])<b['totals']['observations'],
            'actual_called_count':None,'no_show_rate':None,'count_unit':'unknown',
            'full_source_arrays_persisted':True,'source_freshness':'unknown'}

    def index(self,store,*,now):
        if store not in self.stores:raise RemoteTaskError('daily_view_store_scope')
        with self.lock:
            days=[self._summary(day,store,bucket[store],now) for day,bucket in sorted(self.days.items()) if store in bucket]
        return {'daily_schema_version':1,'source':SOURCE,'requested_store_id':store,'timezone':'Asia/Shanghai',
            'generated_at':_now(now),'days':days,'cohort_id':self.cohort,'campaign_scope_store_ids':list(self.stores),
            'heatmap_semantics':'coverage_only_traffic_not_calibrated','network_performed_by_read':False,
            'eta_available':False,'verified_training_labels':0}

    def batch_index(self,*,now):
        with self.lock:
            days={day:{store:self._summary(day,store,b,now) for store,b in bucket.items()}
                for day,bucket in sorted(self.days.items())}
        return {'daily_schema_version':1,'source':SOURCE,'days':days,'store_names':{s:'门店 '+s for s in self.stores},
            'configured_store_ids':list(self.stores),'unavailable_store_ids':[],'cohort_id':self.cohort,
            'heatmap_semantics':'coverage_only_traffic_not_calibrated','actual_called_count':None,
            'network_performed_by_read':False,'eta_available':False}

    def detail(self,store,day,*,now):
        if (type(day) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',day)
                or date.fromisoformat(day).isoformat()!=day or store not in self.stores):
            raise RemoteTaskError('daily_view_invalid_date_or_scope')
        with self.lock:
            b=self.days.get(day,{}).get(store)
            summary=self._summary(day,store,b,now) if b else None
            encoded=list(b['points']) if b else []
        truncated=False
        while sum(len(s.encode())+1 for s in encoded)>MAX_BODY-16384:
            encoded.pop(0);truncated=True
        return {'daily_schema_version':1,'source':SOURCE,'requested_store_id':store,'local_date':day,
            'generated_at':_now(now),'summary':summary,'points':[json.loads(s) for s in encoded],
            'returned_graph_points':len(encoded),'graph_truncated':truncated or bool(summary and summary['graph_truncated']),
            'labels_per_array_in_graph':3,'full_source_arrays_persisted':True,
            'call_reference_semantics':'user_assumed_first_displayed_label',
            'first_label_is_confirmed_call':False,
            'display_turnover_is_no_show_rate':False,'actual_called_count':None,
            'network_performed_by_read':False,'eta_available':False}
