"""Five-store, public-copy-only Flash worker. Key and budgets stay private."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import argparse,json,os,subprocess,time,urllib.request,tempfile
from sushiwait.modeladvice import POLICY,public_context,suggest_weights
from sushiwait.ticketresearch import TicketResearch

READ_ORIGIN='http://127.0.0.1:18080'
STORES=('3014','3004','2009','3015','2001')
DEADLINE=datetime(2026,10,14,14,tzinfo=timezone.utc)
OPEN=datetime(2026,10,14,3,tzinfo=timezone.utc)

def read(route):
 with urllib.request.urlopen(READ_ORIGIN+route,timeout=10) as r:
  raw=r.read(600001);assert len(raw)<=600000;return json.loads(raw)

def write(path,value):
 raw=json.dumps(value,separators=(',',':')).encode();assert len(raw)<=32768
 fd,name=tempfile.mkstemp(prefix='.model-writing-',dir=path.parent);temporary=Path(name)
 try:
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  temporary.chmod(0o600);os.replace(temporary,path)
 finally:
  if temporary.exists():temporary.unlink()
 fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)

def connection(root,mode_only=None):
 current=read('/api/v1/official/stores/3004/days/2026-10-13')
 past=read('/api/v1/official/stores/3004/days/2026-10-12')
 context=public_context(current,history=[past],connection_check=True)
 results={}
 for mode in (mode_only,) if mode_only else ('nonthinking','thinking'):
  budget_root=root/'daily' if (root/'daily').is_dir() else root
  try:result=suggest_weights(context,budget_file=budget_root/mode/'budget.json',key_file=root/'key/key.private.json')
  except Exception as error:result={'state':'failed','error_code':getattr(error,'error_code',type(error).__name__)}
  write(root/(mode+'-cloud-connection-'+str(time.time_ns())+'.json'),result)
  results[mode]={k:result.get(k) for k in ('state','attempts_used','latency_seconds','error_code','usage','estimated_cost_microunits')}
  results[mode]['advice_accepted']=result.get('advice') is not None
 print(json.dumps({'historical_connection_only':True,'results':results}),flush=True)

def loop(root,output,research_file=None):
 history={};cached={'schema_version':1,'policy':POLICY,'model':'deepseek-flash','status':'waiting_for_open','stores':{}}
 while datetime.now(timezone.utc)<DEADLINE:
  now=datetime.now(timezone.utc)
  if now<OPEN:
   write(output,cached);time.sleep(min(30,(OPEN-now).total_seconds()));continue
  try:
   active=subprocess.check_output(['systemctl','is-active','sushiwait-official-cloud-trial.service'],text=True,timeout=5).strip()=='active'
   if not active:raise ValueError('collector_not_active')
   cadence=min(600,max(180,int(read('/api/v1/statistics/settings')['poll_seconds'])))
   demand={}
   if research_file:
    research=TicketResearch(research_file)
    try:
     demand=research.demand(now=now)
     research.update_labels(lambda store,date:read(f'/api/v1/official/stores/{store}/days/{date}'),now=now)
    finally:research.close()
   for store in STORES:
    today=read(f'/api/v1/official/stores/{store}/days/2026-10-14')
    if store not in history:
     history[store]=[]
     for date in ('2026-10-12','2026-10-13'):
      try:history[store].append(read(f'/api/v1/official/stores/{store}/days/{date}'))
      except Exception:pass
    for queue in ('ordinary','reservation'):
     models=cached['stores'].setdefault(store,{}).setdefault(queue,{})
     try:context=public_context(today,queue=queue,history=history[store],cadence=cadence)
     except Exception as error:
      for mode in ('nonthinking','thinking'):models[mode]={'state':'waiting_for_observation','advice':None}
      continue
     for mode in ('nonthinking','thinking'):
      if datetime.now(timezone.utc)>=DEADLINE:break
      if mode not in demand.get(store,{}).get(queue,set()):
       models[mode]={'state':'waiting_for_viewer','advice':models.get(mode,{}).get('advice')};continue
      try:
       result=suggest_weights(context,budget_file=root/'daily'/mode/'budget.json',key_file=root/'key/key.private.json')
       models[mode]={'state':result['state'],'advice':result.get('advice')}
      except Exception as error:models[mode]={'state':getattr(error,'error_code',type(error).__name__),'advice':None}
   cached['status']='ready';cached['updated_at']=datetime.now(timezone.utc).isoformat()
  except Exception:cached['status']='waiting_for_observation'
  # Publishing a stale advice is harmless: browser checks sample identity and expiry.
  write(output,cached);time.sleep(30)
 cached['status']='finished';write(output,cached)

def main():
 global READ_ORIGIN
 p=argparse.ArgumentParser();p.add_argument('--read-origin',choices=['http://127.0.0.1:18080','http://127.0.0.1:18900'],default='http://127.0.0.1:18080');p.add_argument('--root',required=True);p.add_argument('--output');p.add_argument('--research-file');p.add_argument('--connection-check',action='store_true');p.add_argument('--mode',choices=['nonthinking','thinking']);a=p.parse_args();READ_ORIGIN=a.read_origin
 root=Path(a.root).resolve();assert root.is_dir() and root.stat().st_mode&0o777==0o700;os.umask(0o077)
 if a.connection_check:connection(root,a.mode)
 else:
  assert a.output and datetime.now(timezone.utc)<DEADLINE
  loop(root,Path(a.output).resolve(),Path(a.research_file).resolve() if a.research_file else None)
if __name__=='__main__':main()
