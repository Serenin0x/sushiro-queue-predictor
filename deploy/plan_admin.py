"""Local administrator plan feed only; no source query or business operation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import uuid4

from sushiwait.capture import CaptureError, _check_parent, _open_parent
from sushiwait.credentials import _private_file
from sushiwait.planupdates import MAX_BYTES, MAX_REVISION, PlanUpdateError, publish_update, read_update, validate_update


class PlanAdminError(ValueError):
    pass


def decode_document(stream):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        body = stream.read(MAX_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > MAX_BYTES:
            raise ValueError
        return json.loads(body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError, OSError):
        raise PlanAdminError('plan_admin_invalid_document') from None


def summary(update, *, action):
    return {'ok': True, 'action': action, 'committed': False,
            'revision': update['revision'], 'plan_count': len(update['document']['plans']),
            'worker_application_verified': False, 'network_performed': False,
            'credentials_accessed': False, 'business_operation_performed': False,
            'eta_available': False}


def manage(action, destination, *, stores, base_interval=300, document=None,
           clock=lambda: datetime.now(timezone.utc)):
    """Generate file revisions, not authenticated user or worker acknowledgements."""
    if action not in ('init', 'publish', 'clear', 'status'):
        raise PlanAdminError('plan_admin_invalid_action')
    now = clock()
    try:
        os.lstat(destination)
        exists = True
    except FileNotFoundError:
        exists = False
    except OSError:
        raise PlanAdminError('plan_admin_file_unavailable') from None
    if exists:
        old = read_update(destination, stores=stores, base_interval=base_interval, now=now)
        if action in ('init', 'status'):
            return summary(old, action=action)
    elif action != 'init':
        raise PlanAdminError('plan_admin_feed_missing')
    else:
        old = None
    if action in ('init', 'clear'):
        document = {'schema_version': 1, 'plans': []}
    if old is not None and old['revision'] == MAX_REVISION:
        raise PlanAdminError('plan_admin_revision_exhausted')
    update = validate_update({'schema_version': 1, 'series_id': old['series_id'] if old else str(uuid4()),
        'revision': old['revision'] + 1 if old else 1, 'declared_at': now.isoformat(),
        'document': document}, stores=stores, base_interval=base_interval, now=now)
    body = json.dumps(update, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    parent_fd = fd = None
    temporary = None
    initial = None
    cleanup = True
    try:
        parent_fd, _ = _open_parent(destination, private=True)
        temporary = '.sushiwait-admin-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        initial = os.fstat(fd)
        if not _private_file(initial):
            raise PlanAdminError('plan_admin_file_unsafe')
        offset = 0
        while offset < len(body):
            written = os.write(fd, body[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(fd)
        _check_parent(destination, parent_fd, private=True)
        result = publish_update(str(Path(destination).parent / temporary), destination,
                                stores=stores, base_interval=base_interval, clock=clock)
    except (OSError, CaptureError):
        raise PlanAdminError('plan_admin_file_unsafe') from None
    finally:
        if temporary is not None and parent_fd is not None:
            try:
                current = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
                if initial is not None and (current.st_dev, current.st_ino) == (initial.st_dev, initial.st_ino):
                    os.unlink(temporary, dir_fd=parent_fd)
                else:
                    cleanup = False
            except OSError:
                cleanup = False
        for handle in (fd, parent_fd):
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    cleanup = False
    return {**result, 'action': action, 'worker_application_verified': False,
            'temporary_cleanup_confirmed': cleanup}


def main(argv=None, *, stdin=None, clock=lambda: datetime.now(timezone.utc)):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'publish', 'clear', 'status'))
    parser.add_argument('--output', default='/plan-updates/current.json')
    parser.add_argument('--store-id', action='append')
    parser.add_argument('--base-interval', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        document = decode_document(stdin or sys.stdin.buffer) if args.action == 'publish' else None
        result = manage(args.action, args.output, stores=args.store_id or ['3014', '3004', '2009'],
                        base_interval=args.base_interval, document=document, clock=clock)
        print(json.dumps(result))
        return 0 if result['ok'] else 1
    except (PlanAdminError, PlanUpdateError) as error:
        print(json.dumps({'ok': False, 'error_code': str(error),
                          'committed': bool(getattr(error, 'committed', False)),
                          'worker_application_verified': False, 'network_performed': False,
                          'credentials_accessed': False, 'business_operation_performed': False,
                          'eta_available': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
