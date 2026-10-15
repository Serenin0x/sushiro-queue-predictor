"""Read-only source projection for a separately approved finite campaign.

Only saved public observations and the public campaign manifest are read.
No supplier, query context, initialization material or network API is used.
The old three-day sync entry point and its deadline remain unchanged.
"""
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

def _sibling(name):
    spec = importlib.util.spec_from_file_location('campaign_sync_' + name,
                                                 Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

campaign = _sibling('official_cloud_campaign')
sync = _sibling('official_cloud_public_sync')
legacy = campaign.legacy


def read_plan(root):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('cloud_public_paths_invalid')
    plan = legacy._decode(legacy._read_private_file(root / 'campaign-plan.json'))
    campaign.validate_plan(plan)
    if root.name != plan['plan_id'] or os.path.lexists(root / 'trial-plan.json'):
        raise ValueError('cloud_campaign_root_invalid')
    return plan


class Projection:
    def __init__(self, root, archive, plan, *, namespace='public-outbox'):
        self.lock = None
        self.root, self.archive = Path(root), Path(archive)
        windows = campaign.validate_plan(plan)
        if (not self.root.is_absolute() or not self.archive.is_absolute()
                or self.root.is_symlink() or self.root.name != plan['plan_id']
                or os.path.lexists(self.root / 'trial-plan.json')):
            raise ValueError('cloud_public_paths_invalid')
        self.source = self.root / 'observations.sqlite'
        if self.source.resolve() == self.archive.resolve():
            raise ValueError('cloud_public_scope_invalid')
        if type(namespace) is not str or not re.fullmatch('public-outbox(?:-[a-z]+)?', namespace):
            raise ValueError('cloud_public_paths_invalid')
        self.outbox = self.root / namespace
        self.scope = {'store_ids': plan['store_ids'][:], 'api_profile': 'miniapp_gateway', 'data_origin': 'live'}
        self.deadline = windows[-1][1] + timedelta(seconds=120)
        # Preflight stops also create public saved records without any GET.
        # This finite LOCAL delivery ceiling is separate from the query budget.
        # Six-digit names keep lexical order beyond999. Hitting the ceiling
        # stops projection and preserves the source; it never deletes records.
        self.limit = 5000
        self.pin = hashlib.sha256(sync.encoded({'plan': plan, 'source': str(self.source.resolve()),
            'archive': str(self.archive.resolve()), 'scope': self.scope,
            'deadline': self.deadline.isoformat()})).hexdigest()
        self.outbox.mkdir(mode=0o700, exist_ok=True)
        try:
            self.lock = os.open(self.outbox / 'sync.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            checkpoints = sorted(self.outbox.glob('confirmed-*.json'))
            if (len(checkpoints) > self.limit
                    or any(p.name != f'confirmed-{i:06}.json' for i, p in enumerate(checkpoints))):
                raise ValueError('cloud_public_checkpoint_invalid')
            self.after, self.index = 0, len(checkpoints)
            if checkpoints:
                previous = legacy._decode(legacy._read_private_file(checkpoints[-1]))
                keys = {'after_id', 'bundle_sha256', 'durability_confirmed', 'inserted_records',
                        'inserted_links', 'source_queries_added', 'query_credentials_accessed', 'scope_sha256'}
                if (type(previous) is not dict or set(previous) != keys
                        or previous['scope_sha256'] != self.pin
                        or previous['durability_confirmed'] is not True
                        or type(previous['after_id']) is not int or previous['after_id'] < 1
                        or type(previous['bundle_sha256']) is not str
                        or not re.fullmatch('[0-9a-f]{64}', previous['bundle_sha256'])
                        or any(type(previous[k]) is not int or not 0 <= previous[k] <= 150
                               for k in ('inserted_records', 'inserted_links'))
                        or type(previous['source_queries_added']) is not int or previous['source_queries_added'] != 0
                        or previous['query_credentials_accessed'] is not False):
                    raise ValueError('cloud_public_checkpoint_changed')
                self.after = previous['after_id']
        except Exception:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def step(self, at):
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError('cloud_clock_invalid')
        if at >= self.deadline or not self.source.exists():
            return None
        if self.index >= self.limit:
            raise ValueError('cloud_public_checkpoint_limit')
        result = sync.publish_page(self.source, self.archive, self.outbox, self.after,
                                   as_of=at.astimezone(timezone.utc).isoformat(), scope=self.scope,
                                   pending_limit=self.limit)
        if result is None:
            return None
        if type(result['after_id']) is not int or result['after_id'] <= self.after:
            raise ValueError('cloud_public_cursor_invalid')
        committed = sync._write_packet(sync.encoded({**result, 'scope_sha256': self.pin}),
                                       self.outbox / f'confirmed-{self.index:06}.json')
        if committed['durability_confirmed'] is not True:
            raise ValueError('cloud_public_checkpoint_unconfirmed')
        self.after, self.index = result['after_id'], self.index + 1
        return result


def main():
    if len(sys.argv) != 3:
        print('{"ok":false,"error_code":"cloud_public_paths_required"}')
        return 2
    os.umask(0o077)
    try:
        root, archive = map(Path, sys.argv[1:])
        plan = read_plan(root)
        with Projection(root, archive, plan) as projection:
            while datetime.now(timezone.utc) < projection.deadline:
                result = projection.step(datetime.now(timezone.utc))
                if result is not None:
                    print(json.dumps({'event': 'cloud_public_saved',
                        **{k: result[k] for k in ('after_id', 'inserted_records', 'inserted_links',
                        'durability_confirmed', 'source_queries_added', 'query_credentials_accessed')}}), flush=True)
                time.sleep(min(10, max(0, (projection.deadline - datetime.now(timezone.utc)).total_seconds())))
        print('{"event":"cloud_public_finite_sync_finished"}', flush=True)
        return 0
    except Exception:
        print('{"ok":false,"error_code":"cloud_public_sync_stopped","cursor_not_advanced":true,"automatic_retry":false}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
