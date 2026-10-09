"""One paced read-only pair per explicit catalog store, never ID discovery.

Normal business-hours guard applies before each GET and after rate waiting.
No credentials, retries, ticket creation or cancellation. Output is private
safe transport evidence; a 200 does not authenticate identity/freshness.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from sushiwait.businesshours import BusinessHours, read_hours
from sushiwait.dailyfleet import read_catalog
from sushiwait.remote import RemoteClient


def run(args):
    os.umask(0o077)
    catalog = read_catalog(args.catalog_file)
    selected = [r['store_id'] for r in catalog['stores'] if r['store_id'] not in args.exclude_store_id]
    if set(args.exclude_store_id) - {r['store_id'] for r in catalog['stores']}:
        raise ValueError('excluded_store_not_in_catalog')
    hours = BusinessHours(read_hours(args.business_hours_file))
    output = Path(args.output)
    if output.exists() or output.is_symlink(): raise ValueError('proof_must_be_new')
    rows, admissions = [], []
    def guard(store):
        if admissions:
            time.sleep(max(0, admissions[-1]+0.5-time.monotonic()))
        if not hours.decision(store, datetime.now(timezone.utc))['is_open_window']:
            return False
        admissions.append(time.monotonic())
        return True
    client = RemoteClient(request_guard=guard)
    halted = None
    for store in selected:
        record = client.snapshot(store)
        q = record['queries']
        row = {'store_id':store, 'ok':record['ok'], 'queries':{key:{k:v[k] for k in
            ('attempted','ok','http_status','error_code','started_at','received_at')} for key,v in q.items()}}
        rows.append(row)
        if any(v['http_status'] in (403,429) for v in q.values()):
            halted = 'upstream_denied_or_limited'; break
        if any(v['error_code']=='business_window_closed' for v in q.values()):
            halted = 'business_window_closed'; break
        if len(rows)%10 == 0:
            print(json.dumps({'checked':len(rows),'successful':sum(r['ok'] for r in rows)}),flush=True)
    value = {'schema_version':1, 'directory_observed_at':catalog['directory_observed_at'],
        'selected_store_count':len(selected), 'checked_store_count':len(rows),
        'successful_store_count':sum(r['ok'] for r in rows), 'halted':halted,
        'http_attempts':sum(v['attempted'] for r in rows for v in r['queries'].values()),
        'minimum_transport_admission_interval_seconds': min((b-a for a,b in zip(admissions,admissions[1:])), default=None),
        'rows':rows, 'response_store_identity_verified':False, 'source_freshness':'unknown',
        'current_mainland_completeness_verified':False, 'credentials_used':False}
    descriptor = os.open(output, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as stream:
        json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    print(json.dumps({k:v for k,v in value.items() if k!='rows'}),flush=True)
    return 0 if halted is None and value['successful_store_count']==len(selected) else 1


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--catalog-file',required=True)
    p.add_argument('--business-hours-file',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--exclude-store-id',action='append',default=[])
    raise SystemExit(run(p.parse_args()))
