"""Bounded diagnostics of a saved daily projection, without source or DB I/O.

Operation starts include local admission waiting. Receipt coverage measures
local observations, never authenticated restaurant update or call times.
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

from .remote import SOURCE, _id
from .remotetasks import RemoteTaskError, _at, _now

POLICY = 'daily_sampling_quality_v1'
ZONE = ZoneInfo('Asia/Shanghai')


def daily_quality_report(projection, *, store_id, day, as_of,
                         interval_seconds=60, max_gap_seconds=90, rapid_positions=50):
    """Describe only the available projection cutoff; later as_of cannot fill it.

    Complete elapsed bins exclude the currently unfinished bin. Truncated
    graphs produce subset diagnostics and cannot pass whole-day observation.
    """
    from .dailyarchive import _projection, selected_date
    try:
        _id(store_id); selected_date(day)
        if (type(interval_seconds) is not int or not 1 <= interval_seconds <= 7200
                or type(max_gap_seconds) is not int or not interval_seconds <= max_gap_seconds <= 7200
                or type(rapid_positions) is not int or not 1 <= rapid_positions <= 1000000):
            raise ValueError
        value = _projection(projection, store_id, day)
        requested, cutoff = _at(as_of), _at(value['generated_at'])
        if requested < cutoff:
            raise RemoteTaskError('daily_quality_future_projection')
    except RemoteTaskError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
        raise RemoteTaskError('daily_quality_invalid_projection_or_policy') from None
    summary, rows = value['summary'], value['points']
    midnight = datetime.fromisoformat(day).replace(tzinfo=ZONE)
    spans = []
    try:
        for start, end in summary['declared_intervals'] if summary else []:
            a, b = _at(start), _at(end)
            if (not midnight <= a < b <= midnight + timedelta(days=1)
                    or spans and a < spans[-1][1]):
                raise ValueError
            spans.append((a, b))
    except (ValueError, TypeError, KeyError):
        raise RemoteTaskError('daily_quality_invalid_declared_intervals') from None
    full = bool(summary and not value['graph_truncated'] and not summary['graph_truncated']
                and summary['observations'] == len(rows))
    complete_bins = set()
    total_bins = 0
    for i, (a, b) in enumerate(spans):
        total_bins += math.ceil((b-a).total_seconds()/interval_seconds)
        end = min(b, cutoff)
        n = max(0, math.floor((end-a).total_seconds()/interval_seconds))
        # The last partial declared bin is complete when the window closes.
        if cutoff >= b: n = math.ceil((b-a).total_seconds()/interval_seconds)
        complete_bins.update((i, k) for k in range(n))

    def bin_at(stamp):
        for i, (a, b) in enumerate(spans):
            if a <= stamp < b:
                return i, int((stamp-a).total_seconds()//interval_seconds)
        return None

    anomalies, errors, categories = Counter(), Counter(), Counter()
    operation_bins, receipt_bins, queue_good = set(), set(), 0
    receipt_by_span = [[] for _ in spans]
    operation_intervals, receipt_intervals, lags, pair_offsets = [], [], [], []
    last_start = last_receipt = previous = None
    changes = {q: {'empty_first_labels': 0, 'non_numeric_first_labels': 0,
        'comparable_numeric_steps': 0, 'backward_steps': 0,
        'rapid_forward_steps': 0} for q in ('mixedQueue', 'reservationQueue')}
    boundaries = Counter()
    for row in rows:
        start = _at(row['request_started_at'])
        qtime = _at(row['queue_received_at']) if row['queue_received_at'] else None
        ctime = _at(row['count_received_at']) if row['count_received_at'] else None
        errors.update(row['error_codes'].values())
        categories['scheduled_pause_slots' if row['scheduled_pause'] else
                   'successful_pairs' if row['pair_ok'] else 'failed_pairs'] += 1
        boundaries[row['comparison_state']] += 1
        if last_start is not None:
            delta = (start-last_start).total_seconds()
            if delta <= 0: anomalies['operation_time_duplicate_or_reversed'] += 1
            else: operation_intervals.append(delta)
        last_start = start
        future = start > cutoff or any(t is not None and t > cutoff for t in (qtime, ctime))
        inverted = any(t is not None and t < start for t in (qtime, ctime))
        if future: anomalies['point_after_projection_cutoff'] += 1
        if inverted: anomalies['response_before_operation_start'] += 1
        if row['queues'] is not None and qtime is None:
            anomalies['queue_payload_without_receipt'] += 1
        if row['count_raw'] is not None and ctime is None:
            anomalies['count_payload_without_receipt'] += 1
        if row['pair_ok'] and (row['queues'] is None or row['count_raw'] is None
                or row['error_codes'] or row['scheduled_pause']):
            anomalies['successful_pair_conflicts_with_fields'] += 1
        if ctime is not None and qtime is not None:
            offset = (ctime-qtime).total_seconds(); pair_offsets.append(offset)
            if offset < 0: anomalies['count_receipt_before_queue_receipt'] += 1
        usable = (row['queues'] is not None and qtime is not None
                  and 'groupqueues' not in row['error_codes'] and not future and not inverted)
        if not usable:
            previous = None
            continue
        queue_good += 1
        lag = (qtime-start).total_seconds(); lags.append(lag)
        op_bin, recv_bin = bin_at(start), bin_at(qtime)
        if op_bin is not None: operation_bins.add(op_bin)
        if recv_bin is not None:
            receipt_bins.add(recv_bin); receipt_by_span[recv_bin[0]].append(qtime)
        elif op_bin is not None:
            anomalies['queue_received_outside_declared_window'] += 1
        if op_bin is None: anomalies['successful_operation_outside_declared_window'] += 1
        if last_receipt is not None:
            delta = (qtime-last_receipt).total_seconds()
            if delta <= 0: anomalies['queue_receipt_duplicate_or_reversed'] += 1
            elif bin_at(last_receipt) is not None and recv_bin is not None and bin_at(last_receipt)[0] == recv_bin[0]:
                receipt_intervals.append(delta)
        last_receipt = qtime
        comparable = (previous is not None and not row['scheduled_pause']
            and row['comparison_state'] == 'comparable_display_sets'
            and 0 < (qtime-previous[0]).total_seconds() <= max_gap_seconds
            and recv_bin is not None and bin_at(previous[0]) is not None
            and recv_bin[0] == bin_at(previous[0])[0])
        for name, stats in changes.items():
            labels = row['queues'][name]; label = labels[0] if labels else None
            if label is None: stats['empty_first_labels'] += 1
            elif not re.fullmatch('[0-9]{1,7}', label): stats['non_numeric_first_labels'] += 1
            if comparable and label is not None and re.fullmatch('[0-9]{1,7}', label):
                old = previous[1][name]
                if old and re.fullmatch('[0-9]{1,7}', old[0]):
                    difference = int(label)-int(old[0])
                    stats['comparable_numeric_steps'] += 1
                    stats['backward_steps'] += int(difference < 0)
                    stats['rapid_forward_steps'] += int(difference >= rapid_positions)
        previous = None if row['scheduled_pause'] else (qtime, row['queues'])
    if full:
        for key in ('successful_pairs', 'failed_pairs', 'scheduled_pause_slots'):
            if categories[key] != summary[key]: anomalies['summary_category_count_mismatch'] += 1
        if queue_good != summary['group_successes']:
            anomalies['summary_queue_success_count_mismatch'] += 1

    boundary_gaps = []
    for i, (a, b) in enumerate(spans):
        end = min(b, cutoff)
        if end <= a: continue
        times = sorted(set(receipt_by_span[i]))
        if times:
            lead, tail = (times[0]-a).total_seconds(), (end-times[-1]).total_seconds()
        else: lead = tail = (end-a).total_seconds()
        boundary_gaps.append({'declared_interval_index': i, 'received_samples': len(receipt_by_span[i]),
            'opening_to_first_receipt_seconds': lead, 'last_receipt_to_cutoff_seconds': tail,
            'opening_gap_over_threshold': lead > max_gap_seconds,
            'trailing_gap_over_threshold': tail > max_gap_seconds})
    received_complete = receipt_bins & complete_bins
    started_complete = operation_bins & complete_bins
    missing = complete_bins-received_complete
    longest_missing = 0
    for i in range(len(spans)):
        run = 0
        for _, k in sorted(x for x in complete_bins if x[0] == i):
            run = run+1 if (i, k) in missing else 0
            longest_missing = max(longest_missing, run)
    closed = bool(spans and cutoff >= spans[-1][1])
    issues = (bool(anomalies) or categories['failed_pairs'] > 0 or bool(missing)
              or any(g['opening_gap_over_threshold'] or g['trailing_gap_over_threshold'] for g in boundary_gaps)
              or any(t > max_gap_seconds for t in receipt_intervals))
    state = ('no_observations' if not rows else 'partial_projection' if not full else
             'attention' if issues else 'declared_day_observed' if closed else 'in_progress')
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    extent = lambda xs: {'count': len(xs), 'minimum_seconds': min(xs) if xs else None,
        'maximum_seconds': max(xs) if xs else None}
    return {'daily_quality_schema_version': 1, 'policy': POLICY, 'source': SOURCE,
        'store_id': store_id, 'local_date': day, 'requested_as_of': _now(requested),
        'projection_cutoff': _now(cutoff), 'projection_age_seconds': (requested-cutoff).total_seconds(),
        'projection_content_sha256': hashlib.sha256(canonical).hexdigest(),
        'quality_state': state, 'complete_projection_available': full,
        'declared_day_closed_at_projection_cutoff': closed,
        'declared_day_observed_without_detected_gap': state == 'declared_day_observed',
        'parameters': {'background_interval_seconds': interval_seconds,
            'maximum_receipt_gap_seconds': max_gap_seconds, 'rapid_forward_positions': rapid_positions},
        'returned_projection_rows': len(rows), 'total_saved_observations': summary['observations'] if summary else None,
        'subset_categories': {k: categories[k] for k in ('successful_pairs', 'failed_pairs', 'scheduled_pause_slots')},
        'subset_queue_usable_responses': queue_good, 'error_counts': dict(sorted(errors.items())),
        'anomaly_counts': dict(sorted(anomalies.items())), 'comparison_boundaries': dict(sorted(boundaries.items())),
        'coverage': {'declared_full_day_background_bins': total_bins if summary else None,
            'completed_background_bins': len(complete_bins) if summary else None,
            'operation_start_covered_completed_bins': len(started_complete),
            'queue_receipt_covered_completed_bins': len(received_complete),
            'uncovered_completed_bins_in_returned_projection': len(missing) if summary else None,
            'queue_receipt_missing_completed_bins': len(missing) if full else None,
            'longest_missing_completed_bin_run': longest_missing if full else None,
            'completed_bin_receipt_fraction': len(received_complete)/len(complete_bins) if full and complete_bins else None,
            'returned_projection_coverage_lower_bound_fraction': len(received_complete)/len(complete_bins) if complete_bins else None,
            'currently_unfinished_bin_excluded': True, 'semantics': 'local_receipts_in_declared_completed_background_bins'},
        'operation_start_intervals': extent(operation_intervals), 'queue_receipt_intervals': {
            **extent(receipt_intervals), 'over_threshold_count': sum(t > max_gap_seconds for t in receipt_intervals)},
        'operation_to_queue_receipt': extent(lags), 'queue_to_count_receipt': extent(pair_offsets),
        'declared_window_boundary_gaps': boundary_gaps, 'first_label_changes': changes,
        'operation_start_is_network_send_time': False, 'source_freshness': 'unknown',
        'store_identity_verified': False, 'business_hours_verified': False, 'actual_called_count': None,
        'no_show_rate': None, 'actual_call_verified': False, 'independent_backup': False,
        'continuous_multi_day_quality_verified': False, 'network_performed_by_report': False,
        'eta_available': False}
