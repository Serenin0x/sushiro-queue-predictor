"""Finite server-local public projection; no supplier or query access.

Uses the existing three-table archive consumed by the fixed statistics page.
Pending bundles remain immutable. A restart can reapply the same public bundle
idempotently; the cursor advances only after exact durable archive confirmation.
"""
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from sushiwait.continuity import read_continuity_bundle,select_continuity_packet
from sushiwait.continuityarchive import ContinuityArchive
from sushiwait.credentials import _read_private_file
from sushiwait.packets import _write_packet,encoded

DEADLINE=datetime.fromisoformat('2026-10-14T14:02:00+00:00')
SCOPE={'store_ids':['3014','3004','2009'],'api_profile':'miniapp_gateway','data_origin':'live'}


def publish_page(source,archive,outbox,after_id, *, as_of,scope,pending_limit=500):
    """One local select/commit/confirm attempt, including crash replay."""
    source,archive,outbox=map(Path,(source,archive,outbox))
    if source.resolve()==archive.resolve() or type(after_id) is not int or after_id<0:
        raise ValueError('cloud_public_scope_invalid')
    files=list(outbox.glob('pending-*.json'))
    if type(pending_limit) is not int or not 1<=pending_limit<=5000:
        raise ValueError('cloud_public_outbox_limit_invalid')
    if len(files)>pending_limit or any(re.fullmatch(r'pending-[0-9]{12}\.json',p.name) is None for p in files):
        raise ValueError('cloud_public_outbox_invalid')
    pending=[p for p in files if int(p.stem[8:])>after_id]
    if len(pending)>1:
        raise ValueError('cloud_public_delivery_ambiguous')
    if pending:
        path=pending[0];cursor=int(path.stem[8:])
        bundle=read_continuity_bundle(path,as_of=as_of)
        expected={**scope,'store_ids':sorted(scope['store_ids'])}
        if any(bundle['packet'][key]!=expected[key] for key in ('store_ids','api_profile','data_origin')):
            raise ValueError('cloud_public_pending_scope_changed')
    else:
        bundle,page=select_continuity_packet(source,as_of=as_of,after_id=after_id,limit=150,**scope)
        if page['record_count']==0:
            return None
        cursor=page['next_after_id']
        path=outbox/f'pending-{cursor:012}.json'
        result=_write_packet(encoded(bundle),path)
        if result['durability_confirmed'] is not True:
            raise ValueError('cloud_public_pending_unconfirmed')
        bundle=read_continuity_bundle(path,as_of=as_of)
    digest=hashlib.sha256(encoded(bundle)).hexdigest()
    with ContinuityArchive(archive) as destination:
        result=destination.append_bundle(bundle,as_of=as_of)
    if result['bundle_sha256']!=digest or result['durability_confirmed'] is not True:
        raise ValueError('cloud_public_commit_unconfirmed')
    return {'after_id':cursor,'bundle_sha256':digest,'durability_confirmed':True,
        'inserted_records':result['inserted_records'],'inserted_links':result['inserted_links'],
        'source_queries_added':0,'query_credentials_accessed':False}


def main():
    if len(sys.argv)!=3:
        print('{"ok":false,"error_code":"cloud_public_paths_required"}');return 2
    root,archive=map(Path,sys.argv[1:]);lock=None
    os.umask(0o077)
    try:
        if not root.is_absolute() or not archive.is_absolute():
            raise ValueError('cloud_public_paths_invalid')
        source=root/'observations.sqlite'
        outbox=root/'public-outbox';outbox.mkdir(mode=0o700,exist_ok=True)
        lock=os.open(outbox/'sync.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pin=hashlib.sha256(encoded({'source':str(source),'archive':str(archive),
            'scope':SCOPE,'deadline':DEADLINE.isoformat()})).hexdigest()
        checkpoints=sorted(outbox.glob('confirmed-*.json'))
        if len(checkpoints)>500 or any(p.name!=f'confirmed-{i:03}.json' for i,p in enumerate(checkpoints)):
            raise ValueError('cloud_public_checkpoint_invalid')
        after=0;index=len(checkpoints)
        if checkpoints:
            previous=json.loads(_read_private_file(checkpoints[-1]))
            if previous['scope_sha256']!=pin or previous['durability_confirmed'] is not True:
                raise ValueError('cloud_public_checkpoint_changed')
            after=previous['after_id']
        while datetime.now(timezone.utc)<DEADLINE:
            if source.exists():
                now=datetime.now(timezone.utc).isoformat()
                result=publish_page(source,archive,outbox,after,as_of=now,scope=SCOPE)
                if result is not None:
                    if index>=500:
                        raise ValueError('cloud_public_checkpoint_limit')
                    committed=_write_packet(encoded({**result,'scope_sha256':pin}),outbox/f'confirmed-{index:03}.json')
                    if committed['durability_confirmed'] is not True:
                        raise ValueError('cloud_public_checkpoint_unconfirmed')
                    after=result['after_id'];index+=1
                    print(json.dumps({'event':'cloud_public_saved','after_id':after,
                        **{k:result[k] for k in ('inserted_records','inserted_links','durability_confirmed',
                            'source_queries_added','query_credentials_accessed')}}),flush=True)
            time.sleep(min(10,max(0,(DEADLINE-datetime.now(timezone.utc)).total_seconds())))
        print('{"event":"cloud_public_finite_sync_finished"}',flush=True);return 0
    except Exception:
        print('{"ok":false,"error_code":"cloud_public_sync_stopped","cursor_not_advanced":true,"automatic_retry":false}',flush=True)
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__=='__main__':
    raise SystemExit(main())
