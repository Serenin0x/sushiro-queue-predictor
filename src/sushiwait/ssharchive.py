"""Public packets over an operator-configured SSH forced command.

This module neither creates SSH access nor reads query credentials. The SSH
operator must independently configure and approve the key and pinned host.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import select
import time

from .capture import _open_parent, _check_parent
from .credentials import _identity, _private_file
from .officialview import read_config
from .packets import encoded, _write_packet
from .receipts import MAX_PACKET_BYTES, PacketArchive, read_packet, validate_packet
from .storage import _unique_object

COMMAND = 'sushiwait-official-packet-v1'


class SSHArchiveError(ValueError):
    pass


def _sync_preserved_packet(path, body):
    """Also sync a retry's existing packet before acknowledging durability."""
    parent = file_fd = None
    try:
        parent, name = _open_parent(path, private=True)
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        initial = os.fstat(file_fd)
        if not _private_file(initial) or initial.st_size != len(body):
            raise SSHArchiveError('ssh_archive_packet_conflict')
        chunks, remaining = [], len(body)+1
        while remaining:
            part = os.read(file_fd, remaining)
            if not part:
                break
            chunks.append(part); remaining -= len(part)
        _check_parent(path, parent, private=True)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (b''.join(chunks) != body or _identity(initial) != _identity(named)
                or _identity(initial) != _identity(os.fstat(file_fd))):
            raise SSHArchiveError('ssh_archive_packet_conflict')
        os.fsync(file_fd)
        os.fsync(parent)
    finally:
        for fd in (file_fd, parent):
            if fd is not None:
                os.close(fd)


def read_stdin(fd, *, timeout=15, maximum=MAX_PACKET_BYTES):
    """Read through EOF under a size and monotonic deadline, without a shell."""
    if (type(fd) is not int or fd < 0 or type(timeout) not in (int, float)
            or not 0 < timeout <= 15 or type(maximum) is not int
            or not 1 <= maximum <= MAX_PACKET_BYTES):
        raise SSHArchiveError('ssh_archive_invalid_bounds')
    deadline = time.monotonic() + timeout
    chunks, size = [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise SSHArchiveError('ssh_archive_input_deadline')
        part = os.read(fd, min(65536, maximum + 1 - size))
        if not part:
            if not size:
                raise SSHArchiveError('ssh_archive_empty_input')
            return b''.join(chunks)
        size += len(part)
        if size > maximum:
            raise SSHArchiveError('ssh_archive_input_too_large')
        chunks.append(part)


def receive_public_packet(raw, config_file, *, original_command, now=None):
    """Preserve the validated packet, durably append, then return an exact receipt.

    config_file is fixed by the local SSH forced command, never taken from the
    sender. Failed or uncertain commits do not emit a success receipt. A
    retransmitted packet keeps the same immutable file and observation IDs.
    """
    if original_command != COMMAND:
        raise SSHArchiveError('ssh_archive_command_rejected')
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKET_BYTES:
        raise SSHArchiveError('ssh_archive_input_too_large')
    clock = datetime.now(timezone.utc) if now is None else now
    try:
        def reject_constant(_):
            raise ValueError('nonfinite')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=reject_constant)
        packet = validate_packet(value, as_of=clock.isoformat())
        view = read_config(config_file)
        if (packet['api_profile'] != 'miniapp_gateway'
                or packet['data_origin'] != view.data_origin
                or packet['store_ids'] != sorted(view.names)
                or not packet['records']):
            raise SSHArchiveError('ssh_archive_scope_rejected')
        canonical = encoded(packet)
        digest = hashlib.sha256(canonical).hexdigest()
        # This fixed directory is prepared by the operator alongside the
        # archive. No sender-provided paths are accepted or created.
        target = view.database.parent / 'packets' / (digest + '.json')
        if target.exists() or target.is_symlink():
            if read_packet(target, as_of=clock.isoformat()) != packet:
                raise SSHArchiveError('ssh_archive_packet_conflict')
        else:
            written = _write_packet(canonical, target)
            if written['durability_confirmed'] is not True:
                raise SSHArchiveError('ssh_archive_packet_durability_unconfirmed')
        _sync_preserved_packet(target, canonical)
        archive_type = PacketArchive
        from .continuityview import ContinuityStatisticsView
        if isinstance(view, ContinuityStatisticsView):
            from .continuityarchive import ContinuityArchive
            archive_type = ContinuityArchive
        with archive_type(view.database) as archive:
            result = archive.append(packet, as_of=clock.isoformat())
        if (result['durability_confirmed'] is not True
                or result['local_archive_commit_status'] != 'committed'):
            raise SSHArchiveError('ssh_archive_commit_unconfirmed')
        return {'receipt_schema_version': 1, 'receiver_context': 'ssh_forced_command',
                'packet_sha256': digest, 'durability_confirmed': True,
                'archive_commit_status': 'committed',
                'inserted_records': result['inserted_records'],
                'duplicate_records': result['duplicate_records'],
                'observations': [{key: row[key] for key in ('observation_id', 'record_sha256')}
                                 for row in packet['records']],
                'query_credentials_accessed': False, 'source_claims_verified': False}
    except SSHArchiveError:
        raise
    except Exception:
        # Never echo account paths, input text, exceptions or configuration.
        raise SSHArchiveError('ssh_archive_receive_failed') from None


def validate_receipt(receipt, packet):
    """Check the SSH-authenticated reply against this exact packet, not a cursor."""
    keys = {'receipt_schema_version', 'receiver_context', 'packet_sha256',
            'durability_confirmed', 'archive_commit_status', 'inserted_records',
            'duplicate_records', 'observations', 'query_credentials_accessed',
            'source_claims_verified'}
    count = len(packet['records'])
    if (type(receipt) is not dict or set(receipt) != keys
            or type(receipt['receipt_schema_version']) is not int
            or receipt['receipt_schema_version'] != 1
            or receipt['receiver_context'] != 'ssh_forced_command'
            or receipt['packet_sha256'] != hashlib.sha256(encoded(packet)).hexdigest()
            or receipt['durability_confirmed'] is not True
            or receipt['archive_commit_status'] != 'committed'
            or any(type(receipt[key]) is not int or not 0 <= receipt[key] <= count
                   for key in ('inserted_records', 'duplicate_records'))
            or receipt['inserted_records'] + receipt['duplicate_records'] != count
            or receipt['observations'] != [{key: row[key] for key in ('observation_id', 'record_sha256')}
                                          for row in packet['records']]
            or receipt['query_credentials_accessed'] is not False
            or receipt['source_claims_verified'] is not False):
        raise SSHArchiveError('ssh_archive_receipt_mismatch')
    return receipt


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--view-file', required=True)
    args = parser.parse_args()
    try:
        command = os.environ.get('SSH_ORIGINAL_COMMAND')
        if command != COMMAND:
            raise SSHArchiveError('ssh_archive_command_rejected')
        receipt = receive_public_packet(read_stdin(0), args.view_file,
                                        original_command=command)
        print(encoded(receipt).decode('utf-8'), flush=True)
        return 0
    except Exception:
        print('{"error_code":"ssh_archive_rejected"}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
