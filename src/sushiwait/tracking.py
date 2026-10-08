"""Private manual-ticket episodes, committed observations and atomic publication.

Only caller declarations identify a ticket or end its episode. Public display
membership is evidence of display, never proof of a call, a miss or a position.
No collector, provider, notification or restaurant operation is started here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets

from .baseline import MAX_WAIT_US, _us
from .capture import CaptureError, _open_parent, _check_parent
from .credentials import _read_private_file, _private_file, _identity
from .fusion import FusionError, fuse, validate_plan as validate_fusion
from .historyfusion import build_history_fusion
from .intake import _canonical
from .monitoring import polling_policy
from .outcomes import _json, _time, _utc, _uuid
from .remote import SOURCE, QUEUE_NAMES, _LABEL, _payload

POLICY = 'private_manual_ticket_observation_publication_v1'
_TICKET = {'schema_version', 'episode_id', 'data_origin', 'api_profile', 'store_id',
    'queue_type', 'number', 'issued_at', 'party_size', 'table_type', 'checked_in',
    'created_at', 'deadline_at', 'desired_arrival_at', 'call_offset_minutes',
    'minimum_samples', 'max_updates'}
_CODES = {'tracking_invalid_ticket', 'tracking_invalid_input', 'tracking_scope_mismatch',
    'tracking_invalid_view', 'tracking_observation_regressed', 'tracking_observation_conflict',
    'tracking_invalid_ledger', 'tracking_ledger_busy', 'tracking_write_failed',
    'tracking_already_initialized', 'tracking_update_limit', 'tracking_deadline',
    'tracking_terminal', 'tracking_superseded', 'tracking_prediction_conflict',
    'tracking_invalid_prediction', 'tracking_unavailable_version'}


class TrackingError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if type(code) is str and code in _CODES else 'tracking_invalid_input'
        self.committed = committed is True
        super().__init__(self.error_code)


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _integer(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def read_document(path):
    try:
        return _json(_read_private_file(path))
    except Exception:
        raise TrackingError('tracking_invalid_input') from None


def validate_ticket(value, *, now=None):
    try:
        clock = _now() if now is None else now
        if (type(value) is not dict or set(value) != _TICKET
                or not _integer(value['schema_version'], 1, 1)
                or value['data_origin'] not in ('synthetic', 'self_reported')
                or value['api_profile'] not in ('legacy', 'miniapp_gateway')
                or type(value['store_id']) is not str
                or not re.fullmatch('[1-9][0-9]{0,9}', value['store_id'])
                or int(value['store_id']) > 2**31-1
                or value['queue_type'] not in ('ordinary', 'reservation')
                or type(value['number']) is not str or not _LABEL.fullmatch(value['number'])
                or value['party_size'] is not None and not _integer(value['party_size'], 1, 1000)
                or value['table_type'] not in ('booth', 'counter', 'either', 'unknown')
                or value['checked_in'] is not None and type(value['checked_in']) is not bool
                or not _integer(value['call_offset_minutes'], -120, 120)
                or not _integer(value['minimum_samples'], 1, 1000)
                or not _integer(value['max_updates'], 1, 2000)):
            raise ValueError
        _uuid(value['episode_id'])
        created, deadline = _time(value['created_at']), _time(value['deadline_at'])
        if not created <= clock or not created < deadline <= created+timedelta(days=1):
            raise ValueError
        safe = {**value, 'created_at': _utc(created), 'deadline_at': _utc(deadline)}
        if value['issued_at'] is not None:
            issued = _time(value['issued_at'])
            if not 0 <= _us(created-issued) <= MAX_WAIT_US:
                raise ValueError
            safe['issued_at'] = _utc(issued)
        if value['desired_arrival_at'] is not None:
            arrival = _time(value['desired_arrival_at'])
            if not created <= arrival <= deadline:
                raise ValueError
            safe['desired_arrival_at'] = _utc(arrival)
        if len(_canonical(safe).encode()) > 16_384:
            raise ValueError
        return safe
    except Exception:
        raise TrackingError('tracking_invalid_ticket') from None


def _context(value, *, now):
    # Reuse the exact public whitelist without putting personal details in it.
    try:
        plan = {'schema_version': 1, 'data_origin': 'research', 'model_version': 'tracking-validation-v1',
            'prediction_target': 'new_join_total', 'conditioning': 'new_join', 'ai_blend_ppm': 0,
            'public_context': value, 'candidates': [{'candidate_id': 'history',
                'atoms': [{'lower_us': 0, 'upper_us': 0, 'mass_ppm': 1_000_000}]}],
            'prior_weights_ppm': {'history': 1_000_000}}
        return validate_fusion(plan, now=now)['public_context']
    except FusionError:
        raise TrackingError('tracking_invalid_view') from None


def normalize_observation(ticket, view, context, *, as_of, now=None):
    """Bind separately read writer projections; mismatched current receipts fail.

    A retained success after a failed query is explicitly unavailable. A read
    time is not a new queue response. The writer's revision is process-scoped.
    """
    try:
        clock = _now() if now is None else now
        ticket = validate_ticket(ticket, now=clock)
        at = _time(as_of)
        context = _context(context, now=clock)
        if (not _time(ticket['created_at']) <= at <= clock
                or at != _time(context['as_of'])
                or context['store_id'] != ticket['store_id']
                or context['queue_type'] != ticket['queue_type']):
            raise TrackingError('tracking_scope_mismatch')
        if (type(view) is not dict or not _integer(view.get('view_schema_version'), 1, 1)
                or view.get('source') != SOURCE or view.get('requested_store_id') != ticket['store_id']
                or type(view.get('worker_alive')) is not bool
                or view.get('source_freshness') != 'unknown'
                or view.get('response_store_identity_verified') is not False
                or view.get('source_update_time_verified') is not False
                or view.get('atomic_snapshot') is not False
                or type(view.get('fields')) is not dict):
            raise TrackingError('tracking_invalid_view')
        field = view['fields']['groupqueues']
        if (type(field) is not dict or field.get('state') not in ('recent_response', 'unavailable',
                'saved_history', 'last_known_only', 'stale_response', 'clock_invalid')):
            raise TrackingError('tracking_invalid_view')
        received = None if field['received_at'] is None else _time(field['received_at'])
        if received is not None and received > at:
            raise TrackingError('tracking_invalid_view')
        queues = None if field['payload'] is None else _payload('groupqueues', field['payload']['queues'])['queues']
        latest = field['latest_attempt']
        if latest is not None:
            if (type(latest) is not dict or type(latest.get('ok')) is not bool
                    or type(latest.get('attempted')) is not bool):
                raise TrackingError('tracking_invalid_view')
            started = None if latest['started_at'] is None else _time(latest['started_at'])
            ended = None if latest['received_at'] is None else _time(latest['received_at'])
            if (started is not None and started > at or ended is not None and
                    (ended > at or started is None or started > ended)):
                raise TrackingError('tracking_invalid_view')
        recent = field['state'] == 'recent_response'
        if recent:
            if (queues is None or received is None or latest is None or not latest['ok']
                    or not latest['attempted'] or _time(latest['received_at']) != received
                    or not _integer(latest['http_status'], 200, 299)
                    or context['latest_queue_received_at'] is None
                    or received != _time(context['latest_queue_received_at'])
                    or context['latest_queue_origin'] != 'worker_commit'
                    or not context['collector_running'] or not view['worker_alive']
                    or view['service_state'] != 'running'):
                raise TrackingError('tracking_observation_conflict')
        current = (recent and (at-received).total_seconds() <= context['max_local_age_seconds']
                   and at < _time(context['expires_at']))
        return {'as_of': _utc(at), 'public_context': context, 'queue_view_state': field['state'],
            'queue_received_at': _utc(received) if received else None,
            'current_display_evidence': bool(current),
            'queues': queues if current else None,
            'retained_display_available': queues is not None,
            'read_pair_atomic': False, 'source_freshness': 'unknown'}
    except TrackingError:
        raise
    except Exception:
        raise TrackingError('tracking_invalid_view') from None


def _display_analysis(ticket, observation, previous):
    name = 'storeQueue' if ticket['queue_type'] == 'ordinary' else 'reservationQueue'
    current = observation['current_display_evidence']
    labels = observation['queues'][name] if current else None
    present = ticket['number'] in labels if labels is not None else None
    first = previous['display']['first_locally_seen_at'] if previous else None
    last = previous['display']['last_locally_seen_at'] if previous else None
    if present:
        first = first or observation['queue_received_at']
        last = observation['queue_received_at']
    state = ('number_in_upcoming_display' if present else 'previously_seen_not_currently_displayed'
             if current and first is not None else 'not_locally_seen' if current else 'data_unavailable')
    number = re.fullmatch('([A-Za-z]?)([0-9]{1,7})', ticket['number'])
    distances = []
    for label in labels or []:
        other = re.fullmatch('([A-Za-z]?)([0-9]{1,7})', label)
        delta = int(number[2])-int(other[2]) if number and other and number[1] == other[1] else None
        distances.append({'displayed_number': label, 'arithmetic_difference': delta})
    return {'state': state, 'own_number_in_selected_display': present,
        'selected_display_numbers': labels, 'candidate_number_differences': distances,
        'first_locally_seen_at': first, 'last_locally_seen_at': last,
        'seen_time_semantics': 'local_response_receipt_not_call_time',
        'status_check_suggested': state == 'previously_seen_not_currently_displayed',
        'called_verified': False, 'no_show_verified': False,
        'exact_front_tables': None, 'number_cycle_verified': False,
        'queue_order_verified': False, 'difference_is_front_tables': False}


def _safe_receipt(result, *, committed=True, durable=True, idempotent=False):
    return {'ok': durable, 'committed': committed, 'durability_confirmed': durable,
        'tracking_version': result['tracking_version'], 'idempotent': idempotent,
        'private_output_written': committed, 'network_performed': False, 'provider_called': False,
        'business_writes': 0, 'notification_sent': False,
        'verified_training_labels': 0, 'eta_available': False}


class TrackingSession:
    """Explicit private directory, immutable ticket, bounded append-only receipts.

    Lock only while reading or publishing. Computation may run concurrently with
    a new observation; publication compares the latest private version inside
    this same directory lock. No network operation occurs under the lock.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.fd = None

    def __enter__(self):
        try:
            self.fd, _ = _open_parent(self.directory/'ticket.json', private=True)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _check_parent(self.directory/'ticket.json', self.fd, private=True)
            return self
        except BlockingIOError:
            self.__exit__(); raise TrackingError('tracking_ledger_busy') from None
        except Exception:
            self.__exit__(); raise TrackingError('tracking_invalid_ledger') from None

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _read(self, name):
        fd = None
        try:
            _check_parent(self.directory/name, self.fd, private=True)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            before = os.fstat(fd)
            if (not _private_file(before) or before.st_size > 65_536
                    or _identity(before) != _identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False))):
                raise ValueError
            parts, remaining = [], 65_537
            while remaining:
                part = os.read(fd, remaining)
                if not part:
                    break
                parts.append(part); remaining -= len(part)
            body = b''.join(parts)
            if (len(body) != before.st_size or len(body) > 65_536
                    or _identity(before) != _identity(os.fstat(fd))
                    or _identity(before) != _identity(os.stat(name, dir_fd=self.fd, follow_symlinks=False))):
                raise ValueError
            _check_parent(self.directory/name, self.fd, private=True)
            return _json(body)
        except Exception:
            raise TrackingError('tracking_invalid_ledger') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _write(self, name, value):
        # Reuse our directory lock: opening a second flock would conflict even
        # in this process. Durable link publication is atomic and never replaces.
        temporary, fd, committed = '.tracking-'+secrets.token_hex(16)+'.tmp', None, False
        try:
            _check_parent(self.directory/name, self.fd, private=True)
            body = _canonical(value).encode()
            if len(body) > 65_536:
                raise ValueError
            os.fsync(self.fd)
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            initial = os.fstat(fd)
            if not _private_file(initial):
                raise ValueError
            offset = 0
            while offset < len(body):
                count = os.write(fd, body[offset:])
                if count <= 0:
                    raise ValueError
                offset += count
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, len(body)+1) != body:
                raise ValueError
            final, named = os.fstat(fd), os.stat(temporary, dir_fd=self.fd, follow_symlinks=False)
            if (_identity(final) != _identity(named) or not _private_file(final)
                    or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)):
                raise ValueError
            _check_parent(self.directory/name, self.fd, private=True)
            os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            committed = True
            os.unlink(temporary, dir_fd=self.fd)
            _check_parent(self.directory/name, self.fd, private=True)
            return {'committed': True, 'durability_confirmed': self._confirm_durable()}
        except Exception:
            raise TrackingError('tracking_write_failed', committed=committed) from None
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except OSError:
                pass

    def _confirm_durable(self):
        try:
            _check_parent(self.directory/'ticket.json', self.fd, private=True)
            os.fsync(self.fd)
            return True
        except Exception:
            return False

    def load(self, *, now=None):
        try:
            ticket = validate_ticket(self._read('ticket.json'), now=now)
            digest = _hash(ticket)
            names = []
            with os.scandir(self.fd) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if len(names) > 2+2*ticket['max_updates']:
                        raise ValueError
            allowed = {'ticket.json', 'terminal.json'}
            updates = sorted(n for n in names if re.fullmatch('observation-[0-9]{4}.json', n))
            results = {n for n in names if re.fullmatch('prediction-[0-9]{4}.json', n)}
            if (len(updates) > ticket['max_updates'] or len(names) > 2+2*ticket['max_updates']
                    or set(names)-allowed-set(updates)-results
                    or updates != [f'observation-{n:04d}.json' for n in range(1, len(updates)+1)]
                    or any(int(n[11:15]) > len(updates) or int(n[11:15]) < 1 for n in results)):
                raise ValueError
            previous, chain = None, digest
            # Each receipt is bounded. Prior predictions need not be parsed to
            # determine the current observation; they never alter ticket state.
            for index, name in enumerate(updates, 1):
                row = self._read(name)
                if (type(row) is not dict or row.get('tracking_version') != index
                        or type(row['tracking_version']) is not int or row.get('ticket_sha256') != digest
                        or row.get('previous_observation_sha256') != chain
                        or row.get('observation_sha256') != _hash(row['observation'])
                        or row.get('policy') != POLICY):
                    raise ValueError
                observation = row['observation']
                if (type(observation) is not dict or set(observation) != {'as_of', 'public_context',
                        'queue_view_state', 'queue_received_at', 'current_display_evidence', 'queues',
                        'retained_display_available', 'read_pair_atomic', 'source_freshness'}
                        or type(observation['current_display_evidence']) is not bool
                        or type(observation['retained_display_available']) is not bool
                        or observation['read_pair_atomic'] is not False or observation['source_freshness'] != 'unknown'
                        or _context(observation['public_context'], now=now or _now()) != observation['public_context']
                        or observation['public_context']['store_id'] != ticket['store_id']
                        or observation['public_context']['queue_type'] != ticket['queue_type']
                        or _time(observation['as_of']) != _time(observation['public_context']['as_of'])
                        or _time(observation['as_of']) < _time(ticket['created_at'])
                        or _time(observation['as_of']) >= _time(ticket['deadline_at'])
                        or observation['queue_received_at'] is not None and
                            _time(observation['queue_received_at']) > _time(observation['as_of'])):
                    raise ValueError
                if observation['current_display_evidence']:
                    received = _time(observation['queue_received_at'])
                    if (observation['queue_view_state'] != 'recent_response'
                            or not observation['retained_display_available']
                            or _payload('groupqueues', observation['queues'])['queues'] != observation['queues']
                            or not observation['public_context']['collector_running']
                            or observation['public_context']['latest_queue_origin'] != 'worker_commit'
                            or received != _time(observation['public_context']['latest_queue_received_at'])
                            or (_time(observation['as_of'])-received).total_seconds() >
                                observation['public_context']['max_local_age_seconds']):
                        raise ValueError
                elif observation['queues'] is not None:
                    raise ValueError
                if previous and _time(observation['as_of']) <= _time(previous['observation']['as_of']):
                    raise ValueError
                expected = _display_analysis(ticket, observation, previous)
                if row.get('display') != expected:
                    raise ValueError
                previous, chain = row, _hash(row)
            terminal = self._read('terminal.json') if 'terminal.json' in names else None
            if terminal is not None:
                if (type(terminal) is not dict or set(terminal) != {'ticket_sha256', 'status', 'declared_at', 'basis'}
                        or terminal['ticket_sha256'] != digest
                        or terminal['status'] not in ('called', 'no_show', 'cancelled', 'ended')
                        or terminal['basis'] != 'caller_declaration_not_verified'
                        or not _time(ticket['created_at']) <= _time(terminal['declared_at']) <= (now or _now())
                        or previous is not None and _time(terminal['declared_at']) < _time(previous['observation']['as_of'])):
                    raise ValueError
            return ticket, previous, terminal
        except TrackingError:
            raise
        except Exception:
            raise TrackingError('tracking_invalid_ledger') from None


