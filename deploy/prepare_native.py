"""Generate private per-store systemd trial units; does not install/start them.

Use when verified Python wheels are available but the Docker registry is not.
No credentials, firewall changes or public listener are created.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import urllib.request


BASE = '''[Unit]
Description=SUSHIWAIT bounded collection trial
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={user}
Group={user}
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={release}
Restart=on-failure
RestartSec=10
TimeoutStopSec=45
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
MemoryMax=256M
CPUQuota=100%
TasksMax=64
{write}ExecStart={command}

[Install]
WantedBy=multi-user.target
'''


def prepare(args):
    os.umask(0o077)
    release=Path(args.release_dir).resolve();root=Path(args.root).resolve()
    if (not re.fullmatch('[a-z][a-z0-9-]{0,39}',args.prefix)
            or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',args.user)
            or not 1024 <= args.first_port <= 65519
            or '%' in str(release) or '%' in str(root)):
        raise ValueError('native_configuration_invalid')
    executable=release/'.venv/bin/sushiwait'
    if not executable.is_file():raise ValueError('installed_executable_required')
    stores=[s for b in json.loads((release/'config/collection-stores.json').read_text())['batches'] for s in b]
    if len(stores)!=12 or len({s['store_id'] for s in stores})!=12:
        raise ValueError('expected_twelve_unique_stores')
    root.mkdir(mode=0o700)
    units=[]
    for i,s in enumerate(stores):
        store=s['store_id'];state=root/('store-'+store);state.mkdir(mode=0o700)
        (state/'campaign').mkdir(mode=0o700)
        (state/'plans.json').write_text('{"schema_version":1,"plans":[]}\n')
        command=[str(executable),'remote-campaign-serve','--root',str(state/'campaign'),
            '--plan-file',str(state/'plans.json'),'--store-id',store,
            '--business-hours-file',str(release/'config/default-business-hours.json'),
            '--base-interval','60','--duration','172800','--window-duration','86400',
            '--max-pairs','1500','--transient-recovery-limit','3','--resume-if-present',
            '--listen-host','127.0.0.1','--port',str(args.first_port+i)]
        unit=f'{args.prefix}-store{store}.service'
        (root/unit).write_text(BASE.format(user=args.user,release=shlex.quote(str(release)),
            write='ReadWritePaths='+shlex.quote(str(state))+'\n',command=shlex.join(command)))
        units.append(unit)
    (root/'units.json').write_text(json.dumps({'units':units,'store_ids':[s['store_id'] for s in stores],
        'maximum_total_pairs':18000,'maximum_total_requests':36000,
        'first_port':args.first_port,'prefix':args.prefix,'user':args.user,'release':str(release)}))
    print(json.dumps({'prepared_store_units':12,'maximum_total_pairs':18000,'maximum_total_requests':36000,
        'interval_seconds':60,'transient_recoveries_per_store':3,'services_started':False}))


def hub(args):
    os.umask(0o077)
    root=Path(args.root).resolve();meta=json.loads((root/'units.json').read_text())
    release=Path(meta['release']);stores=[s for b in json.loads((release/'config/collection-stores.json').read_text())['batches'] for s in b]
    workers=[];deadlines=[]
    for i,s in enumerate(stores):
        endpoint=f'http://127.0.0.1:{meta["first_port"]+i}'
        # This reads the local collector, never the restaurant origin.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint+'/api/v1/status',timeout=3) as response:
            data=response.read(65537)
            if len(data)>65536:raise ValueError('local_status_too_large')
            v=json.loads(data)
        if not v['worker_alive'] or v['service_state']!='running' or v['store_ids']!=[s['store_id']]:
            raise ValueError('collector_not_ready')
        deadlines.append(v['task']['deadline_at'])
        workers.append({'endpoint':endpoint,'stores':{s['store_id']:s['directory_name']}})
    from sushiwait.monitorhub import validate_config
    config=validate_config({'schema_version':1,'deadline_at':max(deadlines),'workers':workers})
    (root/'hub.json').write_text(json.dumps(config,ensure_ascii=False))
    command=[str(release/'.venv/bin/sushiwait'),'collection-hub-serve','--config-file',str(root/'hub.json'),
             '--port',str(meta['first_port']-1)]
    (root/(meta['prefix']+'-hub.service')).write_text(BASE.format(user=meta['user'],
        release=shlex.quote(str(release)),write='',command=shlex.join(command)))
    output=root/'observer';output.mkdir(mode=0o700,exist_ok=True)
    command=[str(release/'.venv/bin/python'),'-I',str(release/'deploy/cloud_watch.py'),'--root',str(root)]
    (root/(meta['prefix']+'-watch.service')).write_text(BASE.format(user=meta['user'],
        release=shlex.quote(str(release)),write='ReadWritePaths='+shlex.quote(str(output))+'\n',command=shlex.join(command)))
    print(json.dumps({'verified_running_stores':12,'hub_prepared':True,'observer_prepared':True,'official_requests_added':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','hub'])
    parser.add_argument('--root',required=True);parser.add_argument('--release-dir')
    parser.add_argument('--user',default='ubuntu');parser.add_argument('--prefix',default='sushiwait-trial')
    parser.add_argument('--first-port',type=int,default=18801)
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args)
    else:hub(args)
