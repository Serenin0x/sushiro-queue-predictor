"""Bounded distribution fusion and public-only AI weight advice.

This module does arithmetic, not model fitting or provider calls. Interval
support remains uncertain, and no result is a calibrated or current ETA.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import math
import re

from .calendar import date_features
from .credentials import _read_private_file
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import PacketError, _write_packet
from .remote import SOURCE

POLICY = 'public_scenario_interval_mixture_v1'
EMPIRICAL_POLICY = 'public_empirical_interval_cdf_mixture_v2'
PPM = 1_000_000
MAX_WAIT_US = 172_800_000_000
MAX_BYTES = 16_384
CANDIDATE_IDS = frozenset({'history', 'steady', 'fast', 'slow', 'realtime'})
FEATURE_IDS = frozenset({'ordinary_removed_labels', 'ordinary_comparable_pairs',
    'reservation_removed_labels', 'reservation_comparable_pairs', 'reported_count_raw',
    'longest_gap_seconds', 'groupqueues_failures', 'storequeuecount_failures',
    'ordinary_observed_milliseconds', 'reservation_observed_milliseconds',
    'ordinary_turnover_rank_lower_ppm', 'ordinary_turnover_rank_upper_ppm',
    'ordinary_turnover_reference_windows', 'ordinary_turnover_reference_days',
    'reservation_turnover_rank_lower_ppm', 'reservation_turnover_rank_upper_ppm',
    'reservation_turnover_reference_windows', 'reservation_turnover_reference_days'})
REASONS = frozenset({'history_dominant', 'rapid_display_turnover',
    'slower_display_turnover', 'mixed_evidence', 'insufficient_evidence'})
_CONTEXT = {'schema_version', 'source', 'store_id', 'queue_type', 'observation_revision',
    'as_of', 'expires_at', 'window_seconds', 'max_local_age_seconds',
    'latest_queue_received_at', 'latest_count_received_at', 'features',
    'source_freshness', 'count_unit', 'store_identity_verified', 'collector_running', 'latest_queue_origin'}
_PLAN = {'schema_version', 'data_origin', 'model_version', 'prediction_target',
    'conditioning', 'public_context', 'candidates', 'prior_weights_ppm', 'ai_blend_ppm'}
_ADVICE = {'schema_version', 'public_input_sha256', 'observation_revision',
    'model_version', 'generated_at', 'expires_at', 'weights_ppm', 'feature_ids', 'reason_code'}
_CODES = frozenset({'fusion_invalid_plan', 'fusion_invalid_input', 'fusion_invalid_advice',
    'fusion_advice_missing', 'fusion_advice_expired', 'fusion_advice_clock_invalid',
    'fusion_advice_input_mismatch', 'fusion_advice_insufficient_evidence',
    'fusion_stale_queue', 'fusion_collector_not_running', 'fusion_saved_history_only', 'fusion_invalid_history',
    'fusion_superseded_input', 'fusion_output_exists',
    'fusion_operation_failed'})


class FusionError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if type(code) is str and code in _CODES else 'fusion_operation_failed'
        self.committed = committed is True
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _weights(value, ids):
    if (type(value) is not dict or set(value) != set(ids)
            or any(not _integer(v, 0, PPM) for v in value.values())
            or sum(value.values()) != PPM):
        raise ValueError
    return {k: value[k] for k in sorted(ids)}


def validate_plan(value, *, now=None):
    """Only allow public aggregates in the AI context; private atoms stay local."""
    try:
        clock = _clock() if now is None else now
        if not isinstance(clock, datetime) or clock.utcoffset() is None:
            raise ValueError
        if (type(value) is not dict or set(value) != _PLAN
                or not _integer(value['schema_version'], 1, 2)
                or value['data_origin'] not in ('synthetic', 'research')
                or type(value['model_version']) is not str
                or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['model_version'])
                or value['prediction_target'] not in ('new_join_total', 'remaining')
                or value['conditioning'] != ('new_join' if value['prediction_target'] == 'new_join_total'
                    else 'caller_supplied_conditional' if value['schema_version'] == 1
                    else 'call_not_observed_after_elapsed')
                or not _integer(value['ai_blend_ppm'], 0, PPM)):
            raise ValueError
        context = value['public_context']
        if (type(context) is not dict or set(context) != _CONTEXT
                or not _integer(context['schema_version'], 1, 1)
                or context['source'] != SOURCE
                or type(context['store_id']) is not str
                or not re.fullmatch('[1-9][0-9]{0,9}', context['store_id'])
                or int(context['store_id']) > 2**31-1
                or context['queue_type'] not in ('ordinary', 'reservation')
                or not _integer(context['observation_revision'], 0, 2**53-1)
                or not _integer(context['window_seconds'], 30, 3600)
                or not _integer(context['max_local_age_seconds'], 1, 3600)
                or context['source_freshness'] != 'unknown' or context['count_unit'] != 'unknown'
                or context['store_identity_verified'] is not False
                or type(context['collector_running']) is not bool
                or context['latest_queue_origin'] not in ('worker_commit', 'saved_history', 'unavailable')):
            raise ValueError
        cutoff, expiry = _time(context['as_of']), _time(context['expires_at'])
        if cutoff > clock or not cutoff < expiry <= cutoff+timedelta(seconds=300):
            raise ValueError
        context = {**context, 'as_of': _utc(cutoff), 'expires_at': _utc(expiry)}
        for key in ('latest_queue_received_at', 'latest_count_received_at'):
            if context[key] is not None:
                received = _time(context[key])
                if received > cutoff:
                    raise ValueError
                context[key] = _utc(received)
        features = context['features']
        if type(features) is not list or not 1 <= len(features) <= len(FEATURE_IDS):
            raise ValueError
        normalized, seen = [], set()
        for feature in features:
            if (type(feature) is not dict or set(feature) != {'feature_id', 'value', 'available_at'}
                    or type(feature['feature_id']) is not str or feature['feature_id'] not in FEATURE_IDS
                    or feature['feature_id'] in seen or feature['value'] is not None and
                    not _integer(feature['value'], 0, 2**31-1)):
                raise ValueError
            identifier, number = feature['feature_id'], feature['value']
            upper = (PPM if '_turnover_rank_' in identifier else
                96 if identifier.endswith(('_turnover_reference_windows', '_turnover_reference_days')) else
                context['window_seconds']*1000 if identifier.endswith('_observed_milliseconds') else 2**31-1)
            if number is not None and number > upper:
                raise ValueError
            available = _time(feature['available_at'])
            if not cutoff-timedelta(seconds=context['window_seconds']) <= available <= cutoff:
                raise ValueError
            seen.add(feature['feature_id'])
            normalized.append({**feature, 'available_at': _utc(available)})
        context['features'] = sorted(normalized, key=lambda f: f['feature_id'])
        fields = {f['feature_id']:f['value'] for f in normalized}
        for queue in ('ordinary','reservation'):
            names = [queue+'_turnover_'+name for name in
                ('rank_lower_ppm','rank_upper_ppm','reference_windows','reference_days')]
            if any(name in fields for name in names):
                if not all(name in fields for name in names):
                    raise ValueError
                a,b,n,days = [fields[name] for name in names]
                if not all(v is None for v in (a,b,n,days)) and (
                        any(v is None for v in (a,b,n,days)) or a>b or not 1<=days<=n):
                    raise ValueError
        candidates, seen = value['candidates'], set()
        if type(candidates) is not list or not 1 <= len(candidates) <= len(CANDIDATE_IDS):
            raise ValueError
        elapsed_values = set()
        for candidate in candidates:
            if (type(candidate) is not dict or set(candidate) not in
                    ({'candidate_id', 'atoms'}, {'candidate_id', 'interval_sample'},
                     {'candidate_id', 'conditional_interval_sample'})
                    or type(candidate['candidate_id']) is not str
                    or candidate['candidate_id'] not in CANDIDATE_IDS or candidate['candidate_id'] in seen):
                raise ValueError
            seen.add(candidate['candidate_id'])
            if 'interval_sample' in candidate or 'conditional_interval_sample' in candidate:
                direct = 'conditional_interval_sample' in candidate
                if value['schema_version'] != 2 or direct and value['prediction_target'] != 'remaining':
                    raise ValueError
                sample = candidate['conditional_interval_sample' if direct else 'interval_sample']
                elapsed_key = 'conditioned_elapsed_us' if direct else 'elapsed_us'
                if (type(sample) is not dict or set(sample) != {elapsed_key, 'intervals'}
                        or type(sample['intervals']) is not list or not 1 <= len(sample['intervals']) <= 128):
                    raise ValueError
                elapsed = sample[elapsed_key]
                if value['prediction_target'] == 'remaining':
                    if not _integer(elapsed, 0, MAX_WAIT_US):
                        raise ValueError
                    elapsed_values.add(elapsed)
                elif elapsed is not None:
                    raise ValueError
                pairs = set()
                total = definite = 0
                for row in sample['intervals']:
                    if (type(row) is not list or len(row) != 3
                            or not _integer(row[0], 0, MAX_WAIT_US)
                            or row[1] is not None and not _integer(row[1], row[0], MAX_WAIT_US)
                            or not _integer(row[2], 1, 10_000) or (row[0], row[1]) in pairs):
                        raise ValueError
                    pairs.add((row[0], row[1])); total += row[2]
                    definite += row[2] if elapsed is not None and row[0] > elapsed else 0
                if total > 10_000 or not direct and elapsed is not None and definite == 0:
                    raise ValueError
                continue
            if type(candidate['atoms']) is not list or not 1 <= len(candidate['atoms']) <= 32:
                raise ValueError
            for atom in candidate['atoms']:
                if (type(atom) is not dict or set(atom) != {'lower_us', 'upper_us', 'mass_ppm'}
                        or not _integer(atom['lower_us'], 0, MAX_WAIT_US)
                        or atom['upper_us'] is not None and not _integer(atom['upper_us'], atom['lower_us'], MAX_WAIT_US)
                        or not _integer(atom['mass_ppm'], 1, PPM)):
                    raise ValueError
            if sum(a['mass_ppm'] for a in candidate['atoms']) != PPM:
                raise ValueError
        if (len(elapsed_values) > 1 or value['schema_version'] == 2
                and value['prediction_target'] == 'remaining' and not elapsed_values):
            raise ValueError
        prior = _weights(value['prior_weights_ppm'], seen)
        safe = {**deepcopy(value), 'public_context': context, 'prior_weights_ppm': prior}
        for candidate in safe['candidates']:
            if 'interval_sample' in candidate or 'conditional_interval_sample' in candidate:
                candidate['conditional_interval_sample' if 'conditional_interval_sample' in candidate else 'interval_sample']['intervals'].sort(key=lambda row: (row[0], row[1] is None, row[1] or 0))
        if len(_canonical(safe).encode()) > MAX_BYTES:
            raise ValueError
        return safe
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise FusionError('fusion_invalid_plan') from None


def read_plan(path):
    try:
        return validate_plan(_json(_read_private_file(path)))
    except Exception:
        raise FusionError('fusion_invalid_input') from None


def read_advice(path):
    """Malformed optional advice is a fallback, not a failed baseline calculation."""
    try:
        body = _read_private_file(path)
        return _json(body)
    except Exception:
        raise FusionError('fusion_invalid_advice') from None


def context_from_history(history, *, queue_type, now=None, window_seconds=600,
                         max_local_age_seconds=360, ttl_seconds=60):
    """Public aggregates from the writer's bounded projection, never its SQLite.

    Comparison intervals must have both responses inside the requested window.
    Missing/latest failed/duplicate responses invalidate current queue evidence.
    Revision is process-scoped; the complete input hash also binds time/content.
    """
    try:
        clock = _clock() if now is None else now
        if (type(history) is not dict or not _integer(history.get('monitor_schema_version'), 1, 1)
                or history.get('source') != SOURCE or queue_type not in ('ordinary', 'reservation')
                or type(history.get('worker_alive')) is not bool
                or not _integer(window_seconds, 30, 3600)
                or not _integer(max_local_age_seconds, 1, 3600)
                or not _integer(ttl_seconds, 1, 300)
                or history.get('source_freshness') != 'unknown'
                or history.get('count_unit') != 'unknown'
                or history.get('response_store_identity_verified') is not False):
            raise ValueError
        store = history['requested_store_id']
        if type(store) is not str or not re.fullmatch('[1-9][0-9]{0,9}', store) or int(store) > 2**31-1:
            raise ValueError
        cutoff = _time(history['generated_at'])
        if cutoff > clock:
            raise ValueError
        start = cutoff-timedelta(seconds=window_seconds)
        points = history['points']
        evicted = history['evicted_points_this_process']
        if (type(points) is not list or len(points) > 360
                or not _integer(evicted, 0, 2**53-1-len(points))
                or type(history['retained_points']) is not int or history['retained_points'] != len(points)):
            raise ValueError
        previous, latest = None, {'groupqueues': None, 'storequeuecount': None}
        last_seen = {'groupqueues': None, 'storequeuecount': None}
        removed = {'ordinary': 0, 'reservation': 0}
        comparable, observed_us, gaps = 0, 0, []
        failures = {'groupqueues': 0, 'storequeuecount': 0}
        count_raw, queue_origin = None, 'unavailable'
        for point in points:
            if type(point) is not dict or type(point.get('queries')) is not dict:
                raise ValueError
            received = {}
            for name in latest:
                query = point['queries'][name]
                if (type(query) is not dict or type(query['attempted']) is not bool
                        or type(query['ok']) is not bool or query['ok'] and not query['attempted']):
                    raise ValueError
                at = None if query['received_at'] is None else _time(query['received_at'])
                begun = None if query['started_at'] is None else _time(query['started_at'])
                if (at is not None and (at > cutoff or begun is None or begun > at)
                        or begun is not None and begun > cutoff or query['ok'] and at is None):
                    raise ValueError
                received[name] = at
                latest[name] = at if query['ok'] else None
                if at is not None:
                    if last_seen[name] is not None and at <= last_seen[name]:
                        latest[name] = None
                    last_seen[name] = at if last_seen[name] is None else max(at, last_seen[name])
                if query['attempted'] and not query['ok'] and (at or begun) is not None and (at or begun) >= start:
                    failures[name] += 1
            comparison = point['display_comparison']
            current = latest['groupqueues']
            if type(comparison) is not dict:
                raise ValueError
            if previous is not None and current is not None:
                delta = (current-previous).total_seconds()
                if delta <= 0:
                    latest['groupqueues'] = None
                elif start <= previous <= current and comparison['state'] in ('gap', 'comparable_display_sets'):
                    gaps.append(delta)
                    if comparison['state'] == 'comparable_display_sets':
                        values = comparison['removed_labels']
                        if (type(values) is not dict or not _integer(values.get('storeQueue'), 0, 2**31-1)
                                or not _integer(values.get('reservationQueue'), 0, 2**31-1)):
                            raise ValueError
                        comparable += 1
                        span = current-previous
                        observed_us += (span.days*86400+span.seconds)*1_000_000+span.microseconds
                        removed['ordinary'] += values['storeQueue']
                        removed['reservation'] += values['reservationQueue']
            if comparison['state'] == 'time_order_or_duplicate':
                latest['groupqueues'] = None
            previous = current
            if point.get('origin') not in ('worker_commit', 'saved_history'):
                raise ValueError
            queue_origin = point['origin'] if latest['groupqueues'] is not None else 'unavailable'
            raw = point['reported_count_raw']
            if raw is not None and not _integer(raw, 0, 2**31-1):
                raise ValueError
            count_raw = raw if latest['storequeuecount'] is not None and latest['storequeuecount'] >= start else None
        values = {'ordinary_removed_labels': removed['ordinary'] if comparable else None,
                  'ordinary_comparable_pairs': comparable,
                  'reservation_removed_labels': removed['reservation'] if comparable else None,
                  'reservation_comparable_pairs': comparable, 'reported_count_raw': count_raw,
                  'ordinary_observed_milliseconds': observed_us//1000 if comparable else None,
                  'reservation_observed_milliseconds': observed_us//1000 if comparable else None,
                  'longest_gap_seconds': math.ceil(max(gaps)) if gaps else None,
                  'groupqueues_failures': failures['groupqueues'],
                  'storequeuecount_failures': failures['storequeuecount']}
        if any(value is not None and not _integer(value, 0, 2**31-1) for value in values.values()):
            raise ValueError
        return {'schema_version': 1, 'source': SOURCE, 'store_id': store, 'queue_type': queue_type,
            'observation_revision': len(points)+evicted, 'as_of': _utc(cutoff),
            'expires_at': _utc(cutoff+timedelta(seconds=ttl_seconds)),
            'window_seconds': window_seconds, 'max_local_age_seconds': max_local_age_seconds,
            'latest_queue_received_at': _utc(latest['groupqueues']) if latest['groupqueues'] is not None else None,
            'latest_count_received_at': _utc(latest['storequeuecount']) if latest['storequeuecount'] is not None else None,
            'features': [{'feature_id': key, 'value': value, 'available_at': _utc(cutoff)}
                         for key, value in sorted(values.items())],
            'source_freshness': 'unknown', 'count_unit': 'unknown', 'store_identity_verified': False,
            'collector_running': history['service_state'] == 'running' and history['worker_alive'],
            'latest_queue_origin': queue_origin}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise FusionError('fusion_invalid_history') from None


def public_request(plan, *, now=None):
    plan = validate_plan(plan, now=now)
    # No distribution atoms, personal queue number, issue time or arrival plan.
    public = {'schema_version': 1, 'policy': POLICY if plan['schema_version'] == 1 else EMPIRICAL_POLICY, 'model_version': plan['model_version'],
              'context': plan['public_context'], 'candidate_ids': sorted(plan['prior_weights_ppm']),
              'calendar_at_observation': date_features(plan['public_context']['as_of'],
                                                       as_of=plan['public_context']['as_of'])}
    return {**public, 'public_input_sha256': hashlib.sha256(_canonical(public).encode()).hexdigest()}


def _advice(value, request, *, now):
    if value is None:
        raise FusionError('fusion_advice_missing')
    try:
        if (type(value) is not dict or set(value) != _ADVICE or not _integer(value['schema_version'], 1, 1)
                or type(value['reason_code']) is not str or value['reason_code'] not in REASONS):
            raise ValueError
        context = request['context']
        if (value['public_input_sha256'] != request['public_input_sha256']
                or value['observation_revision'] != context['observation_revision']
                or type(value['observation_revision']) is not int
                or value['model_version'] != request['model_version']):
            raise FusionError('fusion_advice_input_mismatch')
        generated, expiry = _time(value['generated_at']), _time(value['expires_at'])
        if not _time(context['as_of']) <= generated <= now or not generated < expiry <= _time(context['expires_at']):
            raise FusionError('fusion_advice_clock_invalid')
        if now >= expiry:
            raise FusionError('fusion_advice_expired')
        if not context['collector_running']:
            raise FusionError('fusion_collector_not_running')
        if context['latest_queue_origin'] != 'worker_commit':
            raise FusionError('fusion_saved_history_only')
        queue = context['latest_queue_received_at']
        if queue is None or (now-_time(queue)).total_seconds() > context['max_local_age_seconds']:
            raise FusionError('fusion_stale_queue')
        known = {f['feature_id'] for f in context['features'] if f['value'] is not None}
        refs = value['feature_ids']
        if (type(refs) is not list or not 1 <= len(refs) <= len(FEATURE_IDS)
                or any(type(f) is not str for f in refs) or len(set(refs)) != len(refs)
                or not set(refs) <= known or value['reason_code'] == 'insufficient_evidence'):
            raise FusionError('fusion_advice_insufficient_evidence')
        return {'weights_ppm': _weights(value['weights_ppm'], request['candidate_ids']),
                'feature_ids': sorted(refs), 'reason_code': value['reason_code'],
                'generated_at': _utc(generated), 'expires_at': _utc(expiry)}
    except FusionError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise FusionError('fusion_invalid_advice') from None


def _quantile(atoms, q, endpoint):
    # Integer products keep both mixture and atom probability mass exact.
    total = sum(mass for atom, mass in atoms)
    ordered = sorted(((atom[endpoint], mass) for atom, mass in atoms if mass),
                     key=lambda item: (item[0] is None, item[0] or 0))
    cumulative = 0
    for point, mass in ordered:
        cumulative += mass
        if cumulative*PPM >= q*total:
            return point
    raise FusionError('fusion_operation_failed')


def _cdf_bounds(candidate, t):
    """Exact rational empirical CDF envelopes; t=0 may be a right-limit bound.

    Conditional intervals do not have a fixed number of survivors: dividing
    by the unconditioned sample size, or assigning fixed survivor masses, is
    incorrect when an interval straddles elapsed time.
    """
    if 'atoms' in candidate:
        rows = candidate['atoms']
        lower = sum(a['mass_ppm'] for a in rows if a['upper_us'] is not None and a['upper_us'] <= t)
        upper = sum(a['mass_ppm'] for a in rows if a['lower_us'] <= t)
        return Fraction(lower, PPM), Fraction(upper, PPM)
    direct = 'conditional_interval_sample' in candidate
    sample = candidate['conditional_interval_sample' if direct else 'interval_sample']
    rows, elapsed = sample['intervals'], None if direct else sample['elapsed_us']
    if elapsed is None:
        total = sum(n for _, _, n in rows)
        return (Fraction(sum(n for _, u, n in rows if u is not None and u <= t), total),
                Fraction(sum(n for l, _, n in rows if l <= t), total))
    target = elapsed+t
    must_finished = sum(n for l, u, n in rows if l > elapsed and u is not None and u <= target)
    could_later = sum(n for _, u, n in rows if u is None or u > target)
    could_finished = sum(n for l, u, n in rows if (u is None or u > elapsed) and l <= target)
    must_later = sum(n for l, _, n in rows if l > target)
    return Fraction(must_finished, must_finished+could_later), Fraction(could_finished, could_finished+must_later)


def _empirical_quantiles(candidates, effective):
    active = [c for c in candidates if effective[c['candidate_id']] > 0]
    points = {0}
    for candidate in active:
        if 'atoms' in candidate:
            for row in candidate['atoms']:
                points.add(row['lower_us'])
                if row['upper_us'] is not None:
                    points.add(row['upper_us'])
        else:
            direct = 'conditional_interval_sample' in candidate
            sample = candidate['conditional_interval_sample' if direct else 'interval_sample']
            elapsed = 0 if direct else sample['elapsed_us'] or 0
            for lower, upper, _ in sample['intervals']:
                points.add(max(0, lower-elapsed))
                if upper is not None:
                    points.add(max(0, upper-elapsed))
    points = sorted(points)
    cache = {}
    def bounds(index):
        if index not in cache:
            values = [_cdf_bounds(c, points[index]) for c in active]
            cache[index] = tuple(sum((Fraction(effective[c['candidate_id']], PPM**2)*v[side]
                for c, v in zip(active, values)), Fraction(0)) for side in (0, 1))
        return cache[index]
    def inverse(q, side):
        if bounds(len(points)-1)[side] < q:
            return None
        lo, hi = 0, len(points)-1
        while lo < hi:
            mid = (lo+hi)//2
            if bounds(mid)[side] >= q:
                hi = mid
            else:
                lo = mid+1
        return points[lo]
    return {name: {'lower_us': inverse(q, 1), 'upper_us': inverse(q, 0)}
            for name, q in (('p10', Fraction(1, 10)), ('p50', Fraction(1, 2)), ('p90', Fraction(9, 10)))}


def fuse(plan, *, advice=None, now=None, current_public_input_sha256=None,
         current_observation_revision=None):
    clock = _clock() if now is None else now
    plan = validate_plan(plan, now=clock)
    request = public_request(plan, now=clock)
    if ((current_public_input_sha256 is not None and
            current_public_input_sha256 != request['public_input_sha256'])
            or current_observation_revision is not None and
            (type(current_observation_revision) is not int or
             current_observation_revision != request['context']['observation_revision'])):
        # A newer input invalidates the entire old result, including fallback.
        raise FusionError('fusion_superseded_input')
    accepted, fallback = None, None
    try:
        accepted = _advice(advice, request, now=clock)
    except FusionError as error:
        fallback = error.error_code
    alpha = plan['ai_blend_ppm'] if accepted is not None else 0
    prior = plan['prior_weights_ppm']
    effective = {key: (PPM-alpha)*weight + alpha*accepted['weights_ppm'][key]
                 if accepted is not None else PPM*weight for key, weight in prior.items()}
    assert sum(effective.values()) == PPM**2
    empirical = any('interval_sample' in c or 'conditional_interval_sample' in c for c in plan['candidates'])
    atoms = [(a, effective[c['candidate_id']]*a['mass_ppm'])
             for c in plan['candidates'] if 'atoms' in c for a in c['atoms']]
    quantiles = _empirical_quantiles(plan['candidates'], effective) if empirical else {name: {'lower_us': _quantile(atoms, q, 'lower_us'),
                         'upper_us': _quantile(atoms, q, 'upper_us')}
                 for name, q in (('p10', 100_000), ('p50', 500_000), ('p90', 900_000))}
    return {'fusion_schema_version': 1, 'policy': request['policy'], 'plan': plan,
            'local_input_sha256': hashlib.sha256(_canonical(plan).encode()).hexdigest(),
            'public_request': request, 'computed_at': _utc(clock),
            'advice_accepted': accepted is not None, 'ai_numerical_influence_applied': alpha > 0,
            'advice_origin_basis': 'caller_supplied_not_provider_attested',
            'candidate_basis': 'caller_supplied_interval_samples_not_population_fit' if empirical else 'caller_supplied_distribution_supports_not_fitted_here',
            'ai_blend_ppm': alpha, 'advice': accepted, 'fallback_reason': fallback,
            'effective_weight_numerators': effective, 'effective_weight_denominator': PPM**2,
            'wait_quantile_envelopes_us': quantiles, 'quantile_method': 'inverse_rational_cdf_envelope_mixture' if empirical else 'inverse_mixture_cdf_interval_envelopes',
            'survival_conditioning_applied_here': any('interval_sample' in c and c['interval_sample']['elapsed_us'] is not None
                and effective[c['candidate_id']] > 0 for c in plan['candidates']),
            'direct_conditional_samples_present': any('conditional_interval_sample' in c for c in plan['candidates']),
            'unbounded_upper_support': any(v['upper_us'] is None for v in quantiles.values()),
            'current_input_guard_applied': current_public_input_sha256 is not None and current_observation_revision is not None,
            'input_currentness_verified': False, 'coverage_calibrated': False,
            'model_fitted_here': False, 'model_performance_verified': False,
            'verified_training_labels': 0, 'eta_available': False,
            'source_freshness': 'unknown', 'count_unit': 'unknown', 'store_identity_verified': False,
            'network_performed': False, 'provider_called': False, 'booking_or_cancellation_performed': False,
            'output_requires_private_handling': True}


def write_fusion(plan, *, destination, advice=None, advice_error=None):
    result = fuse(plan, advice=advice)
    if advice_error is not None:
        if advice_error != 'fusion_invalid_advice' or advice is not None:
            raise FusionError('fusion_invalid_input')
        result['fallback_reason'] = advice_error
    try:
        publication = _write_packet(_canonical(result).encode(), destination)
    except PacketError as error:
        raise FusionError('fusion_output_exists' if error.error_code == 'packet_output_exists'
                          else 'fusion_operation_failed', committed=error.committed) from None
    return {'fusion_schema_version': 1, 'artifact_written': True, 'committed': True,
            'durability_confirmed': publication['durability_confirmed'],
            'candidate_count': len(plan['candidates']), 'advice_accepted': result['advice_accepted'],
            'ai_numerical_influence_applied': result['ai_numerical_influence_applied'],
            'advice_origin_basis': result['advice_origin_basis'],
            'fallback_reason': result['fallback_reason'], 'network_performed': False,
            'provider_called': False, 'verified_training_labels': 0, 'eta_available': False,
            'output_requires_private_handling': True}
