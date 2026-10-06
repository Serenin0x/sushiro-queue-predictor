"""Bounded private packet spool. No sending, acknowledgement or deletion."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

from .capture import CaptureError, _open_parent, _check_parent
from .credentials import _identity, _private_file
from .packets import PacketError, _write_packet, encoded
from .receipts import ReceiptError, read_packet

MAX_PENDING_PACKETS = 128
_NAME = re.compile(r'pending-([0-9a-f]{64})-([0-9a-f]{64})\.json\Z')


class PendingError(RuntimeError):
    def __init__(self, error_code, *, committed=False):
        super().__init__(error_code)
        self.error_code = error_code
        self.committed = committed


def _scope(packet):
    value = {key:packet[key] for key in ('packet_schema_version','store_ids','api_profile','data_origin')}
    return hashlib.sha256(encoded(value)).hexdigest()


def _names(directory_fd):
    names = []
    # scandir(fd) avoids following a replaced path; the whole dedicated spool is
    # bounded. Interrupted temporary files are preserved for explicit review.
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if _NAME.fullmatch(entry.name) is None:
                raise PendingError('pending_directory_requires_review')
            info = entry.stat(follow_symlinks=False)
            if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                raise PendingError('pending_packet_unsafe')
            names.append(entry.name)
            if len(names) > MAX_PENDING_PACKETS:
                raise PendingError('pending_capacity_exceeded')
    return sorted(names)


def _safe_result(**extra):
    return {**extra,'pending_confirmation':True,'server_received':False,
            'network_performed':False,'credentials_accessed':False,
            'source_claims_verified':False,'source_freshness':'unknown',
            'eta_available':False,'verified_training_labels':0}


def enqueue_packet(source, directory, *, as_of):
    """Persist one canonical packet; identical content keeps the existing file."""
    parent_fd = file_fd = None
    committed = False
    try:
        packet = read_packet(source,as_of=as_of)
        body = encoded(packet)
        if not packet['records']:
            raise PendingError('pending_empty_packet')
        digest,scope = hashlib.sha256(body).hexdigest(),_scope(packet)
        directory = Path(os.path.abspath(directory))
        destination = directory / f'pending-{scope}-{digest}.json'
        parent_fd,_ = _open_parent(destination,private=True)
        _names(parent_fd)

        def capacity(fd):
            if len(_names(fd)) >= MAX_PENDING_PACKETS:
                raise PendingError('pending_capacity_exceeded')

        try:
            result = _write_packet(body,destination,before_publish=capacity)
            committed = result['committed']
            duplicate = False
        except PacketError as error:
            if error.error_code != 'packet_output_exists':
                raise PendingError('pending_publish_unconfirmed',committed=error.committed) from None
            # Exact canonical contents, rather than a matching filename alone,
            # determine a duplicate. No existing file is ever overwritten.
            existing = read_packet(destination,as_of=as_of)
            if encoded(existing) != body:
                raise PendingError('pending_content_conflict')
            committed = True
            file_fd = os.open(destination.name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=parent_fd)
            initial = os.fstat(file_fd)
            if not _private_file(initial) or stat.S_IMODE(initial.st_mode) != 0o600:
                raise PendingError('pending_packet_unsafe',committed=True)
            os.fsync(file_fd)
            _check_parent(destination,parent_fd,private=True)
            final = os.stat(destination.name,dir_fd=parent_fd,follow_symlinks=False)
            if _identity(initial) != _identity(final):
                raise PendingError('pending_packet_changed',committed=True)
            os.fsync(parent_fd)
            result = {'committed':True,'durability_confirmed':True}
            duplicate = True
        _check_parent(destination,parent_fd,private=True)
        return _safe_result(**result,already_queued=duplicate,packet_sha256=digest,
                            scope_sha256=scope,record_count=len(packet['records']))
    except PendingError:
        raise
    except ReceiptError:
        raise PendingError('pending_invalid_packet',committed=committed) from None
    except (CaptureError,OSError,TypeError,ValueError):
        raise PendingError('pending_local_operation_failed',committed=committed) from None
    finally:
        for descriptor in (file_fd,parent_fd):
            if descriptor is not None:
                os.close(descriptor)


def pending_status(directory, *, as_of):
    """Validate at most 128 pending packets and return aggregate metadata."""
    parent_fd = None
    try:
        directory = Path(os.path.abspath(directory))
        probe = directory / 'pending-probe'
        parent_fd,_ = _open_parent(probe,private=True)
        import fcntl
        fcntl.flock(parent_fd,fcntl.LOCK_SH | fcntl.LOCK_NB)
        names = _names(parent_fd)
        records = 0
        scopes = set()
        for name in names:
            packet = read_packet(directory/name,as_of=as_of)
            scope,digest = _NAME.fullmatch(name).groups()
            if _scope(packet) != scope or hashlib.sha256(encoded(packet)).hexdigest() != digest or not packet['records']:
                raise PendingError('pending_content_conflict')
            scopes.add(scope);records += len(packet['records'])
        _check_parent(probe,parent_fd,private=True)
        if _names(parent_fd) != names:
            raise PendingError('pending_directory_changed')
        return _safe_result(pending_packets=len(names),pending_record_occurrences=records,
                            distinct_scopes=len(scopes),capacity=MAX_PENDING_PACKETS,
                            status_only=True)
    except PendingError:
        raise
    except ReceiptError:
        raise PendingError('pending_invalid_packet') from None
    except (CaptureError,OSError,TypeError,ValueError):
        raise PendingError('pending_local_operation_failed') from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
