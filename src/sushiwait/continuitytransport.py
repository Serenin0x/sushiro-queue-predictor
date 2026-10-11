"""Explicit SSH transport for public observations and reported relations.

This receiver requires a separately prepared continuity archive. The original
v1 receiver and its fixed command keep rejecting this new bundle format.
"""
from datetime import datetime, timezone
import hashlib
import json
import os

from .continuity import read_continuity_bundle, validate_continuity_bundle
from .continuityarchive import ContinuityArchive
from .continuityview import ContinuityStatisticsView
from .officialview import read_config
from .packets import encoded, _write_packet
from .receipts import MAX_PACKET_BYTES
from .ssharchive import SSHArchiveError, _sync_preserved_packet, read_stdin
from .storage import _unique_object

COMMAND = 'sushiwait-official-continuity-v1'


def _records(bundle):
    # Anchors are original public records too. Receipt order is canonical and
    # includes each identity once; no sender cursor becomes a server identity.
    values = {r['observation_id']: r for r in bundle['packet']['records']}
    values.update({a['record']['observation_id']: a['record'] for a in bundle['anchors']})
    return [values[key] for key in sorted(values)]


def receive_public_bundle(raw, config_file, *, original_command, now=None):
    if original_command != COMMAND:
        raise SSHArchiveError('ssh_archive_command_rejected')
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKET_BYTES:
        raise SSHArchiveError('ssh_archive_input_too_large')
    clock = datetime.now(timezone.utc) if now is None else now
    try:
        bundle = validate_continuity_bundle(json.loads(raw.decode('utf8'),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError())), as_of=clock.isoformat())
        view = read_config(config_file)
        packet = bundle['packet']
        if (not isinstance(view, ContinuityStatisticsView)
                or packet['api_profile'] != 'miniapp_gateway'
                or packet['data_origin'] != view.data_origin
                or packet['store_ids'] != sorted(view.names) or not packet['records']):
            raise SSHArchiveError('ssh_archive_scope_rejected')
        canonical = encoded(bundle)
        digest = hashlib.sha256(canonical).hexdigest()
        target = view.database.parent / 'bundles' / (digest + '.json')
        if target.exists() or target.is_symlink():
            if read_continuity_bundle(target, as_of=clock.isoformat()) != bundle:
                raise SSHArchiveError('ssh_archive_packet_conflict')
        elif _write_packet(canonical, target)['durability_confirmed'] is not True:
            raise SSHArchiveError('ssh_archive_packet_durability_unconfirmed')
        _sync_preserved_packet(target, canonical)
        with ContinuityArchive(view.database) as archive:
            result = archive.append_bundle(bundle, as_of=clock.isoformat())
        if (result['durability_confirmed'] is not True
                or result['archive_commit_status'] != 'committed'):
            raise SSHArchiveError('ssh_archive_commit_unconfirmed')
        return {'receipt_schema_version': 2, 'receiver_context': 'ssh_forced_command',
            'bundle_sha256': digest, 'packet_sha256': hashlib.sha256(encoded(packet)).hexdigest(),
            'durability_confirmed': True, 'archive_commit_status': 'committed',
            'inserted_records': result['inserted_records'], 'duplicate_records': result['duplicate_records'],
            'inserted_links': result['inserted_links'], 'duplicate_links': result['duplicate_links'],
            'observations': [{k:r[k] for k in ('observation_id','record_sha256')} for r in _records(bundle)],
            'links': [{k:r[k] for k in ('observation_id','link_sha256')} for r in bundle['links']],
            'query_credentials_accessed': False, 'source_claims_verified': False}
    except SSHArchiveError:
        raise
    except Exception:
        raise SSHArchiveError('ssh_archive_receive_failed') from None


def validate_bundle_receipt(receipt, bundle):
    keys = {'receipt_schema_version','receiver_context','bundle_sha256','packet_sha256',
        'durability_confirmed','archive_commit_status','inserted_records','duplicate_records',
        'inserted_links','duplicate_links','observations','links','query_credentials_accessed',
        'source_claims_verified'}
    records, links = _records(bundle), bundle['links']
    if (type(receipt) is not dict or set(receipt) != keys
            or type(receipt['receipt_schema_version']) is not int or receipt['receipt_schema_version'] != 2
            or receipt['receiver_context'] != 'ssh_forced_command'
            or receipt['bundle_sha256'] != hashlib.sha256(encoded(bundle)).hexdigest()
            or receipt['packet_sha256'] != hashlib.sha256(encoded(bundle['packet'])).hexdigest()
            or receipt['durability_confirmed'] is not True or receipt['archive_commit_status'] != 'committed'
            or any(type(receipt[k]) is not int or not 0 <= receipt[k] <= n for k,n in (
                ('inserted_records',len(records)),('duplicate_records',len(records)),
                ('inserted_links',len(links)),('duplicate_links',len(links))))
            or receipt['inserted_records'] + receipt['duplicate_records'] != len(records)
            or receipt['inserted_links'] + receipt['duplicate_links'] != len(links)
            or receipt['observations'] != [{k:r[k] for k in ('observation_id','record_sha256')} for r in records]
            or receipt['links'] != [{k:r[k] for k in ('observation_id','link_sha256')} for r in links]
            or receipt['query_credentials_accessed'] is not False or receipt['source_claims_verified'] is not False):
        raise SSHArchiveError('ssh_archive_receipt_mismatch')
    return receipt


def main():
    import argparse
    from .ssharchive import COMMAND as PLAIN_COMMAND, receive_public_packet
    parser = argparse.ArgumentParser()
    parser.add_argument('--view-file', required=True)
    args = parser.parse_args()
    try:
        # Fail before reading stdin unless this is the operator's explicit new
        # mode. Plain packets may coexist, with unknown continuity preserved.
        if not isinstance(read_config(args.view_file), ContinuityStatisticsView):
            raise SSHArchiveError('ssh_archive_scope_rejected')
        command = os.environ.get('SSH_ORIGINAL_COMMAND')
        if command not in {COMMAND, PLAIN_COMMAND}:
            raise SSHArchiveError('ssh_archive_command_rejected')
        operation = receive_public_bundle if command == COMMAND else receive_public_packet
        receipt = operation(read_stdin(0), args.view_file, original_command=command)
        print(encoded(receipt).decode('utf8'), flush=True)
        return 0
    except Exception:
        print('{"error_code":"ssh_archive_rejected"}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
