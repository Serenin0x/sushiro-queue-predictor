"""Response-time reconstruction of independent anonymous display streams."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import json
from uuid import UUID

from .calendar import date_features
from .client import _unique_json_object, _reject_json_constant
from .remote import SOURCE, QUEUE_NAMES, ENDPOINTS, MAX_RECORD, _id, validate_record
from .signals import SignalError, _time, _text, _stats, _add, _finish, _comparison


class _Stream:
    def __init__(self, endpoint, start, middle, at, gap):
        self.endpoint=endpoint;self.start=start;self.middle=middle;self.at=at;self.gap=gap
        self.previous=None;self.watermark=None;self.last_start=None;self.latest=None;self.kind=None
        self.counts=Counter();self.breaks=Counter();self.count_pairs=0;self.count_delta=0
        self.count_seconds=0.;self.cross=0
        self.queues={k:{'whole':_stats(),'previous':_stats(),'recent':_stats(),'cross':0} for k in QUEUE_NAMES}

    def invalidate(self):
        self.previous=None;self.kind='unknown';self.breaks['invalid_record']+=1

    def add(self, query, run, anchor):
        received=_time(query['received_at']) if query['attempted'] else anchor
        began=_time(query['started_at']) if query['attempted'] else None
        if received is None or query['attempted'] and began is None:
            self.invalidate();return
        if received<self.start:
            self.counts['older_responses_excluded']+=1;self.previous=None;return
        self.counts['within_window_rows']+=1
        if not query['ok']:
            reason='failed_response' if query['attempted'] else 'skipped_query'
            self.counts[reason+'s']+=1;self.breaks[reason]+=1
            self.previous=None;self.kind='interrupted';return
        if self.watermark is not None and (received<=self.watermark or began<self.last_start):
            self.counts['out_of_order_responses']+=1;self.breaks['non_increasing_time']+=1
            self.previous=None;self.kind='interrupted';return
        self.watermark=received;self.last_start=began;self.latest=received;self.kind='success'
        self.counts['valid_successful_responses']+=1
        if self.previous is not None:
            old,old_run,old_received=self.previous
            seconds=(received-old_received).total_seconds()
            reason=('run_changed' if old_run!=run else 'sampling_gap' if seconds>self.gap
                    else 'overlapping_requests' if began<old_received else None)
            if reason:self.breaks[reason]+=1
            elif self.endpoint=='groupqueues':
                for key,stats in self.queues.items():
                    before,after=old['queues'][key],query['payload']['queues'][key]
                    _add(stats['whole'],before,after,seconds)
                    if received<=self.middle:_add(stats['previous'],before,after,seconds)
                    elif old_received>=self.middle:_add(stats['recent'],before,after,seconds)
                    else:stats['cross']+=1
            else:
                self.count_pairs+=1;self.count_seconds+=seconds
                self.count_delta+=query['payload']['raw_count']-old['raw_count']
        self.previous=query['payload'],run,received

    def finish(self, seconds, incomplete):
        age=(self.at-self.latest).total_seconds() if self.latest is not None else None
        availability=('unknown' if self.kind=='unknown' else 'interrupted' if self.kind=='interrupted'
            else 'no_responses' if age is None else 'stale_responses' if age>self.gap else 'recent_responses')
        if incomplete:availability='history_scan_incomplete'
        value={'availability':availability,'last_success_response_age_seconds':age,
            'response_counts':dict(sorted(self.counts.items())),'chain_breaks':dict(sorted(self.breaks.items()))}
        if self.endpoint=='groupqueues':
            comparison_state='recent_observations' if availability=='recent_responses' else availability
            value['queues']={key:{'whole':_finish(stats['whole'],seconds),
                'previous_half':_finish(stats['previous'],seconds/2),
                'recent_half':_finish(stats['recent'],seconds/2),'cross_midpoint_pairs':stats['cross'],
                'rate_comparison':_comparison(stats['previous'],stats['recent'],comparison_state)}
                for key,stats in self.queues.items()}
        else:
            value['reported_count']={'field':'storequeuecount','unit':'unknown','comparable_pairs':self.count_pairs,
                'observed_seconds':round(self.count_seconds,6),
                'observed_fraction_of_window':round(self.count_seconds/seconds,6),
                'sum_of_pair_deltas':self.count_delta if self.count_pairs else None}
        return value


def remote_signal_report(store, store_id, *, as_of, window_seconds=120, max_gap_seconds=90, sample_limit=10000):
    """Read one private completed database; never contact endpoints or derive ETA."""
    try:_id(store_id)
    except ValueError:raise SignalError('invalid_remote_signal_store_id') from None
    if (type(window_seconds) is not int or not 30<=window_seconds<=3600
            or type(max_gap_seconds) is not int or not 1<=max_gap_seconds<=3600
            or type(sample_limit) is not int or not 1<=sample_limit<=10000):
        raise SignalError('invalid_remote_signal_bounds')
    at=_time(as_of)
    if at is None:raise SignalError('invalid_remote_signal_time')
    try:start=at-timedelta(seconds=window_seconds)
    except OverflowError:raise SignalError('invalid_remote_signal_time') from None
    if not store.read_only or store.db.in_transaction:raise SignalError('remote_signal_requires_idle_read_only_connection')
    store._guard();middle=start+timedelta(seconds=window_seconds/2)
    streams={e:_Stream(e,start,middle,at,max_gap_seconds) for e in ENDPOINTS}
    counts=Counter();store.db.execute('BEGIN')
    try:
        total=store.db.execute('SELECT COUNT(*) FROM remote_samples WHERE store_id=?',(store_id,)).fetchone()[0]
        rows=store.db.execute('SELECT CASE WHEN length(run_id)<=36 THEN run_id END,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json END '
            'FROM (SELECT id,run_id,ok,payload_json FROM remote_samples WHERE store_id=? ORDER BY id DESC LIMIT ?) ORDER BY id',
            (MAX_RECORD,store_id,sample_limit))
        for run,ok,encoded in rows:
            counts['scanned_rows']+=1
            try:
                if type(run) is not str or len(run)!=36 or str(UUID(run))!=run:raise ValueError
                record=validate_record(json.loads(encoded,object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant))
                if record['requested_store_id']!=store_id or type(ok) is not int or ok!=int(record['ok']):raise ValueError
                queries=record['queries']
                times=[_time(q['received_at']) for q in queries.values() if q['attempted']]
                if not times or any(t is None for t in times):raise ValueError
                # Admit the complete saved pair, including a count failure,
                # only after both response times. SQLite first-seen is unknown.
                completed=max(times)
            except (ValueError,TypeError,KeyError,OverflowError,RecursionError,UnicodeError):
                counts['invalid_records']+=1
                for stream in streams.values():stream.invalidate()
                continue
            if completed>at:
                counts['future_pair_rows_excluded']+=1;continue
            counts['admitted_pair_rows']+=1
            for endpoint,stream in streams.items():stream.add(queries[endpoint],run,completed)
        store._guard()
    finally:store.db.rollback()
    incomplete=total>sample_limit and counts['future_pair_rows_excluded']>0
    return {'schema_version':1,'feature_policy':'remote-display-window-v1','source':SOURCE,
        'requested_store_id':store_id,'as_of':_text(at),'window_started_at':_text(start),
        'window_seconds':window_seconds,'max_gap_seconds':max_gap_seconds,
        'endpoints':{e:s.finish(window_seconds,incomplete) for e,s in streams.items()},
        'scan':{'selection':'latest_store_ids_then_complete_pair_response_time_filter','sample_limit':sample_limit,
            'total_store_rows':total,'truncated':total>sample_limit,**dict(sorted(counts.items()))},
        'diagnostics_are_model_inputs':False,'historical_availability_verified':False,
        'time_selection_semantics':'local_response_completion_reconstruction_not_persistence_first_seen',
        'calendar_at_as_of':date_features(_text(at),as_of=_text(at)),
        'source_freshness':'unknown','response_store_identity_verified':False,'atomic_snapshot':False,
        'complete_queue_cursor_available':False,'eta_available':False,'true_no_show_rate':None,
        'verified_training_labels':0,'notification_sent':False,'network_performed':False,
        'limitations':['Displayed labels are opaque; turnover does not identify called or abandoned parties.',
            'Endpoint response clocks, failures and coverage are independent; neither is source-update time.',
            'Pair receipt is a lower bound for availability, not a verified persistence timestamp.',
            'A truncated latest-ID scan can omit historical rows; diagnostics cannot enter prediction inputs.',
            'Half-window ratios are descriptive and do not trigger anomaly alerts.']}
