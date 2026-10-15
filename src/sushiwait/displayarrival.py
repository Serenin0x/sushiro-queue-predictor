"""User-defined arrival labels from saved displays and a private ticket.

The first two displayed positions or three numbers strictly beyond a ticket
are useful proxy outcomes. They do not certify check-in, service or no-show.
This pure function performs no requests, writes or personal-data publication.
"""
from __future__ import annotations

import re

from .displayreplay import SHANGHAI, _boundary, _iso, _points, _time

POLICY = 'display_top_two_or_all_three_passed_v3'


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,7}', value) or int(value) < 1:
        raise ValueError('unsupported_ticket_number')
    return int(value)


def classify_display(number, labels):
    """Preserve display order; one larger number is never enough to pass."""
    target = _number(number)
    if labels is None:
        return 'unavailable'
    if labels == []:
        return 'empty_display'
    if not isinstance(labels, list) or not 1 <= len(labels) <= 3:
        return 'unsupported_labels'
    try:
        values = [_number(value) for value in labels]
    except ValueError:
        return 'unsupported_labels'
    if target in values[:2]:
        return 'display_top_two'
    if len(values) == 3 and all(value > target for value in values):
        return 'display_all_three_passed'
    if target in values:
        return 'display_third'
    return 'not_reached'


def label_ticket_arrival(detail, *, store_id, number, issued_at, queue='ordinary',
                         checked_in=None, max_gap_seconds=660):
    """Return the earliest accepted proxy outcome and its sampling interval.

    The caller supplies a saved, complete official store/day archive. Match
    store, Shanghai issue date and selected queue before comparing numbers.
    A first already-positive sample has the whole elapsed wait as its interval;
    gaps widen the interval instead of discarding useful later observations.
    A detected number reversal suspends numeric passing inference for the rest
    of the episode. An exact first/second-position match remains observable.
    """
    target = _number(number)
    issued = _time(issued_at)
    if (not isinstance(store_id, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', store_id)
            or detail.get('requested_store_id') != store_id
            or detail.get('local_date') != issued.astimezone(SHANGHAI).date().isoformat()
            or issued > _time(detail['generated_at'])):
        raise ValueError('ticket_archive_scope_mismatch')
    if (queue not in ('ordinary', 'reservation')
            or checked_in is not None and type(checked_in) is not bool
            or type(max_gap_seconds) is not int or not 30 <= max_gap_seconds <= 1800):
        raise ValueError('invalid_arrival_policy')
    points = _points(detail, 'mixedQueue' if queue == 'ordinary' else 'reservationQueue')
    result = {'policy': POLICY, 'prediction_target': 'user_defined_queue_arrival',
        'store_id': store_id, 'local_date': detail['local_date'], 'queue_type': queue,
        'number': number, 'issued_at': _iso(issued), 'checked_in_reported': checked_in,
        'result_available': False, 'state': 'awaiting_observation', 'label_kind': None,
        'detected_at': None, 'arrival_interval': None, 'wait_minutes': None,
        'last_observed_at': None, 'quality_flags': [], 'observation_id': None,
        'display_numbers': None, 'display_position': None,
        'actual_call_verified': False, 'no_show_verified': False,
        'source_update_time_verified': False, 'network_performed': False}
    # A number already passed before its reported issue time cannot become a
    # new arrival at the first subsequent poll. Preserve the contradiction;
    # an exact subsequent appearance can still supply direct display evidence.
    before_issue = [p for p in points if p['at'] < issued and not p['reason']]
    prior = before_issue[-1] if before_issue else None
    conflict = bool(prior and classify_display(number, prior['labels']) == 'display_all_three_passed')
    previous = last_negative = None
    inferred_suspended = False
    flags = set()
    for point in points:
        if point['at'] < issued:
            continue
        result['last_observed_at'] = _iso(point['at'])
        state = classify_display(number, point['labels'])
        if point['reason'] or state in ('unavailable', 'empty_display', 'unsupported_labels'):
            flags.add(point['reason'] or state)
            last_negative = None
            result['state'] = 'awaiting_observation'
            continue
        if previous is not None:
            boundary = _boundary(previous, point, max_gap_seconds)
            if boundary:
                flags.add(boundary)
                last_negative = None
            # Check this separately: a gap or writer boundary must not hide a reset.
            if point['position'] < previous['position']:
                backfill = (len(point['labels']) == len(previous['labels']) == 3
                    and max(map(int,point['labels'])) >= min(map(int,previous['labels'])))
                if backfill:
                    flags.add('display_position_backfill')
                else:
                    flags.add('number_reversal')
                    inferred_suspended = True
                    last_negative = None
        previous = point
        direct = state == 'display_top_two'
        passed = state == 'display_all_three_passed' and not inferred_suspended
        if passed and conflict:
            result.update(state='input_conflict', reason='ticket_precedes_issue',
                prior_sample_at=_iso(prior['at']), prior_display_numbers=prior['labels'])
            flags.add('display_passed_before_reported_issue')
            continue
        if direct or passed:
            if direct:
                for key in ('reason', 'prior_sample_at', 'prior_display_numbers'):
                    result.pop(key, None)
            lower = last_negative['at'] if last_negative is not None else issued
            if last_negative is None:
                flags.add('no_adjacent_before_arrival_sample')
            if direct and checked_in is False:
                flags.add('display_match_despite_reported_no_checkin')
            result.update(result_available=True, state='arrived' if direct else 'inferred_arrived',
                label_kind=state, detected_at=_iso(point['at']),
                arrival_interval={'lower_at': _iso(lower), 'upper_at': _iso(point['at']),
                    'basis': 'adjacent_samples' if last_negative is not None else 'issue_to_detection'},
                wait_minutes={'lower': (lower-issued).total_seconds()/60,
                    'upper': (point['at']-issued).total_seconds()/60},
                observation_id=point['id'], display_numbers=point['labels'],
                display_position=next((i+1 for i, value in enumerate(point['labels'])
                    if int(value) == target), None), quality_flags=sorted(flags))
            return result
        result['state'] = ('number_cycle_uncertain' if inferred_suspended
                           else 'upcoming' if state == 'display_third' else 'waiting')
        if not inferred_suspended:
            last_negative = point
    result['quality_flags'] = sorted(flags)
    if conflict and not result['result_available']:
        result.update(state='input_conflict', reason='ticket_precedes_issue',
            prior_sample_at=_iso(prior['at']), prior_display_numbers=prior['labels'])
    return result
