"""Explicit private plan revisions; no account, query, notification or booking."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from uuid import UUID

from .capture import CaptureError, _open_parent, _check_parent
from .credentials import CredentialError, _read_private_file, _private_file, _identity
from .shared_monitoring import shared_polling_policy, _store

MAX_BYTES = 16 * 1024
MAX_REVISION = 2**31 - 1


class PlanUpdateError(ValueError):
    def __init__(self, code, *, committed=False):
        super().__init__(code)
        self.committed = committed


def stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise PlanUpdateError('plan_update_invalid')
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError
        return at.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PlanUpdateError('plan_update_invalid') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def validate_update(value, *, stores, base_interval, now=None):
    """A whole replacement plan set. declared_at is an operator declaration."""
    try:
        if (type(value) is not dict or set(value) != {'schema_version', 'series_id', 'revision', 'declared_at', 'document'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['revision']) is not int or not 1 <= value['revision'] <= MAX_REVISION
                or type(value['series_id']) is not str):
            raise ValueError
        identity = UUID(value['series_id'])
        if identity.version != 4 or str(identity) != value['series_id']:
            raise ValueError
        declared = stamp(value['declared_at'])
        if now is not None:
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError
            if declared > now:
                raise PlanUpdateError('plan_update_future_declaration')
        doc = value['document']
        if type(doc) is not dict or doc.get('last_poll_started_at') not in (None, {}):
            raise PlanUpdateError('plan_update_borrowed_poll_start')
        policy = shared_polling_policy(doc, as_of=declared.isoformat(), base_interval=base_interval)
        if (type(stores) is not list or not 1 <= len(stores) <= 3 or len(set(stores)) != len(stores)
                or any(type(s) is not str for s in stores)
                or any(s['store_id'] not in stores for s in policy['stores'])):
            raise PlanUpdateError('plan_update_store_scope')
        for store in stores:
            _store(store)
        if len(json.dumps(value, separators=(',', ':'), allow_nan=False).encode()) > MAX_BYTES:
            raise ValueError
        return deepcopy(value)
    except PlanUpdateError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise PlanUpdateError('plan_update_invalid') from None


def read_update(path, *, stores, base_interval, now=None):
    try:
        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError
                result[k] = v
            return result
        # A cooperating atomic rename can land between the reader's identity
        # checks. One extra local read handles that race; never retry HTTP,
        # malformed data, permissions, missing files, or revision conflicts.
        for attempt in range(2):
            try:
                body = _read_private_file(path)
                break
            except CredentialError as error:
                if attempt or error.error_code != 'credentials_file_changed':
                    raise
        value = json.loads(body, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate_update(value, stores=stores, base_interval=base_interval, now=now)
    except PlanUpdateError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise PlanUpdateError('plan_update_unavailable_or_unsafe') from None


def check_successor(old, new, *, old_digest=None):
    if old['series_id'] != new['series_id']:
        raise PlanUpdateError('plan_update_series_conflict')
    if new['revision'] < old['revision']:
        raise PlanUpdateError('plan_update_revision_rollback')
    if new['revision'] == old['revision']:
        if (old_digest or digest(old)) != digest(new):
            raise PlanUpdateError('plan_update_same_revision_conflict')
    elif stamp(new['declared_at']) < stamp(old['declared_at']):
        raise PlanUpdateError('plan_update_declaration_rollback')


def _named(parent_fd, name):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _private_file(info):
        raise PlanUpdateError('plan_update_output_unsafe')
    return _identity(info)


def publish_update(source, destination, *, stores, base_interval, clock=lambda: datetime.now(timezone.utc)):
    """Cooperating writers lock a separate 0700 directory, then atomically replace.

    A noncooperating process under the same OS user can race POSIX rename.
    Post-rename fsync failure is committed with unconfirmed durability.
    """
    if os.path.abspath(source) == os.path.abspath(destination):
        raise PlanUpdateError('plan_update_path_conflict')
    update = read_update(source, stores=stores, base_interval=base_interval, now=clock())
    body = json.dumps(update, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    parent_fd = fd = None
    temporary = None
    committed = False
    try:
        import fcntl
        parent_fd, name = _open_parent(destination, private=True)
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _named(parent_fd, name)
        if before is not None:
            old = read_update(destination, stores=stores, base_interval=base_interval, now=clock())
            if _named(parent_fd, name) != before:
                raise PlanUpdateError('plan_update_output_changed')
            check_successor(old, update)
            if update['revision'] == old['revision']:
                raise PlanUpdateError('plan_update_revision_not_new')
        os.fsync(parent_fd)
        temporary = '.sushiwait-plans-' + secrets.token_hex(16) + '.tmp'
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        initial = os.fstat(fd)
        if not _private_file(initial) or stat.S_IMODE(initial.st_mode) != 0o600:
            raise PlanUpdateError('plan_update_output_unsafe')
        offset = 0
        while offset < len(body):
            count = os.write(fd, body[offset:])
            if count <= 0:
                raise PlanUpdateError('plan_update_write_failed')
            offset += count
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        chunks, remaining = [], len(body) + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(fd)
        named = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        if (_identity(final) != _identity(named) or not _private_file(final)
                or (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino)
                or final.st_size != len(body) or b''.join(chunks) != body):
            raise PlanUpdateError('plan_update_output_changed')
        validate_update(update, stores=stores, base_interval=base_interval, now=clock())
        _check_parent(destination, parent_fd, private=True)
        if _named(parent_fd, name) != before:
            raise PlanUpdateError('plan_update_output_changed')
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        committed, temporary = True, None
        durability = True
        try:
            os.fsync(parent_fd)
        except OSError:
            durability = False
        return {'ok': durability, 'committed': True, 'durability_confirmed': durability,
                'revision': update['revision'], 'plan_count': len(update['document']['plans']),
                'network_performed': False, 'credentials_accessed': False,
                'business_operation_performed': False, 'eta_available': False}
    except PlanUpdateError:
        raise
    except (OSError, CaptureError, ImportError, ValueError, TypeError, OverflowError):
        raise PlanUpdateError('plan_update_output_uncertain' if committed else 'plan_update_write_failed',
                              committed=committed) from None
    finally:
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        for handle in (fd, parent_fd):
            if handle is not None:
                os.close(handle)
