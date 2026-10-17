"""Bounded, network-free incremental statistics publisher."""
from datetime import datetime,timezone
import fcntl,json,os,signal,sys,threading,time
from pathlib import Path
from sushiwait.credentials import _read_private_file
from sushiwait.officialview import read_config
from sushiwait.readmodel import RollupWriter,POLICY
from sushiwait.packets import encoded
from sushiwait.cadence import period_after,daily_policy


def publish(root,value):
    body=encoded(value);pending=root/'status.pending';target=root/'status.json'
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(body);handle.flush();os.fsync(handle.fileno())
    os.replace(pending,target)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def main():
    if len(sys.argv)!=2:return 2
    os.umask(0o077);config=Path(sys.argv[1]);root=config.parent
    if not config.is_absolute() or root.is_symlink() or root.stat().st_mode&0o777!=0o700:return 2
    settings=json.loads(_read_private_file(config,max_bytes=65536))
    if set(settings)!={'schema_version','view_files'} or settings['schema_version']!=1 or type(settings['view_files']) is not list or not 1<=len(settings['view_files'])<=64:return 2
    lock=os.open(root/'publisher.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stopped=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stopped.set())
    try:
        writer=RollupWriter(root/'statistics.sqlite',[read_config(p) for p in settings['view_files']])
        while not stopped.is_set():
            try:
                result=writer.step();now=datetime.now(timezone.utc)
                lag=(now-datetime.fromisoformat(result['oldest_pending_at'].replace('Z','+00:00'))).total_seconds() if result['oldest_pending_at'] else 0
                free=os.statvfs(root);free_bytes=free.f_bavail*free.f_frsize
                low=free_bytes<max(5*1024**3,free.f_blocks*free.f_frsize*.15)
                state='warming' if result['uninitialized_sources'] else 'needs_attention' if result['error_sources'] or lag>period_after(now,daily_policy(now))+120 or low else 'running'
                value={'at':now.isoformat(),'policy':POLICY,'state':state,**result,'storage_pressure':low,'disk_free_bytes':free_bytes,'data_available':result['snapshots']>0}
                publish(root,value)
                print(json.dumps({'event':'read_model_published','state':state,**{k:result[k] for k in ('accepted','pending_days','snapshots','seconds')}}),flush=True)
                stopped.wait(1 if (result['pending_sources'] or result['pending_days']) and not result['error_sources'] else 30)
            except Exception:
                publish(root,{'at':datetime.now(timezone.utc).isoformat(),'policy':POLICY,'state':'needs_attention','data_available':False,'error_code':'read_model_publisher_failed'})
                print('{"event":"read_model_publisher_failed","sensitive_details_omitted":true}',flush=True)
                stopped.wait(30)
        return 0
    finally:os.close(lock)


if __name__=='__main__':raise SystemExit(main())
