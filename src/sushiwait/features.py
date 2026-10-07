"""Private outcome/queue research rows with explicit historical availability.

An accepted review is a human declaration, not an authenticated training label.
Queue labels remain opaque; source identity, freshness and count units remain
unverified. Complete bounded scans avoid latest-ID selection leaking the future.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from uuid import UUID

from .baseline import MAX_WAIT_US, _us
from .calendar import date_features
from .client import _unique_json_object, _reject_json_constant
from .credentials import _read_private_file
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet
from .remote import SOURCE, ENDPOINTS, QUEUE_NAMES, MAX_RECORD, validate_record
from .remoteintake import read_receipt, pair_completed
from .remotesignals import _Stream
from .reviews import ReviewError

POLICY = 'reviewed_outcomes_with_received_queue_features_v1'
MAX_HISTORY_BYTES = 32 * 1024 * 1024
_FIELDS = {'schema_version', 'as_of', 'data_origin', 'api_profile', 'store_id',
           'queue_data_origin', 'elapsed_seconds', 'max_cases',
           'window_seconds', 'max_gap_seconds'}
_CODES = frozenset({'features_invalid_plan', 'features_invalid_input',
    'features_source_invalid', 'features_remote_invalid', 'features_history_limit',
    'features_case_limit', 'features_output_exists', 'features_operation_failed'})


class FeatureError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if type(code) is str and code in _CODES else 'features_operation_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def validate_feature_plan(value, *, now=None):
    try:
        if (type(value) is not dict or set(value) != _FIELDS
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['data_origin'] not in ('synthetic', 'self_reported')
                or value['queue_data_origin'] != ('synthetic' if value['data_origin'] == 'synthetic' else 'live')
                or value['api_profile'] not in ('legacy', 'miniapp_gateway')
                or type(value['store_id']) is not str
                or re.fullmatch('[1-9][0-9]{0,9}', value['store_id']) is None
                or int(value['store_id']) > 2**31-1
                or type(value['max_cases']) is not int or not 1 <= value['max_cases'] <= 100
                or type(value['window_seconds']) is not int or not 30 <= value['window_seconds'] <= 3600
                or type(value['max_gap_seconds']) is not int or not 1 <= value['max_gap_seconds'] <= 3600):
            raise ValueError
        cutoff = _time(value['as_of'])
        if cutoff > (_clock() if now is None else now):
            raise ValueError
        elapsed = value['elapsed_seconds']
        if (type(elapsed) is not list or not 1 <= len(elapsed) <= 16
                or any(type(e) is not int or not 0 <= e <= MAX_WAIT_US//1_000_000 for e in elapsed)
                or any(b <= a for a, b in zip(elapsed, elapsed[1:]))):
            raise ValueError
        return {**value, 'as_of': _utc(cutoff)}
    except (ValueError, TypeError, KeyError, OverflowError):
        raise FeatureError('features_invalid_plan') from None


def read_feature_plan(path):
    try:
        body = _read_private_file(path)
        return validate_feature_plan(_json(body))
    except Exception:
        raise FeatureError('features_invalid_input') from None


def _history(remote, *, now, max_observations):
    """Audit every bounded row, then retain only canonical public query records."""
    if (type(max_observations) is not int or not 1 <= max_observations <= 10_000):
        raise FeatureError('features_invalid_plan')
    if not remote.read_only or remote.db.in_transaction:
        raise FeatureError('features_remote_invalid')
    remote._guard()
    original = os.stat(remote.path, follow_symlinks=False)
    original_version = remote.db.execute('PRAGMA data_version').fetchone()[0]
    rows, size = [], 0
    remote.db.execute('BEGIN')
    try:
        cursor = remote.db.execute('SELECT id,CASE WHEN length(run_id)<=36 THEN run_id END,'
            'CASE WHEN length(store_id)<=19 THEN store_id END,ok,'
            'CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json END '
            'FROM remote_samples ORDER BY id LIMIT ?', (MAX_RECORD, max_observations+1))
        for index, run, store, ok, encoded in cursor:
            if len(rows) >= max_observations:
                raise FeatureError('features_history_limit')
            if type(encoded) is not str:
                raise ValueError
            size += len(encoded.encode('utf-8'))
            if size > MAX_HISTORY_BYTES:
                raise FeatureError('features_history_limit')
            if type(run) is not str or len(run) != 36 or str(UUID(run)) != run:
                raise ValueError
            stored = json.loads(encoded, object_pairs_hook=_unique_json_object,
                                parse_constant=_reject_json_constant)
            record = validate_record(stored)
            if store != record['requested_store_id'] or type(ok) is not int or ok != int(record['ok']):
                raise ValueError
            completed = pair_completed(record)
            receipt = read_receipt(stored, record, run)
            if completed > now or receipt is not None and receipt > now:
                raise ValueError
            rows.append((run, record, completed, receipt))
        remote._guard()
        current = os.stat(remote.path, follow_symlinks=False)
        if ((current.st_size, current.st_mtime_ns, current.st_ctime_ns) !=
                (original.st_size, original.st_mtime_ns, original.st_ctime_ns)
                or remote.db.execute('PRAGMA data_version').fetchone()[0] != original_version):
            raise ValueError
        return rows
    except FeatureError:
        raise
    except Exception:
        raise FeatureError('features_remote_invalid') from None
    finally:
        remote.db.rollback()


def _queue_features(history, *, store_id, at, window_seconds, max_gap_seconds):
    start = at-timedelta(seconds=window_seconds)
    middle = start+timedelta(seconds=window_seconds/2)
    streams = {e: _Stream(e, start, middle, at, max_gap_seconds) for e in ENDPOINTS}
    latest = {e: None for e in ENDPOINTS}
    admitted = []
    for run, record, completed, receipt in history:
        if record['requested_store_id'] != store_id or completed > at:
            continue
        if receipt is None:
            if completed >= start:
                for stream in streams.values():
                    stream.invalidate('local_receipt_missing')
                latest = {e: None for e in ENDPOINTS}
            continue
        if receipt > at:
            continue
        queries = record['queries']
        for endpoint, stream in streams.items():
            query = queries[endpoint]
            stream.add(query, run, completed)
            # Never expose the last good payload as current after a failure or
            # non-increasing response. Availability and payload agree.
            latest[endpoint] = query['payload'] if (query['ok'] and
                stream.kind == 'success' and stream.latest is not None and
                stream.latest >= start and stream.latest == _time(query['received_at'])) else None
        if completed >= start:
            admitted.append({'run_id': run, 'record': record, 'local_received_at': _utc(receipt)})
    queue = streams['groupqueues'].finish(window_seconds, False)
    count = streams['storequeuecount'].finish(window_seconds, False)
    queue_payload, count_payload = latest['groupqueues'], latest['storequeuecount']
    feature = {'source': SOURCE, 'queue_availability': queue['availability'],
        'queue_response_age_seconds': queue['last_success_response_age_seconds'],
        'count_availability': count['availability'],
        'count_response_age_seconds': count['last_success_response_age_seconds'],
        'display_sizes': {key: {'array_length': len(queue_payload['queues'][key]),
            'distinct_labels': len(set(queue_payload['queues'][key]))} for key in QUEUE_NAMES}
            if queue_payload is not None else None,
        'reported_count_raw': count_payload['raw_count'] if count_payload is not None else None,
        'count_unit': 'unknown', 'display_window_statistics': queue['queues'],
        'reported_count_window_statistics': count['reported_count'],
        'queue_store_binding_verified': False, 'source_freshness': 'unknown',
        'atomic_snapshot': False, 'true_no_show_rate': None}
    return feature, hashlib.sha256(_canonical(admitted).encode()).hexdigest()


def build_feature_dataset(*, source, reviews, remote, plan,
                          max_revisions=10_000, max_observations=10_000):
    plan = validate_feature_plan(plan)
    if type(max_revisions) is not int or not 1 <= max_revisions <= 10_000:
        raise FeatureError('features_invalid_plan')
    try:
        audit, targets = reviews._selection(source=source, as_of=plan['as_of'],
            data_origin=plan['data_origin'], api_profile=plan['api_profile'], max_revisions=max_revisions)
        scoped = [target for target in targets if target['episode']['store_id'] == plan['store_id']]
        work, excluded = [], 0
        for target in scoped:
            events = {e['event_type']: e for e in target['episode']['events']}
            issued, called = events['issued'], events['called']
            for elapsed in plan['elapsed_seconds']:
                made = _time(issued['event_time_upper'])+timedelta(seconds=elapsed)
                if made >= _time(called['event_time_lower']):
                    excluded += 1
                    continue
                work.append((made, elapsed, target, events))
        if len(work) > plan['max_cases']:
            raise FeatureError('features_case_limit')
        history = _history(remote, now=_clock(), max_observations=max_observations)
        work.sort(key=lambda row: (row[0], row[2]['source_receipt_sha256'], row[1]))
        rows = []
        for made, elapsed, target, events in work:
            episode = target['episode']
            queue, input_digest = _queue_features(history, store_id=plan['store_id'], at=made,
                window_seconds=plan['window_seconds'], max_gap_seconds=plan['max_gap_seconds'])
            inputs = {'store_id': episode['store_id'], 'queue_type': episode['queue_type'],
                'party_size': episode['party_size'], 'table_type': episode['table_type'],
                'elapsed_seconds': elapsed, 'calendar_at_prediction': date_features(_utc(made), as_of=_utc(made)),
                'queue_observations': queue}
            features_digest = hashlib.sha256(_canonical(inputs).encode()).hexdigest()
            rows.append({'episode_id': episode['episode_id'],
                'reconstructed_prediction_at': _utc(made), 'features': inputs,
                'features_sha256': features_digest, 'admitted_queue_inputs_sha256': input_digest,
                'truth_source_receipt_sha256': target['source_receipt_sha256'],
                'truth_available_at': target['available_at'],
                'target_remaining_call_interval_us': {
                    'lower': _us(_time(events['called']['event_time_lower'])-made),
                    'upper': _us(_time(events['called']['event_time_upper'])-made)},
                'historical_target_context_verified': False, 'training_eligible': False})
        fingerprint = hashlib.sha256(_canonical({'policy': POLICY, 'plan': plan, 'rows': rows}).encode()).hexdigest()
        return {'feature_dataset_schema_version': 1, 'policy': POLICY, 'plan': plan,
            'feature_dataset_sha256': fingerprint, 'rows': rows,
            'reviewed_scope_episodes': len(scoped), 'research_rows': len(rows),
            'excluded_call_not_definitely_future': excluded,
            'audit_diagnostics_not_features': {'outcomes_and_reviews': audit, 'queue_rows_audited': len(history)},
            'queue_origin_basis': 'explicit_operator_declaration_not_transport_attestation',
            'target_context_basis': 'reviewed_outcome_reconstruction_not_historical_request',
            'availability_basis': 'local_writer_first_receipt_before_commit',
            'historical_availability_verified': False, 'durable_availability_verified': False,
            'independent_time_attestation': False, 'queue_store_binding_verified': False,
            'forecast_log_verified': False, 'verified_training_labels': 0, 'training_eligible': False,
            'eta_available': False, 'model_fitted': False, 'output_requires_private_handling': True,
            'network_performed': False}
    except FeatureError:
        raise
    except ReviewError:
        raise FeatureError('features_source_invalid') from None
    except Exception:
        raise FeatureError('features_operation_failed') from None


def write_feature_dataset(*, source, reviews, remote, plan, destination,
                          max_revisions=10_000, max_observations=10_000):
    result = build_feature_dataset(source=source, reviews=reviews, remote=remote, plan=plan,
                                  max_revisions=max_revisions, max_observations=max_observations)
    body = _canonical(result).encode()
    if len(body) > 2_097_152:
        raise FeatureError('features_operation_failed')
    try:
        publication = _write_packet(body, destination)
    except PacketError as error:
        raise FeatureError('features_output_exists' if error.error_code == 'packet_output_exists'
                           else 'features_operation_failed', committed=error.committed) from None
    return {'feature_dataset_schema_version': 1, 'artifact_written': True, 'committed': True,
        'durability_confirmed': publication['durability_confirmed'],
        'reviewed_scope_episodes': result['reviewed_scope_episodes'], 'research_rows': result['research_rows'],
        'verified_training_labels': 0, 'training_eligible': False, 'eta_available': False,
        'model_fitted': False, 'output_requires_private_handling': True, 'network_performed': False}
