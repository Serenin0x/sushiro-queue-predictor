"""Offline shadow targets for sparse official display archives, never tickets.

Targets are chosen from the current reference plus fixed offsets, before any
future rows are inspected. Brackets concern a sampled reference under an
explicit monotonic-between-samples assumption, not authenticated call events.
There is no network, account access, training-label promotion or live adapter.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median
import re

POLICY = 'sparse_display_shadow_v1'
SOURCE = 'sapi_miniapp_gateway'
SEMANTICS = 'one_official_detail_response_not_two_query_pair'
METHODS = ('endpoint_30', 'median_30', 'median_60')
SHANGHAI = timezone(timedelta(hours=8))


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise ValueError('invalid_archive_time')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def _points(detail, field):
    if (detail.get('source') != SOURCE or detail.get('api_profile') != 'miniapp_gateway'
            or detail.get('daily_schema_version') != 1
            or detail.get('network_performed_by_read') is not False
            or detail.get('first_label_is_confirmed_call') is not False
            or detail.get('eta_available') is not False
            or detail.get('call_reference_semantics') != 'user_assumed_first_displayed_label'
            or detail.get('graph_truncated') is not False):
        raise ValueError('unsupported_archive_scope')
    day = detail['local_date']
    datetime.strptime(day, '%Y-%m-%d')
    rows = detail.get('points')
    if not isinstance(rows, list) or len(rows) > 2048 or detail.get('returned_graph_points') != len(rows):
        raise ValueError('invalid_archive_points')
    generated = _time(detail['generated_at'])
    result, previous, ids = [], None, set()
    for row in rows:
        start = _time(row['request_started_at'])
        at = _time(row['queue_received_at']) if row.get('queue_received_at') is not None else start
        if (start > at or at > generated or at.astimezone(SHANGHAI).date().isoformat() != day
                or previous is not None and at <= previous):
            raise ValueError('invalid_archive_order')
        previous = at
        label_id = row.get('observation_id')
        if not isinstance(label_id, str) or not label_id or label_id in ids:
            raise ValueError('invalid_observation_identity')
        ids.add(label_id)
        position, reason = None, None
        labels = (row.get('queues') or {}).get(field)
        if (row.get('pair_ok') is not True or row.get('scheduled_pause') is not False
                or row.get('success_semantics') != SEMANTICS or row.get('error_codes') != {}):
            reason = 'failed_or_paused'
        elif (row.get('queue_field_presence') or {}).get(field) != 'present':
            reason = 'field_missing'
        elif labels == []:
            reason = 'empty_display'
        elif (not isinstance(labels, list) or not 1 <= len(labels) <= 3
                or any(not isinstance(v, str) or not re.fullmatch(r'[0-9]{1,7}', v) for v in labels)):
            reason = 'unsupported_labels'
        else:
            position = int(labels[0])
        link = row.get('continuity') or {}
        result.append(dict(at=at, position=position, labels=list(labels) if reason is None else None,
                           reason=reason, id=label_id,
                           predecessor=link.get('predecessor_id'), writer=link.get('writer_id'),
                           predecessor_writer=link.get('predecessor_writer_id')))
    return result


def _boundary(previous, current, max_gap_seconds):
    if current['reason']:
        return current['reason']
    if (not current['writer'] or current['writer'] != previous['writer']
            or current['predecessor_writer'] != previous['writer']
            or current['predecessor'] != previous['id']):
        return 'writer_or_link_boundary'
    if (current['at'] - previous['at']).total_seconds() > max_gap_seconds:
        return 'sampling_gap'
    if current['position'] < previous['position']:
        return 'reference_reversal'
    return None


def _forecast(segment, offset, method, horizon):
    window = int(method.rsplit('_', 1)[1])
    end = segment[-1]
    rows = [p for p in segment if p['at'] >= end['at'] - timedelta(minutes=window)]
    if len(rows) > 128:
        return {'available': False, 'reason': 'unsupported_sampling_density'}
    if len(rows) < 4 or (end['at'] - rows[0]['at']).total_seconds() < 900:
        return {'available': False, 'reason': 'insufficient_history'}
    def rate(a, b):
        return (b['position'] - a['position']) * 60 / (b['at'] - a['at']).total_seconds()
    speed = (rate(rows[0], end) if method.startswith('endpoint') else
             median(rate(a, b) for i, a in enumerate(rows) for b in rows[i+1:]))
    if speed <= 0:
        return {'available': False, 'reason': 'no_positive_trend'}
    minutes = offset / speed
    if minutes > horizon:
        return {'available': False, 'reason': 'outside_horizon'}
    return {'available': True, 'minutes': minutes, 'rate_positions_per_minute': speed,
            'input_samples': len(rows), 'input_cutoff': _iso(end['at'])}


def _outcome(points, origin, target, max_gap, horizon):
    first = previous = points[origin]
    elapsed = 0.0
    for current in points[origin+1:]:
        elapsed = (current['at'] - first['at']).total_seconds() / 60
        if elapsed > horizon:
            return {'state': 'horizon_censored', 'lower_minutes': (previous['at']-first['at']).total_seconds()/60,
                    'upper_minutes': None}
        boundary = _boundary(previous, current, max_gap)
        if boundary:
            return {'state': 'boundary_censored', 'reason': boundary,
                    'lower_minutes': (previous['at']-first['at']).total_seconds()/60, 'upper_minutes': None}
        if current['position'] >= target:
            return {'state': 'sampled_bracket',
                    'lower_minutes': (previous['at']-first['at']).total_seconds()/60,
                    'upper_minutes': elapsed}
        previous = current
    return {'state': 'archive_end_censored', 'lower_minutes': elapsed, 'upper_minutes': None}


def replay(detail, *, queue='ordinary', offsets=(20, 50, 100), max_gap_seconds=660,
           stride_minutes=15, horizon_minutes=120):
    """Generate paired research forecasts and keep unavailable/censored cases."""
    if queue not in {'ordinary', 'reservation'}:
        raise ValueError('unsupported_queue')
    if (not isinstance(offsets, (tuple, list)) or not 1 <= len(offsets) <= 10
            or any(type(v) is not int or not 1 <= v <= 1000 for v in offsets)
            or len(set(offsets)) != len(offsets)
            or type(max_gap_seconds) is not int or not 30 <= max_gap_seconds <= 1800
            or type(stride_minutes) is not int or not 5 <= stride_minutes <= 60
            or type(horizon_minutes) is not int or not 15 <= horizon_minutes <= 120):
        raise ValueError('invalid_replay_policy')
    points = _points(detail, 'mixedQueue' if queue == 'ordinary' else 'reservationQueue')
    segment, cases, invalid, buckets = [], [], Counter(), set()
    for index, point in enumerate(points):
        if point['reason']:
            invalid[point['reason']] += 1
            segment = []
            continue
        if segment and _boundary(segment[-1], point, max_gap_seconds):
            segment = []
        segment.append(point)
        segment = [p for p in segment if p['at'] >= point['at']-timedelta(minutes=60)]
        # Fixed wall-clock bins, no knowledge of future queue progression.
        bucket = int(point['at'].timestamp()) // (stride_minutes * 60)
        if bucket in buckets:
            continue
        buckets.add(bucket)
        for offset in offsets:
            forecasts = {method: _forecast(segment, offset, method, horizon_minutes) for method in METHODS}
            target = point['position'] + offset
            outcome = _outcome(points, index, target, max_gap_seconds, horizon_minutes)
            if outcome['state'] == 'sampled_bracket':
                lo, hi = outcome['lower_minutes'], outcome['upper_minutes']
                for prediction in forecasts.values():
                    if prediction['available']:
                        t = prediction['minutes']
                        prediction['bracket_error_lower_minutes'] = max(lo-t, t-hi, 0)
                        prediction['bracket_error_upper_minutes'] = max(abs(t-lo), abs(t-hi))
            cases.append({'origin_at': _iso(point['at']), 'reference': point['position'],
                          'target': target, 'offset': offset, 'forecasts': forecasts, 'outcome': outcome})
    return {'policy': POLICY, 'source': SOURCE, 'store_id': detail['requested_store_id'],
            'local_date': detail['local_date'], 'queue': queue, 'upstream_requests': 0,
            'tickets_created': 0, 'actual_call_verified': False, 'eta_available': False,
            'target_was_issued': None, 'source_freshness': 'unknown',
            'target_semantics': 'sampled_first_position_threshold',
            'bracket_assumption': 'unverified_monotonic_reference_between_samples',
            'max_gap_seconds': max_gap_seconds, 'stride_minutes': stride_minutes,
            'horizon_minutes': horizon_minutes, 'offsets': list(offsets),
            'sample_count': len(points), 'invalid_samples': dict(invalid), 'cases': cases,
            'summary': summarize(cases)}


def summarize(cases):
    paired = [c for c in cases if c['outcome']['state'] == 'sampled_bracket'
              and all(c['forecasts'][m]['available'] for m in METHODS)]
    result = {'cases': len(cases), 'outcome_states': dict(Counter(c['outcome']['state'] for c in cases)),
              'paired_cases': len(paired), 'methods': {}}
    for method in METHODS:
        forecasts = [c['forecasts'][method] for c in cases]
        scored = [c['forecasts'][method] for c in paired]
        result['methods'][method] = {
            'available': sum(p['available'] for p in forecasts),
            'unavailable_reasons': dict(Counter(p['reason'] for p in forecasts if not p['available'])),
            'paired_mean_bracket_error_lower_minutes': mean(p['bracket_error_lower_minutes'] for p in scored) if scored else None,
            'paired_mean_bracket_error_upper_minutes': mean(p['bracket_error_upper_minutes'] for p in scored) if scored else None}
    return result
