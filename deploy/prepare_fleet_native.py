"""Prepare one shared-process fleet and fixed read-only hub/gateway units.

No service starts, source calls, access credentials or firewall changes. The
operator must stop old same-store writers before the declared activation.
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
from sushiwait.dailyfleet import DailyFleetService, read_catalog
from sushiwait.monitorhub import validate_config
from sushiwait.remotetasks import _at, _now
from cutover_fleet import validate as validate_cutover


def prepare(args, *, now=None):
    os.umask(0o077)
    release,root=Path(args.release_dir).resolve(),Path(args.root).resolve()
    clock=datetime.now(timezone.utc) if now is None else now
    activation=_at(args.not_before)
    if (activation<=clock or not re.fullmatch(r'sushiwait-[a-z0-9-]{1,32}',args.prefix)
            or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',args.user)
            or not 1024<=args.port<=65535 or not 1024<=args.hub_port<=65535
            or not 1024<=args.public_port<=65535 or len({args.port,args.hub_port,args.public_port})!=3
            or '%' in str(release) or '%' in str(root)):
        raise ValueError('fleet_native_configuration_invalid')
    executable=release/'.venv/bin/sushiwait';python=release/'.venv/bin/python'
    if not executable.is_file() or not python.is_file():raise ValueError('installed_executable_required')
    catalog=read_catalog(release/'config/mainland-store-catalog.json')
    hours=read_hours(release/'config/default-business-hours.json')
    legacy_ids=getattr(args,'legacy_store_id',[])
    fleet=DailyFleetService(root=root,catalog=catalog,business_hours=hours,not_before=_now(activation),
        legacy_exports_root=args.legacy_exports_root,legacy_through_date=args.legacy_through_date,
        legacy_store_ids=legacy_ids,requests_per_second=args.requests_per_second)
    hub=validate_config({'schema_version':3,'mode':'daily_fleet_readonly',
        'workers':[{'endpoint':f'http://127.0.0.1:{args.port}','stores':fleet.names}]})
    old_root=getattr(args,'old_root',None)
    cutover_at=getattr(args,'cutover_at',None)
    auxiliaries=getattr(args,'old_auxiliary_unit',[])
    cutover=None
    if bool(old_root)!=bool(cutover_at) or auxiliaries and not old_root:
        raise ValueError('cutover_requires_old_root_and_time')
    names=[args.prefix+'-'+suffix+'.service' for suffix in ('collector','hub','statistics')]
    if old_root:
        old=json.loads((Path(old_root)/'units.json').read_text())
        if (old.get('store_ids')!=legacy_ids or old.get('user')!=args.user
                or len(old.get('units',[]))!=len(legacy_ids) or _at(cutover_at)<=clock):
            raise ValueError('cutover_old_scope_or_time_invalid')
        cutover=validate_cutover({'schema_version':1,'release':str(release),'root':str(root),
            'user':args.user,'not_before':_now(activation),'cutover_at':_now(_at(cutover_at)),
            'old_units':old['units']+auxiliaries,'old_collector_units':old['units'],
            'new_units':names,'port':args.port,'public_port':args.public_port,
            'legacy_exports_root':args.legacy_exports_root,
            'legacy_through_date':args.legacy_through_date,'legacy_store_ids':legacy_ids})
        from sushiwait.businesshours import BusinessHours
        if any(BusinessHours(hours).decision(s,_at(cutover_at))['is_open_window'] for s in fleet.names):
            raise ValueError('cutover_requires_closed_hours')
    root.mkdir(mode=0o700)
    for store in fleet.names:(root/('store-'+store)).mkdir(mode=0o700)
    (root/'hub.json').write_text(json.dumps(hub,ensure_ascii=False))
    command=[str(executable),'remote-daily-fleet-serve','--root',str(root),
        '--catalog-file',str(release/'config/mainland-store-catalog.json'),
        '--business-hours-file',str(release/'config/default-business-hours.json'),
        '--not-before',_now(activation),'--requests-per-second',str(args.requests_per_second),
        '--daily-pair-cap','1500','--port',str(args.port)]
    if args.legacy_exports_root:
        command += ['--legacy-exports-root',args.legacy_exports_root,'--legacy-through-date',args.legacy_through_date]
        for store in legacy_ids:command += ['--legacy-store-id',store]
    units=[]
    def save(suffix,cmd,write='',fleet_worker=False):
        unit=args.prefix+'-'+suffix+'.service'
        content=BASE.replace('bounded collection trial','daily fleet '+suffix).format(user=args.user,
            release=shlex.quote(str(release)),write=write,command=shlex.join(cmd))
        if fleet_worker:
            content=content.replace('MemoryMax=256M','MemoryMax=1792M').replace('TasksMax=64','TasksMax=512').replace('CPUQuota=100%','CPUQuota=200%')
        (root/unit).write_text(content);units.append(unit)
    save('collector',command,'ReadWritePaths='+shlex.quote(str(root))+'\n',True)
    save('hub',[str(executable),'collection-hub-serve','--config-file',str(root/'hub.json'),'--port',str(args.hub_port)])
    save('statistics',[str(python),'-I',str(release/'deploy/statistics_gateway.py'),
        '--config-file',str(root/'hub.json'),'--listen-host','0.0.0.0','--port',str(args.public_port)])
    meta={'schema_version':1,'units':units,'store_ids':list(fleet.names),'not_before':_now(activation),
        'release':str(release),'root':str(root),'user':args.user,'port':args.port,'hub_port':args.hub_port,
        'public_port':args.public_port,'maximum_request_starts_per_second':args.requests_per_second,
        'daily_pair_cap_per_store':1500,'daily_maximum_total_http_attempts':3000*len(fleet.names),
        'interval_seconds':60,'legacy_exports_root':args.legacy_exports_root,
        'legacy_through_date':args.legacy_through_date,'legacy_store_ids':legacy_ids,
        'directory_observed_at':catalog['directory_observed_at'],
        'current_mainland_completeness_verified':False,'same_store_old_writer_stop_verified':False,
        'services_started':False,'public_port_requires_existing_exposure_authorization':True}
    (root/'units.json').write_text(json.dumps(meta,ensure_ascii=False))
    if cutover:
        (root/'cutover.json').write_text(json.dumps(cutover))
        command=[str(python),'-I',str(release/'deploy/cutover_fleet.py'),
                 '--config-file',str(root/'cutover.json')]
        # Authorized operator unit needs systemctl and runuser. It is separate
        # from unprivileged, hardened collectors; never adds access credentials.
        (root/(args.prefix+'-cutover.service')).write_text(
            '[Unit]\nDescription=SUSHIWAIT verified closed-hours handover\n'
            'After=network-online.target\nWants=network-online.target\n\n'
            '[Service]\nType=oneshot\nUser=root\nUMask=0077\n'
            'Environment=PYTHONDONTWRITEBYTECODE=1\n'
            'Environment=PYTHONUNBUFFERED=1\nTimeoutStartSec=240\n'
            'ExecStart='+shlex.join(command)+'\n')
        calendar=_at(cutover_at).strftime('%Y-%m-%d %H:%M:%S UTC')
        (root/(args.prefix+'-cutover.timer')).write_text(
            '[Unit]\nDescription=SUSHIWAIT one closed-hours handover\n\n'
            '[Timer]\nOnCalendar='+calendar+'\nAccuracySec=1s\nPersistent=true\n'
            'Unit='+args.prefix+'-cutover.service\n\n[Install]\nWantedBy=timers.target\n')
    return {'prepared_stores':len(fleet.names),'prepared_services':3,'services_started':False,
        'maximum_request_starts_per_second':args.requests_per_second,'official_requests':0,
        'directory_currentness_verified':False,'new_firewall_rules':0,
        'cutover_timer_prepared':bool(cutover),'cutover_timer_started':False}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--release-dir',required=True);p.add_argument('--root',required=True)
    p.add_argument('--not-before',required=True);p.add_argument('--prefix',default='sushiwait-mainland')
    p.add_argument('--user',default='ubuntu');p.add_argument('--port',type=int,default=18821)
    p.add_argument('--hub-port',type=int,default=18820);p.add_argument('--public-port',type=int,default=18080)
    p.add_argument('--requests-per-second',type=float,default=5)
    p.add_argument('--legacy-exports-root');p.add_argument('--legacy-through-date')
    p.add_argument('--legacy-store-id',action='append',default=[])
    p.add_argument('--old-root');p.add_argument('--cutover-at')
    p.add_argument('--old-auxiliary-unit',action='append',default=[])
    print(json.dumps(prepare(p.parse_args())))
