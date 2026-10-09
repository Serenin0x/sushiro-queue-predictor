"""Prepare new daily units; never replace, stop or extend an old trial.

The operator must verify the old same-store writer is stopped by not-before.
Different root namespaces are not automatically discovered or locked together.
This helper creates no credentials, external requests or firewall changes.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex

from prepare_native import BASE
from sushiwait.businesshours import read_hours
from sushiwait.dailycontroller import daily_config
from sushiwait.monitorhub import validate_config
from sushiwait.remotetasks import _at, _now


def prepare(args, *, now=None):
    os.umask(0o077)
    release, root = Path(args.release_dir).resolve(), Path(args.root).resolve()
    clock = datetime.now(timezone.utc) if now is None else now
    activation = _at(args.not_before)
    if (activation < clock or not re.fullmatch('[a-z][a-z0-9-]{0,39}',args.prefix)
            or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',args.user)
            or not 1024 <= args.first_port <= 65519 or '%' in str(release) or '%' in str(root)):
        raise ValueError('daily_native_configuration_invalid')
    executable = release/'.venv/bin/sushiwait'
    if not executable.is_file(): raise ValueError('installed_executable_required')
    hours = read_hours(release/'config/default-business-hours.json')
    stores = [s for b in json.loads((release/'config/collection-stores.json').read_text())['batches'] for s in b]
    if len(stores)!=12 or len({s['store_id'] for s in stores})!=12:
        raise ValueError('expected_twelve_unique_stores')
    # Validate all configs before creating the new root. Never adopt an old one.
    for s in stores:
        daily_config(root/('store-'+s['store_id']),s['store_id'],hours,
            not_before=_now(activation),daily_pair_cap=1500)
    root.mkdir(mode=0o700)
    workers=[];units=[]
    for i,s in enumerate(stores):
        store=s['store_id'];state=root/('store-'+store);state.mkdir(mode=0o700)
        command=[str(executable),'remote-daily-serve','--root',str(state),'--store-id',store,
            '--business-hours-file',str(release/'config/default-business-hours.json'),
            '--not-before',_now(activation),'--daily-pair-cap','1500','--port',str(args.first_port+i)]
        unit=f'{args.prefix}-store{store}.service'
        content=BASE.replace('bounded collection trial','daily business-hours collection').format(
            user=args.user,release=shlex.quote(str(release)),
            write='ReadWritePaths='+shlex.quote(str(state))+'\n',command=shlex.join(command))
        (root/unit).write_text(content);units.append(unit)
        workers.append({'endpoint':f'http://127.0.0.1:{args.first_port+i}',
            'stores':{store:s['directory_name']}})
    hub=validate_config({'schema_version':2,'mode':'daily_controller_readonly','workers':workers})
    (root/'hub.json').write_text(json.dumps(hub,ensure_ascii=False))
    (root/'units.json').write_text(json.dumps({'units':units,'not_before':_now(activation),
        'release':str(release),'first_port':args.first_port,'daily_pair_cap_per_store':1500,
        'daily_maximum_total_http_attempts':36000,'interval_seconds':60,
        'same_store_old_writer_stop_verified':False,'services_started':False}))
    return {'prepared_store_units':12,'not_before':_now(activation),
        'same_store_old_writer_stop_verified':False,'services_started':False,'origin_requests':0,
        'public_listener_created':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--release-dir',required=True)
    parser.add_argument('--root',required=True)
    parser.add_argument('--not-before',required=True)
    parser.add_argument('--prefix',default='sushiwait-daily')
    parser.add_argument('--user',default='ubuntu')
    parser.add_argument('--first-port',type=int,default=18821)
    print(json.dumps(prepare(parser.parse_args())))
