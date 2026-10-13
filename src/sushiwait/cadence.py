"""Continuous meal-peak sampling rate; pure arithmetic, no network or clock IO.

One sample is due when accumulated sampling phase reaches one. The rate uses
a cubic smoothstep on each 30-minute shoulder, so both rate and its derivative
join continuously. Sampling events themselves are necessarily discrete.
"""
from datetime import datetime, timedelta, timezone


POLICY = 'meal-peaks-v1'
CHINA = timezone(timedelta(hours=8))
BASE_SECONDS = 600
PEAK_SECONDS = 180
SHOULDER_SECONDS = 1800
PEAKS = ((11.5 * 3600, 13.5 * 3600), (18 * 3600, 20 * 3600))


def description(policy):
    if type(policy) is not str or policy != POLICY:
        raise ValueError('invalid_cadence_policy')
    return {'id': POLICY, 'timezone': 'Asia/Shanghai',
            'base_seconds': BASE_SECONDS, 'peak_seconds': PEAK_SECONDS,
            'transition_seconds': SHOULDER_SECONDS,
            'peaks': [{'start': '11:30', 'end': '13:30'},
                      {'start': '18:00', 'end': '20:00'}],
            'method': 'integrated_smoothstep_rate'}


def _local(at):
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('invalid_cadence_clock')
    return at.astimezone(CHINA)


def _smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _primitive(x):
    """Integral of clamped smoothstep from zero to x."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return x - 0.5
    return x ** 3 - 0.5 * x ** 4


def _seconds(at):
    return (at.hour * 3600 + at.minute * 60 + at.second + at.microsecond / 1e6)


def interval_at(at, policy=POLICY):
    """Instantaneous period, continuously varying between 180 and 600 seconds."""
    description(policy)
    second = _seconds(_local(at))
    weight = 0.0
    for start, end in PEAKS:
        weight += (_smooth((second - start + SHOULDER_SECONDS) / SHOULDER_SECONDS)
                   - _smooth((second - end) / SHOULDER_SECONDS))
    rate = 1 / BASE_SECONDS + weight * (1 / PEAK_SECONDS - 1 / BASE_SECONDS)
    return 1 / rate


def _day_phase(second):
    phase = second / BASE_SECONDS
    for start, end in PEAKS:
        phase += ((1 / PEAK_SECONDS - 1 / BASE_SECONDS) * SHOULDER_SECONDS
                  * (_primitive((second - start + SHOULDER_SECONDS) / SHOULDER_SECONDS)
                     - _primitive((second - end) / SHOULDER_SECONDS)))
    return phase


def phase_between(start, end, policy=POLICY):
    description(policy)
    start, end = _local(start), _local(end)
    if not 0 <= (end - start).total_seconds() <= 7 * 86400:
        raise ValueError('invalid_cadence_span')
    days = (end.date() - start.date()).days
    return days * _day_phase(86400) + _day_phase(_seconds(end)) - _day_phase(_seconds(start))


def next_sample_at(start, policy=POLICY):
    """One phase after an actual round anchor, never backfill missed slots."""
    description(policy)
    _local(start)
    low, high = float(PEAK_SECONDS), float(BASE_SECONDS)
    # Keep the peak plateau exactly 180 seconds at datetime's microsecond
    # precision; the binary-search upper bound would otherwise round to 181
    # when the public polling delay is rounded up to a whole second.
    if phase_between(start, start + timedelta(seconds=low), policy) >= 1 - 1e-12:
        return start + timedelta(seconds=low)
    for _ in range(40):
        middle = (low + high) / 2
        if phase_between(start, start + timedelta(seconds=middle), policy) < 1:
            low = middle
        else:
            high = middle
    # The conservative side of the bracket prevents an early sample.
    return start + timedelta(seconds=high)


def period_after(start, policy=POLICY):
    return (next_sample_at(start, policy) - start).total_seconds()


def planned_rounds(start, end, policy=POLICY, *, closing_margin=16):
    """No-IO demand estimate; excludes source latency, failures and outages."""
    _local(start); _local(end)
    seconds = (end - start).total_seconds()
    if not 0 < seconds <= 41400 or type(closing_margin) is not int or not 0 <= closing_margin < 60:
        raise ValueError('invalid_cadence_span')
    at, rounds = start, 0
    while at < end - timedelta(seconds=closing_margin):
        rounds += 1
        at = next_sample_at(at, policy)
    return rounds