def create_session(ticket, *, directory, now=None):
    ticket = validate_ticket(ticket, now=now)
    if (_now() if now is None else now) >= _time(ticket['deadline_at']):
        raise TrackingError('tracking_deadline')
    with TrackingSession(directory) as session:
        if os.listdir(session.fd):
            raise TrackingError('tracking_already_initialized')
        publication = session._write('ticket.json', ticket)
    return {'ok': publication['durability_confirmed'], 'committed': True,
        'durability_confirmed': publication['durability_confirmed'], 'tracking_version': 0,
        'private_output_written': True, 'network_performed': False, 'provider_called': False,
        'business_writes': 0, 'notification_sent': False, 'verified_training_labels': 0, 'eta_available': False}


def observe_session(*, directory, view, context, as_of, now=None):
    clock = _now() if now is None else now
    with TrackingSession(directory) as session:
        ticket, previous, terminal = session.load(now=clock)
        if terminal is not None:
            raise TrackingError('tracking_terminal')
        observation = normalize_observation(ticket, view, context, as_of=as_of, now=clock)
        if clock >= _time(ticket['deadline_at']) or _time(as_of) >= _time(ticket['deadline_at']):
            raise TrackingError('tracking_deadline')
        if previous:
            before, after = _time(previous['observation']['as_of']), _time(observation['as_of'])
            if after == before and observation == previous['observation']:
                return _safe_receipt(previous, idempotent=True, durable=session._confirm_durable())
            if after <= before:
                raise TrackingError('tracking_observation_regressed')
            old_received = previous['observation']['queue_received_at']
            received = observation['queue_received_at']
            if (observation['current_display_evidence'] and old_received and received
                    and _time(received) < _time(old_received)):
                raise TrackingError('tracking_observation_regressed')
            if (observation['current_display_evidence'] and previous['observation']['current_display_evidence']
                    and received == old_received and observation['queues'] != previous['observation']['queues']):
                raise TrackingError('tracking_observation_conflict')
        version = previous['tracking_version']+1 if previous else 1
        if version > ticket['max_updates']:
            raise TrackingError('tracking_update_limit')
        result = {'policy': POLICY, 'tracking_version': version, 'ticket_sha256': _hash(ticket),
            'previous_observation_sha256': _hash(previous) if previous else _hash(ticket),
            'observation_sha256': _hash(observation), 'observation': observation,
            'display': _display_analysis(ticket, observation, previous),
            'waiting_basis': 'ongoing_caller_declaration_not_verified_by_public_feed',
            'verified_training_labels': 0, 'eta_available': False,
            'network_performed': False, 'provider_called': False}
        publication = session._write(f'observation-{version:04d}.json', result)
        return _safe_receipt(result, durable=publication['durability_confirmed'])


