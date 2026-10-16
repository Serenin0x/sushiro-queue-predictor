"""Durable per-store scheduling for the explicitly approved five-city fleet.

Only 502/503/504 may be isolated; 429 additionally pauses all requests. Every attempt is reserved before transport;
ambiguous outcomes, authentication failures and other errors remain global stops.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from sushiwait import cli
from sushiwait.cadence import next_sample_at
from sushiwait.credentials import _read_private_file
from sushiwait.packets import encoded
from sushiwait.storage import SnapshotStore

UTC = timezone.utc
POLICY = 'store-isolation-v1'


def instant(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.utcoffset() is None:
        raise ValueError('fleet_clock_invalid')
    return at


def save(path, value):
    body = encoded(value)
    if len(body) > 16384:
        raise ValueError('fleet_state_too_large')
    pending = path.with_suffix('.pending')
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(body); handle.flush(); os.fsync(handle.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def is_transient(row):
    payload = json.loads(row[4])
    return (row[3] == 0 and payload.get('failure_phase') == 'request'
            and payload.get('error_code') == 'http_error'
            and type(payload.get('http_status')) is int
            and payload['http_status'] in (429, 502, 503, 504))


class Fleet:
    def __init__(self, day, plan, old_usage, now):
        self.day, self.plan = Path(day), plan
        self.path = self.day / 'fleet-state.json'
        self.db = SnapshotStore(self.day / 'observations.sqlite')
        digest = hashlib.sha256(encoded(plan)).hexdigest()
        if self.path.exists():
            self.value = json.loads(_read_private_file(self.path))
        else:
            rows = self.db.db.execute("SELECT id,store_id,received_at,ok,payload_json FROM samples "
                "WHERE data_origin='live' AND api_profile='miniapp_gateway' ORDER BY id").fetchall()
            if len(rows) != old_usage or any(r[1] not in plan['store_ids'] for r in rows):
                raise ValueError('fleet_inherited_usage_invalid')
            self.value = {'schema_version': 1, 'policy': POLICY, 'plan_sha256': digest,
                'issued': old_usage, 'successful': 0, 'failed': 0, 'pending': None,
                'last_progress_at': None, 'rate_limit_until': None, 'rate_limit_streak': 0,
                'stores': {s: [now.isoformat(), 0, 0] for s in plan['store_ids']}}
            for row in rows:
                self.finish(row, persist=False)
            save(self.path, self.value)
        v = self.value
        if (set(v) != {'schema_version','policy','plan_sha256','issued','successful','failed',
                       'pending','last_progress_at','stores','rate_limit_until','rate_limit_streak'}
                or v['schema_version'] != 1 or v['policy'] != POLICY or v['plan_sha256'] != digest
                or set(v['stores']) != set(plan['store_ids'])
                or any(type(v[k]) is not int or v[k] < 0 for k in ('issued','successful','failed','rate_limit_streak'))
                or not old_usage <= v['issued'] <= plan['ordinary_query_limit']
                or v['issued'] != v['successful'] + v['failed'] + int(v['pending'] is not None)
                or sum(item[1] for item in v['stores'].values()) != v['successful'] + v['failed']):
            raise ValueError('fleet_state_invalid')
        if v['rate_limit_until'] is not None: instant(v['rate_limit_until'])
        for item in v['stores'].values():
            if (type(item) is not list or len(item) != 3
                    or any(type(n) is not int or n < 0 for n in item[1:])):
                raise ValueError('fleet_state_invalid')
            instant(item[0])
        if v['pending'] is not None:
            self.reconcile()

    def close(self):
        self.db.db.close()

    def finish(self, row, persist=True):
        _, store, received, ok, _ = row
        if store not in self.plan['store_ids'] or ok not in (0,1):
            raise ValueError('fleet_record_invalid')
        at = instant(received)
        item = self.value['stores'][store]
        item[1] += 1
        self.value['last_progress_at'] = received
        self.value['pending'] = None
        if ok:
            self.value['successful'] += 1
            self.value['rate_limit_streak'] = 0
            item[0], item[2] = next_sample_at(at).isoformat(), 0
        else:
            self.value['failed'] += 1
            if not is_transient(row):
                if persist: save(self.path, self.value)
                raise ValueError('fleet_nontransient_failure')
            payload=json.loads(row[4])
            if payload.get('http_status')==429:
                self.value['rate_limit_streak'] += 1
                minutes=min(120,15*2**min(3,self.value['rate_limit_streak']-1))
                retry=(payload.get('transport') or {}).get('retry_after_seconds')
                seconds=max(minutes*60,retry if type(retry) is int and retry>=0 else 0)
                end=instant(self.plan['windows'][0]['end'])
                self.value['rate_limit_until']=(at+timedelta(seconds=min(seconds,max(0,(end-at).total_seconds())))).isoformat()
            item[2] += 1
            end = instant(self.plan['windows'][0]['end'])
            item[0] = (min(end, at + timedelta(minutes=30)) if item[2] < 3 else end).isoformat()
        if persist: save(self.path, self.value)

    def reconcile(self):
        pending = self.value['pending']
        if (type(pending) is not dict or set(pending) != {'store','after_id'}
                or pending['store'] not in self.plan['store_ids']
                or type(pending['after_id']) is not int or pending['after_id'] < 0):
            raise ValueError('fleet_pending_invalid')
        rows = self.db.db.execute("SELECT id,store_id,received_at,ok,payload_json FROM samples "
            "WHERE id>? AND data_origin='live' AND api_profile='miniapp_gateway' ORDER BY id LIMIT 2",
            (pending['after_id'],)).fetchall()
        if len(rows) != 1 or rows[0][1] != pending['store']:
            raise ValueError('fleet_outcome_unknown')
        self.finish(rows[0])

    def due(self, now):
        rounds = self.plan['ordinary_query_limit'] // len(self.plan['store_ids'])
        if (self.value['rate_limit_until'] and now<instant(self.value['rate_limit_until'])):
            return None
        if self.value['issued'] >= self.plan['ordinary_query_limit']:
            return None
        # Unvisited stores first, then earliest actual due time. No catch-up.
        candidates = [s for s, item in self.value['stores'].items()
                      if item[1] < rounds and instant(item[0]) <= now]
        return min(candidates, key=lambda s: (self.value['stores'][s][1] != 0,
                   instant(self.value['stores'][s][0]), self.plan['store_ids'].index(s))) if candidates else None

    def reserve(self, store):
        if self.value['pending'] is not None or self.value['issued'] >= self.plan['ordinary_query_limit']:
            raise ValueError('fleet_reservation_invalid')
        after = self.db.db.execute('SELECT coalesce(max(id),0) FROM samples').fetchone()[0]
        self.value['issued'] += 1
        self.value['pending'] = {'store': store, 'after_id': after}
        save(self.path, self.value)

    def status(self):
        return {'successful_slots': self.value['successful'], 'failed_slots': self.value['failed'],
            'last_progress_at': self.value['last_progress_at'], 'isolation_policy': POLICY,
            'rate_limit_until': self.value['rate_limit_until'],
            'unavailable_store_ids': [s for s, item in self.value['stores'].items() if item[2] > 0]}


def run(service, root, day, plan, ledger, stopped):
    start, end, _ = service.campaign.validate_plan(plan)[0]
    usage, _ = service.reauthorization.task_usage(day, plan, include_fleet=False)
    fleet = Fleet(day, plan, usage, datetime.now(UTC))
    source = service.campaign.legacy.prepare_material_source(bundle_file=day/'initialization-material.json',
        expected_app_id=plan['app_id'], expected_source_version=plan['source_version'],
        expected_package_sha256=plan['package_sha256'])
    controller = service.campaign.legacy.Controller(day, plan, source, ledger)
    try:
        while not stopped.is_set():
            now = datetime.now(UTC)
            if now >= end - timedelta(seconds=16):
                ledger.finish(0)
                return
            if now < start:
                stopped.wait(1); continue
            limited=bool(fleet.value['rate_limit_until'] and now<instant(fleet.value['rate_limit_until']))
            service.publish_status(root, 'collection', {'state':'needs_attention' if limited else 'running', 'error_code':'cloud_rate_limited' if limited else None,
                'day':start.astimezone(service.CHINA).date().isoformat(),
                'opened_at':start.isoformat(), 'closes_at':end.isoformat(),
                'cadence':plan['cadence'], 'store_ids':plan['store_ids'], **fleet.status()})
            if limited:
                stopped.wait(1); continue
            controller.ensure_refresh(start, end, now=now)
            store = fleet.due(now)
            if store is None:
                stopped.wait(1); continue
            context, digest = controller.context()
            ledger.check_context(context.revision, digest)
            if cli._authorization_stop_code(cli.describe_authorization(context.authorization, now=now)):
                raise ValueError('fleet_auth_timing_stop')
            client = cli.client_for(SimpleNamespace(api_profile='miniapp_gateway'), credentials=context)
            fleet.reserve(store)
            cli.observe(client, store, fleet.db, api_profile='miniapp_gateway')
            fleet.reconcile()
            stopped.wait(2.1)
    finally:
        fleet.close()
