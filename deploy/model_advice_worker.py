"""Daily, viewer-driven Flash advice over saved public observations only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse, fcntl, hashlib, json, math, os, tempfile, time, urllib.request
from sushiwait.deepseek import _Ledger, DeepSeekError
from sushiwait.intake import _canonical

from sushiwait.calendar import date_features
from sushiwait.cadence import CHINA, period_after, daily_policy
from sushiwait.modeladvice import POLICY, MAX_PROJECTION_BYTES, public_context, public_projection, suggest_weights
from sushiwait.ticketresearch import TicketResearch
from sushiwait.cityscope import APPROVED_STORES as STORES

READ_ORIGIN = 'http://127.0.0.1:18900'


def read(route):
    with urllib.request.urlopen(READ_ORIGIN + route, timeout=10) as response:
        raw = response.read(600001)
        if len(raw) > 600000: raise ValueError('saved_projection_too_large')
        return json.loads(raw)


def write(path, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_PROJECTION_BYTES: raise ValueError('model_state_too_large')
    fd, name = tempfile.mkstemp(prefix='.model-writing-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        temporary.chmod(0o600); os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def business_window(now):
    local = now.astimezone(CHINA)
    features = date_features(local.isoformat(), as_of=local.isoformat())
    early = local.weekday() >= 5 or features['date_type'] == 'holiday'
    return (local.replace(hour=10 if early else 11, minute=30 if early else 0, second=0, microsecond=0),
            local.replace(hour=22, minute=0, second=0, microsecond=0))


def reserved_cost(path, now):
    """Read-only accounting of a retired ledger at its last valid instant."""
    deadline=datetime.fromisoformat(json.loads(path.read_text())['deadline_at'])
    ledger=_Ledger(path,min(now,deadline-timedelta(microseconds=1)))
    try:return sum(call['reserved_cost_microunits'] for call,_ in ledger.calls)
    finally:ledger.close()


def prepare_budget(root, mode, now):
    """Immutable per-day ledgers: 8 CNY thinking + 2 CNY nonthinking, total 10."""
    local = now.astimezone(CHINA)
    day_root = root / 'recurring' / local.date().isoformat()
    directory = day_root / (mode+'-v2')
    # Retire the 4096-token ledger without refunding its consumed or unknown calls.
    previous = day_root / mode / 'budget.json'
    charged = 0
    if previous.exists():
        charged = reserved_cost(previous,now)
    remaining = (8_000_000 if mode=='thinking' else 2_000_000)-charged
    if remaining <= 0: raise DeepSeekError('deepseek_budget_exhausted')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o777 != 0o700:
        raise ValueError('model_budget_directory_unsafe')
    value = {'schema_version':1, 'provider':'deepseek_official', 'model':'deepseek-flash',
        'thinking_mode':mode, 'accounting_policy':'actual_known_reserved_unknown',
        'created_at':local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat(),
        'deadline_at':business_window(now)[1].astimezone(timezone.utc).isoformat(),
        'max_calls':1000, 'max_reserved_tokens':30_000_000,
        'max_reserved_cost_microunits':remaining,
        'max_prompt_tokens':16384, 'max_output_tokens':16384 if mode=='thinking' else 2048,
        'input_rate_microunits_per_million':4_000_000, 'output_rate_microunits_per_million':16_000_000,
        'price_basis':'caller_supplied_ceiling_not_verified', 'timeout_seconds':90 if mode=='thinking' else 30}
    path = directory / 'budget.json'
    if path.exists():
        if json.loads(path.read_text()) != value: raise ValueError('existing_model_budget_changed')
    else:
        with path.open('x') as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        path.chmod(0o600)
    return path


def prepare_analysis_budget(root, mode, now):
    """Explicit analysis is available all day; retain every earlier reservation."""
    base=prepare_budget(root,mode,now)
    if now < business_window(now)[1]:return base
    directory=base.parent.parent/(mode+'-afterhours')
    original=json.loads(base.read_text())
    charged=reserved_cost(base,now)
    remaining=original['max_reserved_cost_microunits']-charged
    if remaining<=0:raise DeepSeekError('deepseek_budget_exhausted')
    local=now.astimezone(CHINA)
    value={**original,'max_reserved_cost_microunits':remaining,
           'deadline_at':(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()}
    directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode&0o777!=0o700:raise ValueError('model_budget_directory_unsafe')
    path=directory/'budget.json'
    if path.exists():
        if json.loads(path.read_text())!=value:raise ValueError('existing_model_budget_changed')
    else:write(path,value);path.chmod(0o600)
    return path


def retired_observation(root, mode, context, now):
    previous=root/'recurring'/now.astimezone(CHINA).date().isoformat()/mode/'budget.json'
    if not previous.exists(): return False
    identity={k:context[k] for k in ('policy','source','store_id','queue_type','local_date','observation_id','sample_at','phase')}
    key=hashlib.sha256(_canonical(identity).encode()).hexdigest()
    old=_Ledger(previous,now)
    try: return any(call['observation_key']==key for call,_ in old.calls)
    finally: old.close()


def tick(root, output, research_file, cache, history, *, now=None, reader=read, suggest=suggest_weights):
    now = now or datetime.now(timezone.utc)
    start, end = business_window(now)
    day = now.astimezone(CHINA).date().isoformat()
    if cache.get('day') != day:
        cache.clear(); cache.update(day=day, projection={'schema_version':1,'policy':POLICY,
            'model':'deepseek-flash','status':'waiting_for_open','stores':{}})
        history.clear()
    projection = cache['projection']
    projection['supported_store_ids'] = list(STORES)
    def publish():
        projection['updated_at'] = (datetime.now(timezone.utc) if now is None else now).isoformat()
        write(output, public_projection(projection))
    research = TicketResearch(research_file)
    try:
        demand = research.demand(now=now)
        # Label maintenance is independent of paid-model demand and budget.
        if cache.get('labels_at') is None or (now-cache['labels_at']).total_seconds() >= 60:
            research.update_labels(lambda store,date:reader(f'/api/v1/official/stores/{store}/days/{date}'), now=now)
            cache['labels_at'] = now
    finally:
        research.close()
    if not start <= now < end:
        projection['status']='waiting_for_open'; publish(); return
    health = reader('/api/v1/collection/health')
    if health.get('state')!='running' or health.get('heartbeat_stale') is not False:
        projection['status']='waiting_for_observation'; publish(); return
    projection['status']='ready'
    for store in STORES:
        for queue in ('ordinary','reservation'):
            models=projection['stores'].setdefault(store,{}).setdefault(queue,{})
            modes=demand.get(store,{}).get(queue,set())
            for mode in ('thinking','nonthinking'):
                if mode not in modes:
                    models[mode]={'state':'waiting_for_viewer','advice':models.get(mode,{}).get('advice')}
            if not modes: continue
            try: current=reader(f'/api/v1/official/stores/{store}/days/{day}')
            except Exception:
                for mode in modes: models[mode]={'state':'waiting_for_observation','advice':None}
                continue
            if store not in history:
                history[store]=[]
                for days in range(1,8):
                    past=(start-timedelta(days=days)).date().isoformat()
                    try: history[store].append(reader(f'/api/v1/official/stores/{store}/days/{past}'))
                    except Exception: pass
            latest = next((p.get('queue_received_at') for p in reversed(current.get('points',[])) if p.get('queue_received_at')), None)
            cadence=math.ceil(period_after(datetime.fromisoformat(latest.replace('Z','+00:00')),daily_policy(now))) if latest else 600
            try: context=public_context(current,queue=queue,history=history[store],cadence=cadence,now=now)
            except (ValueError,KeyError):
                for mode in modes: models[mode]={'state':'waiting_for_observation','advice':None}
                continue
            for mode in ('thinking','nonthinking'):
                if mode not in modes: continue
                try:
                    if retired_observation(root,mode,context,now):
                        models[mode]={'state':'previous_attempt_unknown','advice':None};continue
                    models[mode]={'state':'computing','advice':models.get(mode,{}).get('advice')}
                    publish()
                    result=suggest(context,budget_file=prepare_budget(root,mode,now),
                        key_file=root/'key/key.private.json',now=now)
                    models[mode]={'state':result['state'],'advice':result.get('advice')}
                    if result.get('network_performed'):
                        # Only accounting metadata in logs, never prompts, reasoning or keys.
                        print(json.dumps({k:result.get(k) for k in ('state','thinking_mode','latency_seconds','usage','estimated_cost_microunits','error_code')}),flush=True)
                except Exception as error:
                    code=getattr(error,'error_code','model_temporarily_unavailable')
                    models[mode]={'state':code,'advice':None}
                publish()
    publish()


def main():
    global READ_ORIGIN
    parser=argparse.ArgumentParser()
    parser.add_argument('--read-origin',choices=['http://127.0.0.1:18900'],default=READ_ORIGIN)
    parser.add_argument('--root',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--research-file',required=True)
    args=parser.parse_args(); READ_ORIGIN=args.read_origin; os.umask(0o077)
    root=Path(args.root).resolve(); output=Path(args.output).resolve()
    if not root.is_dir() or root.stat().st_mode & 0o777 != 0o700: raise ValueError('model_root_unsafe')
    lock=os.open(root/'recurring-worker.lock',os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    cache, history={},{}
    while True:
        try: tick(root,output,Path(args.research_file).resolve(),cache,history)
        except Exception:
            value=cache.get('projection',{'schema_version':1,'policy':POLICY,'model':'deepseek-flash','stores':{}})
            value.update(status='waiting_for_observation',updated_at=datetime.now(timezone.utc).isoformat());write(output,value)
        try:
            from sushiwait.analyticsai import tick as analytics_tick
            analytics_tick(root,prepare_analysis_budget,business_window,job_file=Path(args.research_file).parent/'analytics-job.json')
        except Exception:
            pass  # Analytics never stops collection labels or prediction advice.
        time.sleep(15)

if __name__=='__main__': main()