def prepare_prediction(*, directory, version, now=None):
    clock = _now() if now is None else now
    with TrackingSession(directory) as session:
        ticket, latest, terminal = session.load(now=clock)
        if terminal is not None:
            raise TrackingError('tracking_terminal')
        if not _integer(version, 1, 2000) or latest is None or version != latest['tracking_version']:
            raise TrackingError('tracking_unavailable_version')
        if clock >= _time(ticket['deadline_at']):
            raise TrackingError('tracking_deadline')
        return {'ticket': deepcopy(ticket), 'receipt': deepcopy(latest),
                'expected_receipt_sha256': _hash(latest)}


def _bound_fusion(preparation, plan, *, now):
    try:
        ticket, receipt = preparation['ticket'], preparation['receipt']
        plan = validate_fusion(plan, now=now)
        elapsed = _us(_time(receipt['observation']['as_of'])-_time(ticket['issued_at']))
        if (plan['public_context'] != receipt['observation']['public_context']
                or plan['schema_version'] != 2 or plan['prediction_target'] != 'remaining'
                or plan['data_origin'] != ('synthetic' if ticket['data_origin'] == 'synthetic' else 'research')
                or any((c['interval_sample']['elapsed_us'] != elapsed if 'interval_sample' in c else
                        c['conditional_interval_sample']['conditioned_elapsed_us'] != elapsed
                        if 'conditional_interval_sample' in c else True) for c in plan['candidates'])):
            raise ValueError
        return plan
    except Exception:
        raise TrackingError('tracking_invalid_prediction') from None


