"""Locally assigned anonymous receipt times; no independent time attestation.

The clock is read inside the writer transaction, before commit. A receipt says
when this writer accepted a complete pair, not when it became durably available
to another process. Old observations deliberately have no inferred receipt.
"""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from .remote import _time


def _clock():
    return datetime.now(timezone.utc)


def _stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('remote_local_receipt_clock_invalid')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(record, run_id, received_at):
    return hashlib.sha256(_canonical({'record': record, 'run_id': run_id,
                                     'received_at': received_at}).encode()).hexdigest()


def _run(run_id):
    if type(run_id) is not str or len(run_id) != 36 or str(UUID(run_id)) != run_id:
        raise ValueError('remote_local_receipt_invalid')


def pair_completed(record):
    return max(_time(q['received_at']) for q in record['queries'].values() if q['attempted'])


def make_receipt(record, run_id, now):
    _run(run_id)
    stamp = _stamp(now)
    if _time(stamp) < pair_completed(record):
        raise ValueError('remote_local_receipt_clock_order')
    return {'schema_version': 1, 'received_at': stamp,
            'record_sha256': _digest(record, run_id, stamp)}


def read_receipt(stored, record, run_id):
    """Return None only for genuinely absent legacy metadata; reject damage."""
    if 'local_intake' not in stored:
        return None
    try:
        value = stored['local_intake']
        _run(run_id)
        if (type(value) is not dict or set(value) != {'schema_version', 'received_at', 'record_sha256'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['record_sha256']) is not str or len(value['record_sha256']) != 64):
            raise ValueError
        at = _time(value['received_at'])
        if (_stamp(at) != value['received_at'] or at < pair_completed(record)
                or _digest(record, run_id, value['received_at']) != value['record_sha256']):
            raise ValueError
        return at
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError('remote_local_receipt_invalid') from None
