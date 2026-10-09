"""Caller-observed event drafts linked to a private manually entered ticket.

Display membership, terminal declarations and model estimates are never event
times. Drafting does not receive, review, authenticate or train an outcome.
"""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .intake import _canonical
from .outcomes import _time, _utc, validate_episode, public_summary
from .packets import PacketError, _write_packet
from .tracking import TrackingSession, _now

EVENTS = ('checked_in', 'called', 'seated', 'no_show', 'cancelled', 'observation_ended')
_SCOPE = ('episode_id', 'store_id', 'api_profile', 'data_origin', 'queue_type',
          'party_size', 'table_type')


class ExperienceError(ValueError):
    def __init__(self, code='experience_invalid_input', *, committed=False):
        self.error_code = code if code in {'experience_invalid_input',
            'experience_issued_time_required', 'experience_scope_mismatch',
            'experience_output_exists', 'experience_write_failed'} else 'experience_invalid_input'
        self.committed = committed is True
        super().__init__(self.error_code)


def draft_experience(*, directory, event_type, lower=None, upper=None,
                     issued_lower=None, issued_upper=None, previous=None, now=None):
    """Build a private outcome revision from an explicit human event declaration.

    An omitted event interval means the caller reports the event at this local
    action time. Unknown issue time must be provided; it is never set to now.
    Previous drafts are claims; the intake ledger separately checks revisions.
    """
    clock = _now() if now is None else now
    try:
        if (event_type not in EVENTS or (lower is None) != (upper is None)
                or (issued_lower is None) != (issued_upper is None)):
            raise ExperienceError()
        with TrackingSession(directory) as session:
            ticket, _, _ = session.load(now=clock)
        scope = {k: ticket[k] for k in _SCOPE}
        if previous is not None:
            prior = validate_episode(previous, now=clock)
            if any(prior[k] != scope[k] for k in _SCOPE):
                raise ExperienceError('experience_scope_mismatch')
            events = deepcopy(prior['events'])
            issued = events[0]
            if issued_lower is not None and (
                    _time(issued_lower) != _time(issued['event_time_lower'])
                    or _time(issued_upper) != _time(issued['event_time_upper'])):
                raise ExperienceError('experience_scope_mismatch')
            if _time(prior['recorded_at']) > clock:
                raise ExperienceError()
            revision = prior['revision']+1
        else:
            if issued_lower is None:
                if ticket['issued_at'] is None:
                    raise ExperienceError('experience_issued_time_required')
                issued_lower = issued_upper = ticket['issued_at']
            lo, hi = _time(issued_lower), _time(issued_upper)
            if (not lo <= hi <= _time(ticket['created_at']) or ticket['issued_at'] is not None
                    and not lo <= _time(ticket['issued_at']) <= hi):
                raise ExperienceError('experience_scope_mismatch')
            events = [{'event_id': str(uuid4()), 'event_type': 'issued',
                'event_time_lower': _utc(lo), 'event_time_upper': _utc(hi),
                'observed_at': _utc(clock), 'evidence_kind': 'synthetic'
                    if ticket['data_origin'] == 'synthetic' else 'self_observation',
                'verification_status': 'unverified'}]
            revision = 1
        issue = events[0]
        issue_lo, issue_hi = _time(issue['event_time_lower']), _time(issue['event_time_upper'])
        if (not issue_lo <= issue_hi <= _time(ticket['created_at']) or ticket['issued_at'] is not None
                and not issue_lo <= _time(ticket['issued_at']) <= issue_hi):
            raise ExperienceError('experience_scope_mismatch')
        at_lower = clock if lower is None else _time(lower)
        at_upper = clock if upper is None else _time(upper)
        events.append({'event_id': str(uuid4()), 'event_type': event_type,
            'event_time_lower': _utc(at_lower), 'event_time_upper': _utc(at_upper),
            'observed_at': _utc(clock), 'evidence_kind': 'synthetic'
                if ticket['data_origin'] == 'synthetic' else 'self_observation',
            'verification_status': 'unverified'})
        return validate_episode({'schema_version': 1, **scope, 'revision': revision,
            'supersedes_revision': revision-1 if revision > 1 else None,
            'recorded_at': _utc(clock), 'events': events}, now=clock)
    except ExperienceError:
        raise
    except Exception:
        raise ExperienceError() from None


def write_experience(*, destination, **options):
    episode = draft_experience(**options)
    try:
        if Path(destination).parent.resolve() == Path(options['directory']).resolve():
            raise ExperienceError('experience_scope_mismatch')
        result = _write_packet(_canonical(episode).encode(), destination)
    except ExperienceError:
        raise
    except PacketError as error:
        raise ExperienceError('experience_output_exists' if error.error_code == 'packet_output_exists'
            else 'experience_write_failed', committed=error.committed) from None
    except (OSError, TypeError, ValueError, RuntimeError):
        raise ExperienceError('experience_write_failed') from None
    return {**public_summary(episode), **result, 'draft_written': True,
        'output_requires_private_handling': True, 'intake_received': False,
        'review_accepted': False, 'verified_training_labels': 0, 'eta_available': False,
        'provider_called': False, 'business_writes': 0, 'notification_sent': False}
