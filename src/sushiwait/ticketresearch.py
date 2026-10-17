"""Private, owner-scoped research tickets and short public-viewing leases.

No official requests, model calls or verified-outcome admission occur here.
Server-derived display labels stay private; only cohorts of at least five
completed samples across two dates can produce an anonymous wait summary.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from statistics import median

from .displayarrival import POLICY as ARRIVAL_POLICY, label_ticket_arrival, ticket_timing
from .displayreplay import SHANGHAI, _points, _boundary, _time
from .cityscope import STORES

MODES = {'auto', 'nonthinking', 'thinking', 'algorithm'}
_CREATE = (
    'CREATE TABLE tickets(record_id TEXT PRIMARY KEY,owner_hash TEXT NOT NULL,reported TEXT NOT NULL,'
    'result TEXT,received_at TEXT NOT NULL,updated_at TEXT NOT NULL)',
    'CREATE TABLE viewers(client_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,queue TEXT NOT NULL,'
    'mode TEXT NOT NULL,expires_at TEXT NOT NULL)',
)
_ACTIVE = "json_extract(reported,'$.deleted_at') IS NULL"


def clock():
    return datetime.now(timezone.utc)


def owner_hash(secret):
    if type(secret) is not str or not re.fullmatch('[0-9a-f]{64}', secret):
        raise ValueError('invalid_research_owner')
    return hashlib.sha256(secret.encode()).hexdigest()


def reported_ticket(value, now):
    required = {'store_id', 'queue_type', 'number', 'issued_at', 'checked_in', 'entry_mode', 'prediction_reason'}
    optional={'input_method','image_sha256','record_kind','prediction_mode'}
    if type(value) is not dict or not required<=set(value) or set(value)-required-optional:
        raise ValueError('invalid_research_ticket')
    if value.get('record_kind','record') not in ('record','prediction') or value.get('prediction_mode','algorithm') not in MODES:
        raise ValueError('invalid_research_ticket')
    if value.get('input_method','manual') not in {'manual','screenshot_ocr','screenshot_reviewed'} or ('image_sha256' in value and (value.get('input_method') not in {'screenshot_ocr','screenshot_reviewed'} or type(value['image_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['image_sha256']))):
        raise ValueError('invalid_screenshot_evidence')
    issued = _time(value['issued_at'])
    if (type(value['store_id']) is not str or not re.fullmatch('[1-9][0-9]{0,9}',value['store_id'])
            or int(value['store_id'])>2**31-1 or value['queue_type'] not in ('ordinary', 'reservation')
            or type(value['number']) is not str or not re.fullmatch('[0-9]{1,7}', value['number'])
            or int(value['number']) < 1 or issued > now or issued.year < 2000
            or value['checked_in'] is not None and type(value['checked_in']) is not bool
            or value['entry_mode'] not in ('live', 'retrospective')
            or value['prediction_reason'] is not None and (type(value['prediction_reason']) is not str
                or not re.fullmatch('[a-z_]{1,80}', value['prediction_reason']))):
        raise ValueError('invalid_research_ticket')
    return {**value, 'issued_at': issued.isoformat(), 'local_date': issued.astimezone(SHANGHAI).date().isoformat(),
            'report_origin': 'user_reported_test', 'verified_actual_ticket': False}


class TicketResearch:
    def __init__(self, path):
        self.path = Path(path)
        parent = self.path.parent
        if (not self.path.is_absolute() or not parent.is_dir() or parent.is_symlink()
                or stat.S_IMODE(parent.stat().st_mode) != 0o700):
            raise ValueError('unsafe_research_directory')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 8*1024*1024:
                raise ValueError('unsafe_research_database')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=2)
        try:
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.execute('PRAGMA journal_mode=DELETE')
            if self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
                if self.db.execute("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]:
                    raise ValueError('unexpected_research_schema')
                for statement in _CREATE:
                    self.db.execute(statement)
                self.db.execute('PRAGMA user_version=1')
                self.db.commit()
            objects = self.db.execute("SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            if self.db.execute('PRAGMA user_version').fetchone()[0] != 1 or sorted(v[0] for v in objects) != sorted(_CREATE):
                raise ValueError('unexpected_research_schema')
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def submit(self, record_id, secret, value, *, now=None):
        now = now or clock()
        owner = owner_hash(secret)
        if type(record_id) is not str or not re.fullmatch('[0-9a-f]{32}', record_id):
            raise ValueError('invalid_research_identity')
        reported = reported_ticket(value, now)
        previous = self.db.execute('SELECT owner_hash,reported FROM tickets WHERE record_id=?', [record_id]).fetchone()
        if previous:
            before = json.loads(previous[1])
            if previous[0] != owner or any(before[k] != reported[k] for k in ('store_id','queue_type','number','issued_at')):
                raise ValueError('research_identity_conflict')
            return {'record_id': record_id, 'saved': not bool(before.get('deleted_at')),
                    'duplicate': True, **({'deleted': True} if before.get('deleted_at') else {})}
        if self.db.execute('SELECT count(*) FROM tickets').fetchone()[0] >= 5000:
            raise ValueError('research_database_full')
        with self.db:
            self.db.execute('INSERT INTO tickets VALUES(?,?,?,NULL,?,?)',
                            [record_id, owner, json.dumps(reported), now.isoformat(), now.isoformat()])
        return {'record_id': record_id, 'saved': True, 'duplicate': False}

    def set_deleted(self, record_id, secret, *, deleted=True, now=None):
        """Owner-only reversible removal; stale submissions cannot revive it."""
        now = now or clock()
        owner = owner_hash(secret)
        if type(record_id) is not str or not re.fullmatch('[0-9a-f]{32}', record_id) or type(deleted) is not bool:
            raise ValueError('invalid_research_identity')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT reported FROM tickets WHERE record_id=? AND owner_hash=?',
                                  [record_id, owner]).fetchone()
            if row is None:
                # Missing and another owner's records are indistinguishable.
                return {'record_id':record_id, 'deleted':deleted, 'found':False}
            reported = json.loads(row[0])
            if deleted:
                reported.setdefault('deleted_at', now.isoformat())
                self.db.execute('DELETE FROM viewers WHERE client_id=?', [record_id])
            else:
                reported.pop('deleted_at', None)
            self.db.execute('UPDATE tickets SET reported=?,updated_at=? WHERE record_id=? AND owner_hash=?',
                            [json.dumps(reported),now.isoformat(),record_id,owner])
        return {'record_id':record_id, 'deleted':deleted, 'found':True}

    def capture_forecast(self, record_id, secret, forecast, *, read_day=None, now=None):
        """Freeze the first pre-outcome estimate per actual model, owner-only."""
        now = now or clock()
        owner = owner_hash(secret)
        if type(record_id) is not str or not re.fullmatch('[0-9a-f]{32}',record_id):
            raise ValueError('invalid_research_identity')
        keys={'mode','requested_mode','computed_at','sample_at','reference_time','scenario_earliest','scenario_latest','policy'}
        if (type(forecast) is not dict or set(forecast)!=keys or forecast['mode'] not in ('algorithm','thinking','nonthinking')
                or forecast['requested_mode'] not in MODES or forecast['policy']!='display_arrival_history_live_v6'):
            raise ValueError('invalid_forecast_baseline')
        computed,sample,predicted,early=map(_time,(forecast[k] for k in ('computed_at','sample_at','reference_time','scenario_earliest')))
        late=_time(forecast['scenario_latest']) if forecast['scenario_latest'] is not None else None
        if not sample<=computed<=now or (computed-sample).total_seconds()>660 or not computed<=early<=predicted or predicted>computed+timedelta(hours=6) or late and predicted>late:
            raise ValueError('invalid_forecast_baseline')
        row=self.db.execute('SELECT reported FROM tickets WHERE record_id=? AND owner_hash=? AND '+_ACTIVE,[record_id,owner]).fetchone()
        if not row:return {'found':False,'accepted':False}
        reported=json.loads(row[0]);issued=_time(reported['issued_at'])
        if computed<issued or computed.astimezone(SHANGHAI).date()!=issued.astimezone(SHANGHAI).date():
            raise ValueError('invalid_forecast_baseline')
        if reported.get('record_kind','prediction' if reported['entry_mode']=='live' else 'record')!='prediction':
            return {'found':True,'accepted':False,'reason':'record_only'}
        if read_day is not None:self.update_labels(read_day,record_id=record_id,now=now)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT reported,result FROM tickets WHERE record_id=? AND owner_hash=? AND '+_ACTIVE,[record_id,owner]).fetchone()
            if not row:return {'found':False,'accepted':False}
            reported=json.loads(row[0]);label=json.loads(row[1]) if row[1] else {}
            baselines=reported.setdefault('first_forecasts',{})
            if forecast['mode'] in baselines:
                return {'found':True,'accepted':True,'duplicate':True,'baseline':baselines[forecast['mode']]}
            if now.astimezone(SHANGHAI).date()!=issued.astimezone(SHANGHAI).date() or now.astimezone(SHANGHAI).hour>=22:
                return {'found':True,'accepted':False,'reason':'prediction_closed'}
            if label.get('result_available'):
                return {'found':True,'accepted':False,'reason':'outcome_already_known'}
            if label.get('state')=='archive_unavailable':
                return {'found':True,'accepted':False,'reason':'archive_unavailable','retryable':True}
            baseline={**forecast,'received_at':now.isoformat(),'basis':'browser_reported_before_saved_outcome'}
            baselines[forecast['mode']]=baseline;reported['record_kind']='prediction';reported['prediction_mode']=forecast['requested_mode']
            self.db.execute('UPDATE tickets SET reported=?,updated_at=? WHERE record_id=? AND owner_hash=? AND '+_ACTIVE,
                            [json.dumps(reported),now.isoformat(),record_id,owner])
        return {'found':True,'accepted':True,'duplicate':False,'baseline':baseline}

    def own_records(self, secret):
        return self.query_records(secret)['records']

    def query_records(self, secret, *, page=1, page_size=100, filters=None):
        """Owner-bound database paging. Dates refer to issuance in Shanghai."""
        filters = {} if filters is None else filters
        if (type(page) is not int or not 1 <= page <= 5000 or type(page_size) is not int
                or not 1 <= page_size <= 500 or type(filters) is not dict
                or set(filters) - {'store_id', 'from', 'to'}):
            raise ValueError('invalid_research_page')
        clauses, values = ['owner_hash=?', _ACTIVE], [owner_hash(secret)]
        if filters.get('store_id'):
            if type(filters['store_id']) is not str or not re.fullmatch('[1-9][0-9]{0,9}', filters['store_id']):
                raise ValueError('invalid_research_filter')
            clauses.append("json_extract(reported,'$.store_id')=?"); values.append(filters['store_id'])
        for key, operator in (('from', '>='), ('to', '<=')):
            value = filters.get(key)
            if value:
                if type(value) is not str or not re.fullmatch('20[0-9]{2}-[0-9]{2}-[0-9]{2}', value):
                    raise ValueError('invalid_research_filter')
                datetime.strptime(value, '%Y-%m-%d')
                clauses.append("json_extract(reported,'$.local_date')" + operator + '?'); values.append(value)
        if filters.get('from') and filters.get('to') and filters['from'] > filters['to']:
            raise ValueError('invalid_research_filter')
        where = ' AND '.join(clauses)
        total = self.db.execute('SELECT count(*) FROM tickets WHERE '+where, values).fetchone()[0]
        rows = self.db.execute('SELECT record_id,reported,result,received_at FROM tickets WHERE '+where+
            ' ORDER BY received_at DESC,record_id DESC LIMIT ? OFFSET ?', values+[page_size,(page-1)*page_size]).fetchall()
        records=[];checked_at=clock()
        for identity,raw,result,received in rows:
            reported=json.loads(raw);label=json.loads(result) if result else None
            records.append({'record_id':identity,'reported':reported,'result':label,'received_at':received,
                            'timing':ticket_timing(label,reported,now=checked_at)})
        return {'records':records,
                'deleted_record_ids':[r[0] for r in self.db.execute(
                    'SELECT record_id FROM tickets WHERE owner_hash=? AND NOT ('+_ACTIVE+')', [owner_hash(secret)])],
                'total':total, 'page':page, 'page_size':page_size, 'has_more':page*page_size<total}

    def presence(self, value, *, now=None):
        now = now or clock()
        if (type(value) is not dict or set(value) != {'client_id','store_id','queue_type','mode','visible'}
                or type(value['client_id']) is not str or not re.fullmatch('[0-9a-f]{32}', value['client_id'])
                or value['store_id'] not in STORES or value['queue_type'] not in ('ordinary','reservation')
                or value['mode'] not in MODES or type(value['visible']) is not bool):
            raise ValueError('invalid_prediction_presence')
        with self.db:
            self.db.execute('DELETE FROM viewers WHERE expires_at<=?', [now.isoformat()])
            self.db.execute('DELETE FROM viewers WHERE client_id=?', [value['client_id']])
            deleted = self.db.execute('SELECT 1 FROM tickets WHERE record_id=? AND NOT ('+_ACTIVE+')',
                                      [value['client_id']]).fetchone()
            if value['visible'] and value['mode'] != 'algorithm' and not deleted:
                if self.db.execute('SELECT count(*) FROM viewers').fetchone()[0] >= 64:
                    raise ValueError('prediction_viewer_capacity')
                self.db.execute('INSERT INTO viewers VALUES(?,?,?,?,?)', [value['client_id'],value['store_id'],
                                value['queue_type'],value['mode'],(now+timedelta(seconds=90)).isoformat()])
        return {'accepted': True, 'lease_seconds': 90}

    def demand(self, *, now=None):
        now = now or clock()
        result = {}
        for store, queue, mode in self.db.execute('SELECT store_id,queue,mode FROM viewers WHERE expires_at>?', [now.isoformat()]):
            result.setdefault(store, {}).setdefault(queue, set()).add('thinking' if mode == 'auto' else mode)
        return result

    def update_labels(self, read_day, *, now=None, record_id=None):
        now = now or clock()
        cache, changed = {}, 0
        rows = (self.db.execute('SELECT record_id,reported,result FROM tickets WHERE record_id=? AND '+_ACTIVE,[record_id]).fetchall()
                if record_id is not None else self.db.execute('SELECT record_id,reported,result FROM tickets WHERE '+_ACTIVE+' LIMIT 5000').fetchall())
        for identity, raw, previous in rows:
            old = json.loads(previous) if previous else None
            if old and old.get('result_available') and old.get('policy') == ARRIVAL_POLICY:
                continue
            value = json.loads(raw)
            key = (value['store_id'], value['local_date'])
            try:
                if key not in cache:
                    cache[key] = read_day(*key)
                detail = cache[key]
                if not detail.get('points') and value['local_date'] < now.astimezone(SHANGHAI).date().isoformat():
                    raise ValueError('no_saved_historical_observation')
                label = label_ticket_arrival(detail, store_id=value['store_id'], number=value['number'],
                    issued_at=value['issued_at'], queue=value['queue_type'], checked_in=value['checked_in'])
                points = _points(detail, 'mixedQueue' if value['queue_type']=='ordinary' else 'reservationQueue')
                usable = [p for p in points if not p['reason'] and p['at'] <= _time(value['issued_at'])]
                rates = []
                for a,b in zip(usable,usable[1:]):
                    if b['at'] >= _time(value['issued_at'])-timedelta(minutes=30) and not _boundary(a,b,660):
                        rates.append((b['position']-a['position'])*60/(b['at']-a['at']).total_seconds())
                label['issue_rate_reference'] = median(rates) if rates else None
            except Exception:
                label = {'result_available':False,'state':'archive_unavailable','actual_call_verified':False}
            encoded = json.dumps(label, sort_keys=True)
            if encoded != previous:
                with self.db:
                    self.db.execute('UPDATE tickets SET result=?,updated_at=? WHERE record_id=?', [encoded,now.isoformat(),identity])
                changed += 1
        return changed

    def public_wait_summary(self, *, now=None):
        now = now or clock()
        from .calendar import date_features
        current = date_features(now.isoformat(), as_of=now.isoformat())
        groups, seen = {}, set()
        for raw, result in self.db.execute('SELECT reported,result FROM tickets WHERE result IS NOT NULL AND '+_ACTIVE):
            reported, label = json.loads(raw), json.loads(result)
            if not label.get('result_available') or 'number_reversal' in label.get('quality_flags', []):
                continue
            if ticket_timing(label,reported,now=now)['status'] not in ('observed','inferred'):
                continue
            identity = (reported['store_id'],reported['queue_type'],int(reported['number']),reported['issued_at'])
            if identity in seen:
                continue
            seen.add(identity)
            key = (reported['store_id'], reported['queue_type'])
            features = date_features(reported['issued_at'], as_of=now.isoformat())
            if current['date_type'] == 'unknown' or features['date_type'] != current['date_type']:
                continue
            groups.setdefault(key, []).append((reported['local_date'],label,features))
        stores = {}
        for (store,queue), rows in groups.items():
            # Prefer a matching weekday, then matching season, with date type
            # always preserved. Coarse fallback is disclosed in the summary.
            for predicate, cohort in ((lambda f:f['iso_weekday']==current['iso_weekday'] and f['month']==current['month'],'weekday_month'),
                                      (lambda f:f['calendar_season']==current['calendar_season'],'date_type_season')):
                selected=[r for r in rows if predicate(r[2])]
                if len(selected)>=5 and len({r[0] for r in selected})>=2:
                    rows=selected;break
            else:cohort='date_type'
            if len(rows)<5 or len({day for day,_,_ in rows})<2:
                continue
            low = sorted(v['wait_minutes']['lower'] for _,v,_ in rows)
            high = sorted(v['wait_minutes']['upper'] for _,v,_ in rows)
            rates = [v['issue_rate_reference'] for _,v,_ in rows if v.get('issue_rate_reference',0) and v['issue_rate_reference']>0]
            stores.setdefault(store,{})[queue] = {'sample_count':len(rows),'distinct_days':len({d for d,_,_ in rows}),
                'median_lower':median(low),'median_upper':median(high),'early_lower':low[int((len(low)-1)*.1)],
                'late_upper':high[int((len(high)-1)*.9)],'issue_rate_median':median(rates) if rates else None,
                'date_type':current['date_type'],'cohort':cohort,
                'actual_call_verified':False,'calibrated_interval':False,'sample_basis':'user_reported_display_arrival'}
        return {'schema_version':1,'generated_at':now.isoformat(),'stores':stores}
