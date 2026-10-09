"""Read-only terminal-window sampling evidence; never an ETA or source freshness proof."""
from __future__ import annotations

import os
from uuid import UUID
from zoneinfo import ZoneInfo

from .calendar import date_features
from .credentials import _read_private_file
from .remote import SOURCE, MAX_RECORD, QUEUE_NAMES, validate_record
from .remotetasks import RemoteTaskError, _at, _json, _EMPTY, _chain
from .remotewindow import _decode_window


def terminal_checkpoint(path, *, as_of):
    """Reject running checkpoints before a caller opens their locked database."""
    raw = _read_private_file(path)
    value = _decode_window(raw)
    at = _at(as_of)
    if value['state'] not in ('completed', 'failed') or value['pending'] is not None:
        raise RemoteTaskError('remote_quality_not_terminal')
    if at < _at(value['updated_at']):
        raise RemoteTaskError('remote_quality_future_checkpoint')
    return raw, value


def window_quality_report(database, task_file, *, as_of, max_gap_seconds=360):
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 7200:
        raise RemoteTaskError('remote_quality_invalid_gap_bound')
    if not database.read_only:
        raise RemoteTaskError('remote_quality_requires_read_only_database')
    raw, task = terminal_checkpoint(task_file, as_of=as_of)
    database._guard()
    def fingerprint():
        info = os.stat(database.path, follow_symlinks=False)
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns
    original_fingerprint = fingerprint()
    original_data_version = database.db.execute('PRAGMA data_version').fetchone()[0]
    config = task['config']
    if (str(database.path) != config['db'] or list(database.identity) != task['database_identity']):
        raise RemoteTaskError('remote_quality_database_conflict')
    first = _at(task['created_at'])
    deadline = _at(task['deadline_at'])
    through = min(_at(as_of), deadline)
    states = {store: {'successful_pairs': 0, 'failed_pairs': 0, 'http_attempts': 0,
        'days': {}, 'queue_changes': {name: 0 for name in QUEUE_NAMES},
        'count_changes': 0, 'run_boundaries': 0, 'long_gap_boundaries': 0,
        'responses_completed_after_deadline': 0, 'previous': None,
        'request_starts': [], 'successful_starts': []} for store in config['store_ids']}
    if 'business_hours' in config:
        for state in states.values():state['scheduled_pause_slots']=0
    count = attempts = successes = pauses = 0
    digest = _EMPTY
    previous_start = None
    rows = database.db.execute(
        'SELECT id,CASE WHEN length(run_id)<=36 THEN run_id END,'
        'CASE WHEN length(store_id)<=19 THEN store_id END,ok,CASE WHEN length(CAST(payload_json AS BLOB))<=? '
        'THEN payload_json END FROM remote_samples WHERE id>? ORDER BY id LIMIT ?',
        (MAX_RECORD, task['initial_id'], config['max_pairs'] + 2))
    for row in rows:
        count += 1
        try:
            UUID(row[1])
            record = validate_record(_json(row[4]))
            store = record['requested_store_id']
            if (row[0] != task['initial_id'] + count or store not in states
                    or row[2] != store or row[3] != int(record['ok'])):
                raise ValueError
            started = _at(record['queries']['groupqueues']['started_at'])
            ended = max(_at(q['received_at']) for q in record['queries'].values() if q['received_at'] is not None)
            if (not first <= started < deadline or ended > _at(task['updated_at'])
                    or previous_start is not None and started < previous_start):
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise RemoteTaskError('remote_quality_record_conflict') from None
        previous_start = started
        digest = _chain(digest, row)
        state = states[store]
        n = sum(q['attempted'] for q in record['queries'].values())
        attempts += n
        successes += int(record['ok'])
        state['http_attempts'] += n
        paused='business_hours' in config and any(q['error_code']=='business_window_closed' for q in record['queries'].values())
        pauses+=int(paused)
        category='scheduled_pause_slots' if paused else 'successful_pairs' if record['ok'] else 'failed_pairs'
        state[category] += 1
        state['request_starts'].append(started)
        state['responses_completed_after_deadline'] += int(ended > deadline)
        local = started.astimezone(ZoneInfo('Asia/Shanghai'))
        day = local.date().isoformat()
        bucket = state['days'].setdefault(day, {'successful_pairs': 0, 'failed_pairs': 0,
            'http_attempts': 0, 'hour_counts': {},
            'date_features': date_features(started.isoformat(), as_of=as_of)})
        if 'business_hours' in config:bucket.setdefault('scheduled_pause_slots',0)
        bucket[category] += 1
        bucket['http_attempts'] += n
        hour = str(local.hour)
        bucket['hour_counts'][hour] = bucket['hour_counts'].get(hour, 0) + 1
        prior = state['previous']
        if prior is not None and prior[0] != row[1]:
            state['run_boundaries'] += 1
        long_gap = prior is not None and (started-_at(prior[1]['queries']['groupqueues']['started_at'])).total_seconds() > max_gap_seconds
        state['long_gap_boundaries'] += int(long_gap)
        if record['ok']:
            state['successful_starts'].append(started)
            if prior is not None and prior[0] == row[1] and prior[1]['ok'] and not long_gap:
                previous = prior[1]['queries']
                current = record['queries']
                for name in QUEUE_NAMES:
                    state['queue_changes'][name] += int(
                        previous['groupqueues']['payload']['queues'][name]
                        != current['groupqueues']['payload']['queues'][name])
                state['count_changes'] += int(previous['storequeuecount']['payload']['raw_count']
                    != current['storequeuecount']['payload']['raw_count'])
        state['previous'] = row[1], record
    if (count != task['successful'] + task['failed'] + task.get('scheduled_pauses',0) or successes != task['successful']
            or pauses!=task.get('scheduled_pauses',0)
            or digest != task['records_digest'] or attempts != task['recorded_http_attempts']):
        raise RemoteTaskError('remote_quality_checkpoint_conflict')
    database._guard()
    if (fingerprint() != original_fingerprint
            or database.db.execute('PRAGMA data_version').fetchone()[0] != original_data_version):
        raise RemoteTaskError('remote_quality_database_changed')
    if _read_private_file(task_file) != raw:
        raise RemoteTaskError('remote_quality_checkpoint_changed')

    stores = []
    for store, state in states.items():
        starts = state.pop('request_starts')
        good = state.pop('successful_starts')
        state.pop('previous')
        gaps = [(b-a).total_seconds() for a, b in zip(starts, starts[1:])]
        success_gaps = [(b-a).total_seconds() for a, b in zip(good, good[1:])]
        start_gap = (good[0]-first).total_seconds() if good else (through-first).total_seconds()
        tail_gap = (through-good[-1]).total_seconds() if good else (through-first).total_seconds()
        stores.append({'requested_store_id': store, **state,
            'first_request_at': starts[0].isoformat() if starts else None,
            'last_request_at': starts[-1].isoformat() if starts else None,
            'request_span_seconds': (starts[-1]-starts[0]).total_seconds() if starts else 0,
            'request_interval_seconds': {'minimum': min(gaps) if gaps else None,
                'maximum': max(gaps) if gaps else None, 'intervals': len(gaps)},
            'successful_request_gap_seconds': {'start_boundary': start_gap,
                'end_boundary': tail_gap, 'maximum_internal': max(success_gaps) if success_gaps else None,
                'internal_gaps_above_threshold': sum(g > max_gap_seconds for g in success_gaps),
                'start_boundary_above_threshold': start_gap > max_gap_seconds,
                'end_boundary_above_threshold': tail_gap > max_gap_seconds}})
    return {'ok': True, 'schema_version': 1, 'source': SOURCE,
        'checkpoint_state': task['state'], 'end_reason': task['end_reason'],
        'created_at': task['created_at'], 'deadline_at': task['deadline_at'],
        'as_of': _at(as_of).isoformat(), 'reported_through': through.isoformat(),
        'planned_duration_seconds': config['duration_seconds'],
        'analysis_interval_seconds': (through-first).total_seconds(),
        'deadline_checkpoint_reached': task['end_reason'] == 'deadline',
        'max_gap_seconds': max_gap_seconds, 'rows_examined': count,
        'successful_pairs': successes, 'failed_pairs': count-successes-pauses, 'scheduled_pause_slots': pauses,
        'uncertain_pair_slots': task['uncertain'], 'recorded_http_attempts': attempts,
        'unrecorded_http_attempts': 'unknown' if task['uncertain'] else 0,
        'unknown_slots_by_store': 'unknown' if task['uncertain'] else 0,
        'checkpoint_chain_verified': True, 'stores': stores,
        'time_basis': 'client_request_start_reconstruction', 'source_freshness': 'unknown',
        'count_unit': 'unknown', 'response_store_identity_verified': False,
        'continuous_collection_verified': False, 'process_liveness': 'unknown',
        'verified_training_labels': 0, 'eta_available': False, 'network_performed': False}
