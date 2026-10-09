"""Prospective empirical ranks for public display turnover, never no-show rates.

Reference windows do not overlap. Endpoint failures, run boundaries, gaps and
late local receipts retain the feature export's exclusion rules. Calendar and
time-of-day fallback is explicit; ranks are descriptive, not calibrated risks.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib

from .calendar import date_features
from .credentials import _read_private_file
from .features import _history, _queue_features
from .fusion import validate_plan as validate_fusion_plan, PPM
from .intake import _canonical
from .outcomes import _json, _time, _utc
from .packets import _write_packet
from .remote import SOURCE, _id
from .remotesignals import _Stream
from .queuebinding import field_for_queue, require_bound_context

POLICY = 'dated_nonoverlapping_display_turnover_ranks_v2'
MAX_WINDOWS = 96
_FIELDS = {'trend_profile_schema_version', 'policy', 'source', 'store_id', 'created_at',
    'history_cutoff', 'window_seconds', 'max_gap_seconds', 'minimum_pairs',
    'minimum_coverage_ppm', 'samples', 'profile_sha256', 'availability_basis'}
_GROUPS = (('day_type_weekday_hour_month', ('day_type','weekday','hour','month')),
    ('day_type_weekday_hour_season', ('day_type','weekday','hour','season')),
    ('day_type_hour_season', ('day_type','hour','season')),
    ('day_type_hour', ('day_type','hour')), ('day_type', ('day_type',)))
_SELECTION = {'policy','archive_sha256','archive_revision','archive_imports_known',
    'archive_rows_known','reference_for','cadence','matched_group','matching_windows',
    'matching_days','selected_windows','selected_days','maximum_windows','maximum_per_day',
    'minimum_windows','minimum_days'}


class TrendProfileError(ValueError):
    def __init__(self, code='trend_profile_invalid'):
        self.error_code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc)


def _int(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def _digest(profile):
    return hashlib.sha256(_canonical({k: v for k, v in profile.items()
        if k != 'profile_sha256'}).encode()).hexdigest()


def validate_profile(value, *, now=None):
    try:
        now = _now() if now is None else now
        if (type(value) is not dict or set(value) != _FIELDS|(
                    {'selection'} if value.get('trend_profile_schema_version') == 2 else set())
                or not _int(value['trend_profile_schema_version'], 1, 2)
                or value['policy'] != POLICY or value['source'] != SOURCE
                or value['availability_basis'] not in ('local_first_receipt', 'sealed_response_reconstruction')
                or not _int(value['window_seconds'], 120, 3600)
                or not _int(value['max_gap_seconds'], 1, 3600)
                or not _int(value['minimum_pairs'], 1, 128)
                or not _int(value['minimum_coverage_ppm'], 1, PPM)
                or type(value['samples']) is not list or len(value['samples']) > MAX_WINDOWS
                or _time(value['history_cutoff']) > _time(value['created_at'])
                or _time(value['created_at']) > now):
            raise ValueError
        _id(value['store_id'])
        previous = None
        window = value['window_seconds']
        for row in value['samples']:
            if type(row) is not list or len(row) != 5:
                raise ValueError
            at = _time(row[0])
            if (at > _time(value['history_cutoff']) or at.microsecond
                    or int(at.timestamp()) % window != 0
                    or previous is not None and (at-previous).total_seconds() < window
                    or not _int(row[1], value['minimum_pairs'], 10_000)
                    or not _int(row[2], 1, window*1000)
                    or row[2]*PPM < value['minimum_coverage_ppm']*window*1000
                    or any(not _int(v, 0, 2**31-1) for v in row[3:])):
                raise ValueError
            previous = at
        if value['trend_profile_schema_version'] == 2:
            _validate_selection(value)
        if value['profile_sha256'] != _digest(value) or len(_canonical(value).encode()) > 16_384:
            raise ValueError
        return deepcopy(value)
    except Exception:
        raise TrendProfileError() from None


def _validate_selection(value):
    meta = value['selection']
    if (type(meta) is not dict or set(meta) != _SELECTION
            or meta['policy'] != 'complete_archive_date_balanced_v1'
            or type(meta['archive_sha256']) is not str or len(meta['archive_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in meta['archive_sha256'])
            or not _int(meta['archive_revision'], 1, 2**31-1)
            or not _int(meta['archive_imports_known'], 0, 512)
            or not _int(meta['archive_rows_known'], 0, 24576)
            or _time(meta['reference_for']) != _time(value['history_cutoff'])
            or not _int(meta['maximum_windows'], 2, MAX_WINDOWS)
            or not _int(meta['maximum_per_day'], 1, MAX_WINDOWS)
            or not _int(meta['minimum_windows'], 2, meta['maximum_windows'])
            or not _int(meta['minimum_days'], 1, meta['maximum_windows'])
            or not _int(meta['matching_windows'], 0, meta['archive_rows_known'])
            or not _int(meta['matching_days'], 0, meta['matching_windows'])
            or not _int(meta['selected_windows'], 0, meta['maximum_windows'])
            or meta['selected_windows'] != len(value['samples'])
            or not _int(meta['selected_days'], 0, meta['selected_windows'])):
        raise ValueError
    cadence = meta['cadence']
    if cadence is not None and (type(cadence) is not list or len(cadence) != 2
            or not _int(cadence[0], 1, value['window_seconds']*1000)
            or not _int(cadence[1], 1, 10000)):
        raise ValueError
    counts = {}
    at = _time(meta['reference_for'])
    current = _calendar(at.isoformat(), value['created_at'])
    keys = dict(_GROUPS).get(meta['matched_group'])
    for row in value['samples']:
        cal = _calendar(row[0], value['created_at'])
        counts[cal['date']] = counts.get(cal['date'], 0)+1
        if (keys is None or cadence is None or current['day_type'] == 'unknown'
                or _time(row[0]) > at-timedelta(seconds=value['window_seconds'])
                or cal['holiday_name'] != current['holiday_name']
                or any(current[key] is None or cal[key] != current[key] for key in keys)
                or not Fraction(4,5) <= Fraction(row[2],row[1])/Fraction(*cadence) <= Fraction(5,4)):
            raise ValueError
    if (len(counts) != meta['selected_days'] or any(n>meta['maximum_per_day'] for n in counts.values())
            or meta['matching_windows'] < meta['selected_windows']
            or meta['matching_days'] < meta['selected_days']
            or keys is None and (meta['matched_group'] is not None or meta['selected_windows']
                or meta['matching_windows'] or meta['matching_days'])
            or keys is not None and (meta['selected_windows'] < meta['minimum_windows']
                or meta['selected_days'] < meta['minimum_days'])):
        raise ValueError


def read_profile(path, *, now=None):
    try:
        return validate_profile(_json(_read_private_file(path)), now=now)
    except Exception:
        raise TrendProfileError('trend_profile_file_invalid') from None


def build_profile(remote, store_id, *, as_of, now=None, window_seconds=1800,
                  max_gap_seconds=360, minimum_pairs=2, minimum_coverage_ppm=500_000,
                  max_observations=10_000, availability_basis='local_first_receipt'):
    """Read one explicitly idle database, complete scan; never truncate history."""
    try:
        clock = _now() if now is None else now
        _id(store_id)
        value = {'trend_profile_schema_version': 1, 'policy': POLICY, 'source': SOURCE,
            'store_id': store_id, 'created_at': _utc(clock), 'history_cutoff': _utc(_time(as_of)),
            'window_seconds': window_seconds, 'max_gap_seconds': max_gap_seconds,
            'minimum_pairs': minimum_pairs, 'minimum_coverage_ppm': minimum_coverage_ppm,
            'availability_basis': availability_basis, 'samples': []}
        value['profile_sha256'] = _digest(value)
        validate_profile(value, now=clock)
        history = _history(remote, now=clock, max_observations=max_observations)
        cutoff = _time(as_of)
        available = [completed for _, record, completed, receipt in history
            if record['requested_store_id'] == store_id and completed <= cutoff
            and (availability_basis == 'sealed_response_reconstruction' or
                 receipt is not None and receipt <= cutoff)]
        if available:
            first = (int(min(available).timestamp())//window_seconds+1)*window_seconds
            last = int(max(available).timestamp())//window_seconds*window_seconds
            if last >= first and (last-first)//window_seconds+1 > MAX_WINDOWS:
                raise TrendProfileError('trend_profile_window_limit')
            for end in range(first, last+1, window_seconds):
                at = datetime.fromtimestamp(end, timezone.utc)
                if availability_basis == 'local_first_receipt':
                    feature, _ = _queue_features(history, store_id=store_id, at=at,
                        window_seconds=window_seconds, max_gap_seconds=max_gap_seconds)
                else:
                    # These responses are known when the sealed file is read
                    # now. Never assign a fabricated historical receipt time.
                    start = at-timedelta(seconds=window_seconds)
                    stream = _Stream('groupqueues', start, start+timedelta(seconds=window_seconds/2), at, max_gap_seconds)
                    for run, record, completed, _ in history:
                        if record['requested_store_id'] == store_id and completed <= at:
                            stream.add(record['queries']['groupqueues'], run, completed)
                    summary = stream.finish(window_seconds, False)
                    feature = {'queue_availability':summary['availability'],
                        'display_window_statistics':summary['queues']}
                ordinary = feature['display_window_statistics'][field_for_queue('ordinary')]['whole']
                reservation = feature['display_window_statistics']['reservationQueue']['whole']
                milliseconds = int(ordinary['observed_seconds']*1000)
                pairs = ordinary['comparable_pairs']
                if (feature['queue_availability'] != 'recent_responses' or pairs < minimum_pairs
                        or milliseconds*PPM < minimum_coverage_ppm*window_seconds*1000):
                    continue
                value['samples'].append([_utc(at), pairs, milliseconds,
                    ordinary['removed_labels'], reservation['removed_labels']])
        value['profile_sha256'] = _digest(value)
        return validate_profile(value, now=clock)
    except TrendProfileError:
        raise
    except Exception:
        raise TrendProfileError('trend_profile_build_failed') from None


def write_profile(profile, destination):
    profile = validate_profile(profile)
    result = _write_packet(_canonical(profile).encode(), destination)
    return {'artifact_written': True, 'committed': True,
        'durability_confirmed': result['durability_confirmed'],
        'reference_windows': len(profile['samples']), 'network_performed': False,
        'availability_basis':profile['availability_basis'], 'historical_availability_verified':False,
        'no_show_rate_estimated': False, 'eta_available': False}


def _context(value, now):
    plan = {'schema_version': 1, 'data_origin': 'research', 'model_version': 'trend-profile-v1',
        'prediction_target': 'new_join_total', 'conditioning': 'new_join',
        'public_context': value, 'candidates': [{'candidate_id': 'history',
            'atoms': [{'lower_us': 0, 'upper_us': 0, 'mass_ppm': PPM}]}],
        'prior_weights_ppm': {'history': PPM}, 'ai_blend_ppm': 0}
    return require_bound_context(validate_fusion_plan(plan, now=now)['public_context'])


def _calendar(at, as_of):
    value = date_features(at, as_of=as_of)
    return {'date': value['local_date'], 'weekday': value['iso_weekday'],
        'hour': value['minute_of_day']//60, 'month': value['month'],
        'season': value['calendar_season'], 'day_type': value['date_type'],
        'holiday_name':value['holiday']['name'] if value['holiday'] else None}


def score_context(profile, context, *, now=None, minimum_windows=8, minimum_days=2):
    """Rank a current covered rate against cutoff-known, earlier whole windows."""
    try:
        clock = _now() if now is None else now
        profile = validate_profile(profile, now=clock)
        context = _context(context, clock)
        if (profile['store_id'] != context['store_id']
                or profile['window_seconds'] != context['window_seconds']
                or _time(profile['created_at']) > _time(context['as_of'])
                or not _int(minimum_windows, 2, MAX_WINDOWS)
                or not _int(minimum_days, 1, MAX_WINDOWS)):
            raise ValueError
        result = {'trend_score_schema_version': 1, 'profile_sha256': profile['profile_sha256'],
            'reference_windows': 0, 'reference_days': 0, 'matched_group': None,
            'rank_lower_ppm': None, 'rank_upper_ppm': None,
            'display_turnover_band': 'insufficient_evidence', 'unavailable_reason': None,
            'no_show_rate': None, 'risk_probability_calibrated': False,
            'historical_availability_verified':False,'reference_windows_independent':False,
            'availability_basis':profile['availability_basis'],
            'source_freshness': 'unknown', 'eta_available': False, 'network_performed': False}
        features = {f['feature_id']: f['value'] for f in context['features']}
        prefix = context['queue_type']
        removed = features.get(prefix+'_removed_labels')
        duration = features.get(prefix+'_observed_milliseconds')
        pairs = features.get(prefix+'_comparable_pairs')
        gap = features.get('longest_gap_seconds')
        if (not context['collector_running'] or context['latest_queue_origin'] != 'worker_commit'
                or context['latest_queue_received_at'] is None
                or (clock-_time(context['latest_queue_received_at'])).total_seconds() > context['max_local_age_seconds']
                or clock >= _time(context['expires_at']) or removed is None or duration is None
                or duration <= 0 or pairs is None or pairs < profile['minimum_pairs']
                or gap is not None and gap > profile['max_gap_seconds']
                or duration*PPM < profile['minimum_coverage_ppm']*context['window_seconds']*1000):
            return {**result, 'unavailable_reason': 'current_window_unavailable_or_sparse'}
        current = _calendar(context['as_of'], context['as_of'])
        if current['day_type'] == 'unknown':
            return {**result, 'unavailable_reason': 'calendar_type_unknown'}
        if profile['trend_profile_schema_version'] == 2:
            selection = profile['selection']
            keys = dict(_GROUPS).get(selection['matched_group'])
            target = _calendar(selection['reference_for'], context['as_of'])
            if keys is not None and (target['holiday_name'] != current['holiday_name']
                    or any(target[key] != current[key] for key in keys)):
                return {**result, 'unavailable_reason': 'reference_target_changed'}
        start = _time(context['as_of'])-timedelta(seconds=context['window_seconds'])
        cadence = Fraction(duration, pairs)
        rows = [(row, _calendar(row[0], context['as_of'])) for row in profile['samples']
            if _time(row[0]) <= start and Fraction(4,5) <= Fraction(row[2],row[1])/cadence <= Fraction(5,4)]
        for name, keys in _GROUPS:
            selected = [(row, calendar) for row, calendar in rows
                if calendar['holiday_name'] == current['holiday_name']
                and all(current[key] is not None and calendar[key] == current[key] for key in keys)]
            days = len({calendar['date'] for _, calendar in selected})
            if len(selected) >= minimum_windows and days >= minimum_days:
                break
        else:
            return {**result, 'unavailable_reason': 'reference_dates_or_windows_insufficient'}
        rate = Fraction(removed, duration)
        column = 3 if prefix == 'ordinary' else 4
        rates = [Fraction(row[column], row[2]) for row, _ in selected]
        lower = sum(other < rate for other in rates)*PPM//len(rates)
        upper = (sum(other <= rate for other in rates)*PPM+len(rates)-1)//len(rates)
        return {**result, 'reference_windows': len(rates), 'reference_days': days, 'matched_group': name,
            'rank_lower_ppm': lower, 'rank_upper_ppm': upper,
            'display_turnover_band': 'higher_than_reference' if lower >= 900_000 else
                'lower_than_reference' if upper <= 100_000 else 'within_or_tied_reference'}
    except TrendProfileError:
        raise
    except Exception:
        raise TrendProfileError('trend_profile_score_failed') from None


def enrich_context(profile, context, *, now=None, minimum_windows=8, minimum_days=2):
    result = score_context(profile, context, now=now,
        minimum_windows=minimum_windows, minimum_days=minimum_days)
    safe = _context(context, _now() if now is None else now)
    prefix = safe['queue_type']+'_turnover_'
    ids = {prefix+name: result[key] for name, key in
        [('rank_lower_ppm','rank_lower_ppm'), ('rank_upper_ppm','rank_upper_ppm'),
         ('reference_windows','reference_windows'), ('reference_days','reference_days')]}
    # Missing evidence remains null, not a fabricated percentile or zero prior.
    if result['unavailable_reason'] is not None:
        ids = {key: None for key in ids}
    safe['features'] = [f for f in safe['features'] if f['feature_id'] not in ids]+[
        {'feature_id': key, 'value': value, 'available_at': safe['as_of']} for key, value in ids.items()]
    return _context(safe, _now() if now is None else now), result