def _poll_request(display, policy, base_interval, computed_at):
    interval = policy['requested_interval_seconds'] if policy else base_interval
    reason = policy['reason'] if policy else 'arrival_time_unknown_background'
    if display['first_locally_seen_at'] is not None:
        interval = min(interval, 30)
        reason = ('own_number_in_upcoming_display' if display['own_number_in_selected_display']
                  else 'previously_seen_status_uncertain')
    return {'requested_interval_seconds': interval, 'reason': reason,
        'next_target_at': _utc(_time(computed_at)+timedelta(seconds=interval)),
        'source_freshness': 'unknown', 'scheduler_applied': False,
        'polling_performed': False, 'missed_call_prevention_guaranteed': False}


def calculate_prediction(preparation, *, source=None, reviews=None, fusion_plan=None,
                         advice=None, advice_error=None, model_version='reviewed-history-intervals-v1',
                         ai_blend_ppm=0, base_interval=300, now=None):
    """Integrate a frozen personal version with history/conditional fusion.

    The caller may supply trained complete interval candidates in the same
    schema instead. Their origin and accuracy are unverified. No paid model
    call is made; optional public advice is validated by the fusion component.
    """
    clock = _now() if now is None else now
    try:
        ticket, receipt = validate_ticket(preparation['ticket'], now=clock), preparation['receipt']
        if (receipt['ticket_sha256'] != _hash(ticket) or _hash(receipt) != preparation['expected_receipt_sha256']
                or not _integer(base_interval, 60, 3600)
                or advice_error is not None and (advice_error != 'fusion_invalid_advice' or advice is not None)):
            raise ValueError
    except Exception:
        raise TrackingError('tracking_invalid_prediction') from None
    observation = receipt['observation']
    reason, history, result, plan = None, None, None, None
    if ticket['issued_at'] is None:
        reason = 'issued_time_unknown'
    elif not observation['current_display_evidence']:
        reason = 'current_display_evidence_unavailable'
    elif clock >= _time(observation['public_context']['expires_at']):
        reason = 'public_context_expired'
    elif _us(_time(observation['as_of'])-_time(ticket['issued_at'])) > MAX_WAIT_US:
        reason = 'elapsed_wait_outside_model_support'
    elif fusion_plan is not None:
        if source is not None or reviews is not None:
            raise TrackingError('tracking_invalid_prediction')
        plan = _bound_fusion(preparation, fusion_plan, now=clock)
    elif source is None and reviews is None:
        reason = 'history_not_supplied'
    elif source is None or reviews is None:
        raise TrackingError('tracking_invalid_prediction')
    else:
        historical_plan = {'schema_version': 1, 'as_of': observation['as_of'],
            'data_origin': ticket['data_origin'], 'api_profile': ticket['api_profile'],
            'store_id': ticket['store_id'], 'queue_type': ticket['queue_type'],
            'party_size': ticket['party_size'], 'table_type': ticket['table_type'],
            'mode': 'remaining', 'minimum_samples': ticket['minimum_samples'],
            'target_episode_id': ticket['episode_id'], 'issued_at': ticket['issued_at'], 'call_not_observed': True}
        history = build_history_fusion(source=source, reviews=reviews, plan=historical_plan,
            context=observation['public_context'], model_version=model_version, ai_blend_ppm=ai_blend_ppm)
        plan = history['candidates'][0]['fusion_plan']
        if plan is None:
            reason = 'insufficient_matching_history'
        else:
            plan = _bound_fusion(preparation, plan, now=clock)
    if plan is not None:
        result = fuse(plan, advice=advice, now=clock)
        if advice_error is not None:
            result['fallback_reason'] = advice_error
    polling, early = None, None
    if result is not None:
        early = _time(observation['as_of'])+timedelta(microseconds=result['wait_quantile_envelopes_us']['p10']['lower_us'])
    if ticket['desired_arrival_at'] is not None:
        polling = polling_policy(desired_arrival_at=ticket['desired_arrival_at'],
            call_offset_minutes=ticket['call_offset_minutes'], as_of=_utc(clock),
            earliest_call_at=_utc(early) if early else None, base_interval=base_interval)
    call_times = {key: {bound: _utc(_time(observation['as_of'])+timedelta(microseconds=value))
                       if value is not None else None for bound, value in limits.items()}
                  for key, limits in result['wait_quantile_envelopes_us'].items()} if result else None
    return {'policy': POLICY, 'tracking_version': receipt['tracking_version'],
        'ticket_sha256': receipt['ticket_sha256'], 'expected_receipt_sha256': preparation['expected_receipt_sha256'],
        'observation_sha256': receipt['observation_sha256'], 'computed_at': _utc(clock),
        'research_prediction_available': result is not None, 'unavailable_reason': reason,
        'display': deepcopy(receipt['display']), 'history': history, 'fusion_plan': plan, 'fusion': result,
        'monitoring_policy': polling, 'base_interval_seconds': base_interval,
        'polling_request': _poll_request(receipt['display'], polling, base_interval, _utc(clock)),
        'research_call_time_envelopes': call_times, 'earliest_call_estimate_verified': False,
        'survival_basis': 'ongoing_caller_declaration_not_verified_by_public_feed',
        'number_used_as_verified_position': False, 'provider_called': False,
        'notification_sent': False, 'scheduler_applied': False, 'business_writes': 0,
        'verified_training_labels': 0, 'eta_available': False,
        'output_requires_private_handling': True}


