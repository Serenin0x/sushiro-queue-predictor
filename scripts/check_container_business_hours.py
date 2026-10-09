"""Run the actual installed CLI boundary check on Linux without networking."""
import argparse,json,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--docker',default='docker');args=p.parse_args()
fixture=Path(__file__).resolve().with_name('check_business_install.py')
result=subprocess.run([args.docker,'run','--rm','--network=none','--read-only','--cap-drop=ALL',
    '--security-opt=no-new-privileges','--pids-limit=64','--memory=256m',
    '--tmpfs','/tmp:rw,noexec,nosuid,size=32m,mode=1777','--mount',f'type=bind,source={fixture},target=/opt/sushiwait/check_business_install.py,readonly',
    '--entrypoint','python',args.image,'-I','/opt/sushiwait/check_business_install.py',
    '/opt/sushiwait/default-business-hours.json'],capture_output=True,text=True,timeout=45)
if result.returncode:raise SystemExit('Linux business check failed: '+result.stderr[-2000:])
proof=json.loads(result.stdout);assert proof['installed_business_hours_ok'] and proof['official_http_requests']==0
proof.update(actual_linux_container=True,non_root_uid=10001,read_only_root=True,network_disabled=True)
print(json.dumps(proof))
