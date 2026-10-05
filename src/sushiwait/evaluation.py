"""Arithmetic for interval-observed calls, without forecasting or certifying evidence."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from uuid import UUID

import json
from pathlib import Path

from .calendar import CalendarError, _instant
from .credentials import CredentialError, _read_private_file

_FIELDS = frozenset({'prediction_id', 'prediction_made_at', 'point_call_at',
                     'predicted_call_lower', 'predicted_call_upper',
                     'observed_call_lower', 'observed_call_upper', 'observation_received_at'})
_TIME_FIELDS = _FIELDS - {'prediction_id'}


class EvaluationError(ValueError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


def _microseconds(value: timedelta) -> int:
    return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds


def _record(value: dict, cutoff):
    if type(value) is not dict or set(value) != _FIELDS:
        raise EvaluationError('evaluation_invalid_record')
    identifier = value['prediction_id']
    try:
        identity = UUID(identifier) if type(identifier) is str and len(identifier) == 36 else None
        if identity is None or identity.version != 4 or str(identity) != identifier:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError('evaluation_invalid_identifier') from None
    try:
        times = {key: _instant(value[key]) for key in _TIME_FIELDS}
    except CalendarError:
        raise EvaluationError('evaluation_invalid_time') from None
    made, point, p, q, l, u, received = (times[key] for key in
        ('prediction_made_at', 'point_call_at', 'predicted_call_lower',
         'predicted_call_upper', 'observed_call_lower', 'observed_call_upper',
         'observation_received_at'))
    if not (made <= p <= point <= q and made <= l <= u <= received <= cutoff):
        raise EvaluationError('evaluation_time_order')
    distance = l-point if point < l else point-u if point > u else timedelta()
    maximum = max(abs(point-l), abs(point-u))
    return identifier, {
        'absolute_error_lower_us': _microseconds(distance),
        'absolute_error_upper_us': _microseconds(maximum),
        'predicted_width_us': _microseconds(q-p),
        'observed_width_us': _microseconds(u-l),
        'coverage_confirmed': p <= l and u <= q,
        'coverage_possible': not (u < p or l > q),
        'exact_label': l == u,
    }


def evaluate_intervals(records: list[dict], *, as_of: str, data_origin: str) -> dict:
    """Score recorded claims; does not establish real model performance.

    Inputs contain private precise times. Only bounded aggregate arithmetic is
    returned; the aggregate is still private, particularly for small cohorts.
    Synthetic/self-reported records are explicitly separate. No label review,
    censoring analysis, training split or forecast-log authenticity is inferred.
    """
    if type(records) is not list or not 0 <= len(records) <= 10_000:
        raise EvaluationError('evaluation_invalid_bounds')
    if type(data_origin) is not str or data_origin not in {'synthetic', 'self_reported'}:
        raise EvaluationError('evaluation_invalid_origin')
    try:
        cutoff = _instant(as_of)
    except CalendarError:
        raise EvaluationError('evaluation_invalid_time') from None
    totals, counts, seen = Counter(), Counter(), set()
    for value in records:
        identifier, score = _record(value, cutoff)
        if identifier in seen:
            raise EvaluationError('evaluation_duplicate_prediction')
        seen.add(identifier)
        for key in ('absolute_error_lower_us','absolute_error_upper_us','predicted_width_us','observed_width_us'):
            totals[key] += score[key]
        for key in ('coverage_confirmed','coverage_possible','exact_label'):
            counts[key] += int(score[key])
    n = len(records)
    def seconds(key):
        return round(totals[key] / (n * 1_000_000), 6) if n else None
    return {
        'evaluation_schema_version': 1, 'data_origin': data_origin,
        'interval_labels_evaluated': n, 'exact_labels': counts['exact_label'],
        'uncertain_interval_labels': n-counts['exact_label'],
        'mean_absolute_error_seconds': {
            'lower': seconds('absolute_error_lower_us'),
            'upper': seconds('absolute_error_upper_us'),
        },
        'coverage': {
            'confirmed_labels': counts['coverage_confirmed'],
            'possible_labels': counts['coverage_possible'],
            'lower': counts['coverage_confirmed']/n if n else None,
            'upper': counts['coverage_possible']/n if n else None,
        },
        'mean_prediction_interval_width_seconds': seconds('predicted_width_us'),
        'mean_observed_interval_width_seconds': seconds('observed_width_us'),
        'scores_are_recorded_claims': True, 'authenticity_verified': False,
        'forecast_log_verified': False, 'model_performance_verified': False,
        'verified_training_labels': 0, 'censored_records_included': False,
        'output_requires_private_handling': True, 'eta_available': False,
        'network_performed': False,
    }


def evaluate_document(document: dict) -> dict:
    if (type(document) is not dict
            or set(document) != {'schema_version', 'as_of', 'data_origin', 'records'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1):
        raise EvaluationError('evaluation_invalid_document')
    return evaluate_intervals(document['records'], as_of=document['as_of'],
                              data_origin=document['data_origin'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError('evaluation_invalid_input_file')
        result[key] = value
    return result


def read_evaluation_document(path: str | Path) -> dict:
    """Read one explicit private 16KiB JSON input, never an auth configuration."""
    try:
        value = json.loads(_read_private_file(path), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError
        return value
    except (CredentialError, ValueError, TypeError, RecursionError, UnicodeError):
        raise EvaluationError('evaluation_invalid_input_file') from None