def _validate_prediction(preparation, result, *, clock):
    """Recheck a frozen result without using later training observations."""
    ticket, latest = preparation['ticket'], preparation['receipt']
    fields = {'policy', 'tracking_version', 'ticket_sha256', 'expected_receipt_sha256',
        'observation_sha256', 'computed_at', 'research_prediction_available', 'unavailable_reason',
        'display', 'history', 'fusion_plan', 'fusion', 'monitoring_policy', 'base_interval_seconds',
        'polling_request', 'research_call_time_envelopes', 'earliest_call_estimate_verified',
        'survival_basis', 'number_used_as_verified_position', 'provider_called', 'notification_sent',
        'scheduler_applied', 'business_writes', 'verified_training_labels', 'eta_available',
        'output_requires_private_handling'}
    optional = {'realtime_research', 'provider_adapter_summary'}
    if (type(result) is not dict or not fields <= set(result) or set(result)-fields-optional
            or any(type(result[k]) is not dict for k in optional if k in result)
            or result['earliest_call_estimate_verified'] is not False
            or result['number_used_as_verified_position'] is not False
            or result['output_requires_private_handling'] is not True
            or result['survival_basis'] != 'ongoing_caller_declaration_not_verified_by_public_feed'):
        raise TrackingError('tracking_invalid_prediction')
    if (type(result) is not dict or result.get('tracking_version') != latest['tracking_version']
            or type(result['tracking_version']) is not int or result.get('ticket_sha256') != _hash(ticket)
            or result.get('expected_receipt_sha256') != _hash(latest)
            or result.get('observation_sha256') != latest['observation_sha256']
            or not _time(latest['observation']['as_of']) <= _time(result['computed_at']) <= clock
            or result.get('eta_available') is not False or result.get('provider_called') is not False
            or result.get('display') != latest['display'] or result.get('policy') != POLICY
            or type(result.get('research_prediction_available')) is not bool
            or result.get('notification_sent') is not False or result.get('scheduler_applied') is not False
            or type(result.get('business_writes')) is not int or result['business_writes'] != 0
            or type(result.get('verified_training_labels')) is not int or result['verified_training_labels'] != 0
            or not _integer(result.get('base_interval_seconds'), 60, 3600)):
        raise TrackingError('tracking_invalid_prediction')
    if result['research_prediction_available']:
        plan = _bound_fusion(preparation, result['fusion_plan'], now=_time(result['computed_at']))
        accepted = result['fusion'].get('advice')
        if accepted is not None:
            request = result['fusion']['public_request']
            accepted = {**accepted, 'schema_version': 1,
                'public_input_sha256': request['public_input_sha256'],
                'observation_revision': request['context']['observation_revision'],
                'model_version': request['model_version']}
        rebuilt = fuse(plan, advice=accepted, now=_time(result['computed_at']))
        for key in ('local_input_sha256', 'public_request', 'advice_accepted', 'ai_numerical_influence_applied',
                    'effective_weight_numerators', 'wait_quantile_envelopes_us', 'plan', 'advice'):
            if result['fusion'].get(key) != rebuilt[key]:
                raise TrackingError('tracking_invalid_prediction')
        if rebuilt['advice_accepted'] and clock >= _time(rebuilt['advice']['expires_at']):
            raise TrackingError('tracking_superseded')
        call_times = {key: {bound: _utc(_time(latest['observation']['as_of'])+timedelta(microseconds=value))
                       if value is not None else None for bound, value in limits.items()}
                      for key, limits in rebuilt['wait_quantile_envelopes_us'].items()}
        if result.get('research_call_time_envelopes') != call_times:
            raise TrackingError('tracking_invalid_prediction')
    elif result.get('fusion') is not None or result.get('fusion_plan') is not None:
        raise TrackingError('tracking_invalid_prediction')
    elif result.get('research_call_time_envelopes') is not None or result.get('unavailable_reason') not in (
            'issued_time_unknown', 'current_display_evidence_unavailable', 'public_context_expired',
            'elapsed_wait_outside_model_support', 'history_not_supplied', 'insufficient_matching_history'):
        raise TrackingError('tracking_invalid_prediction')
    expected_polling = None
    if ticket['desired_arrival_at'] is not None:
        early = result['research_call_time_envelopes']['p10']['lower_us'] if result['research_prediction_available'] else None
        expected_polling = polling_policy(desired_arrival_at=ticket['desired_arrival_at'],
            call_offset_minutes=ticket['call_offset_minutes'], as_of=result['computed_at'],
            earliest_call_at=early, base_interval=result['base_interval_seconds'])
    if result.get('monitoring_policy') != expected_polling:
        raise TrackingError('tracking_invalid_prediction')
    if result.get('polling_request') != _poll_request(latest['display'], expected_polling,
            result['base_interval_seconds'], result['computed_at']):
        raise TrackingError('tracking_invalid_prediction')
    # Do not commit a once-valid numerical prediction after its context TTL.
    if result['research_prediction_available'] and clock >= _time(latest['observation']['public_context']['expires_at']):
        raise TrackingError('tracking_superseded')


def _unpack_prediction(value, *, now):
    """Legacy numerical records remain usable, but have no publication receipt."""
    try:
        if type(value) is not dict:
            raise ValueError
        if 'publication_receipt' not in value:
            return value, None
        result = {k:v for k,v in value.items() if k != 'publication_receipt'}
        receipt = value['publication_receipt']
        if (type(receipt) is not dict or set(receipt) != {'schema_version', 'policy',
                'first_received_at', 'result_sha256', 'independent_time_attestation'}
                or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
                or receipt['policy'] != 'program_clock_before_atomic_publication_v1'
                or receipt['independent_time_attestation'] is not False
                or receipt['result_sha256'] != _hash(result)):
            raise ValueError
        received = _time(receipt['first_received_at'])
        if (_utc(received) != receipt['first_received_at']
                or not _time(result['computed_at']) <= received <= now):
            raise ValueError
        return result, receipt
    except Exception:
        raise TrackingError('tracking_invalid_ledger') from None


def publish_prediction(*, directory, preparation, result, now=None):
    """The current-version compare and durable no-overwrite commit share a lock."""
    clock = _now() if now is None else now
    with TrackingSession(directory) as session:
        ticket, latest, terminal = session.load(now=clock)
        if terminal is not None:
            raise TrackingError('tracking_terminal')
        if clock >= _time(ticket['deadline_at']):
            raise TrackingError('tracking_deadline')
        if (latest is None or _hash(ticket) != _hash(preparation['ticket'])
                or _hash(latest) != preparation['expected_receipt_sha256']
                or latest != preparation['receipt']):
            raise TrackingError('tracking_superseded')
        _validate_prediction(preparation, result, clock=clock)
        name = f"prediction-{latest['tracking_version']:04d}.json"
        if name in os.listdir(session.fd):
            old, _ = _unpack_prediction(session._read(name), now=clock)
            if old == result:
                return _safe_receipt(result, idempotent=True, durable=session._confirm_durable())
            raise TrackingError('tracking_prediction_conflict')
        # Caller input cannot supply this receipt. The program assigns its clock
        # while holding the same publication lock, immediately before one atomic
        # file commit. This is not a signature or a post-fsync time attestation.
        received = _now() if now is None else now
        if received < clock or received >= _time(ticket['deadline_at']):
            raise TrackingError('tracking_superseded')
        _validate_prediction(preparation, result, clock=received)
        stored = {**result, 'publication_receipt': {'schema_version': 1,
            'policy': 'program_clock_before_atomic_publication_v1',
            'first_received_at': _utc(received), 'result_sha256': _hash(result),
            'independent_time_attestation': False}}
        publication = session._write(name, stored)
        return _safe_receipt(result, durable=publication['durability_confirmed'])


def end_session(*, directory, status, declared_at, now=None):
    clock = _now() if now is None else now
    with TrackingSession(directory) as session:
        ticket, latest, terminal = session.load(now=clock)
        if (status not in ('called', 'no_show', 'cancelled', 'ended')
                or not _time(ticket['created_at']) <= _time(declared_at) <= clock
                or latest is not None and _time(declared_at) < _time(latest['observation']['as_of'])):
            raise TrackingError('tracking_invalid_input')
        value = {'ticket_sha256': _hash(ticket), 'status': status,
            'declared_at': _utc(_time(declared_at)), 'basis': 'caller_declaration_not_verified'}
        if terminal is not None:
            if terminal != value:
                raise TrackingError('tracking_terminal')
            durable = session._confirm_durable()
        else:
            durable = session._write('terminal.json', value)['durability_confirmed']
        return {'ok': durable, 'committed': True, 'durability_confirmed': durable,
            'terminal': True, 'status_basis': 'caller_declaration_not_verified',
            'network_performed': False, 'provider_called': False, 'business_writes': 0,
            'notification_sent': False, 'verified_training_labels': 0, 'eta_available': False}


def session_status(*, directory, now=None):
    clock = _now() if now is None else now
    with TrackingSession(directory) as session:
        ticket, latest, terminal = session.load(now=clock)
        present = bool(latest and f"prediction-{latest['tracking_version']:04d}.json" in os.listdir(session.fd))
        state = 'unavailable'
        if present:
            prediction, _ = _unpack_prediction(
                session._read(f"prediction-{latest['tracking_version']:04d}.json"), now=clock)
            if prediction.get('expected_receipt_sha256') != _hash(latest):
                raise TrackingError('tracking_invalid_ledger')
            state = 'research_only' if prediction['research_prediction_available'] else 'unavailable'
            if prediction['research_prediction_available'] and (
                    clock >= _time(latest['observation']['public_context']['expires_at'])
                    or prediction['fusion']['advice_accepted'] and clock >= _time(prediction['fusion']['advice']['expires_at'])):
                state = 'expired'
        if terminal is not None:
            state = 'terminal'
        elif clock >= _time(ticket['deadline_at']):
            state = 'deadline_reached'
        return {'tracking_schema_version': 1, 'tracking_version': latest['tracking_version'] if latest else 0,
            'terminal': terminal is not None, 'deadline_reached': clock >= _time(ticket['deadline_at']),
            'current_prediction_file_present': present, 'prediction_state': state,
            'current_prediction_freshness_verified': False, 'source_freshness': 'unknown',
            'network_performed': False, 'provider_called': False, 'business_writes': 0,
            'notification_sent': False, 'verified_training_labels': 0, 'eta_available': False}
