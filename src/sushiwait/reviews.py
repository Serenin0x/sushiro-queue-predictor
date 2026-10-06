"""Human-declared review of private outcome intervals, separate from authenticity.

Reviews bind an exact first-receipt revision. Corrections invalidate old reviews;
acceptance never certifies a real event, a reviewer identity, or model accuracy.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from .capture import _open_parent
from .credentials import CredentialError, _private_file, _read_private_file
from .intake import _record as intake_record, _canonical, OutcomeIntakeStore
from .outcomes import (OutcomeError, OutcomeStore, _json,
                       _time, _utc, _uuid, candidate_targets, validate_episode)
from .packets import PacketError, _write_packet

MAX_REVIEWS = 10_000
MAX_BYTES = 16_384
POLICY = 'human_declared_call_interval_v1'
_FIELDS = frozenset({'review_schema_version', 'review_id', 'reviewer_id', 'revision',
    'supersedes_revision', 'episode_id', 'episode_revision', 'episode_receipt_sha256',
    'reviewed_at', 'decision', 'evidence_kind', 'issued_time_bounds_checked',
    'called_time_bounds_checked', 'store_and_queue_checked', 'reason_code'})
_COLUMNS = {'id': 'INTEGER', 'review_id': 'TEXT', 'revision': 'INTEGER',
    'received_at': 'TEXT', 'review_sha256': 'TEXT', 'payload_json': 'TEXT'}
_CODES = frozenset({'review_invalid_record', 'review_invalid_scope_or_bounds',
    'review_database_schema', 'review_database_unsafe', 'review_requires_idle_reader',
    'review_requires_idle_writer', 'review_limit_exceeded', 'review_revision_conflict',
    'review_time_order', 'review_target_missing', 'review_target_changed',
    'review_target_not_latest', 'review_acceptance_not_supported',
    'review_source_invalid', 'review_input_unavailable', 'review_input_unsafe',
    'review_input_too_large', 'review_input_changed', 'review_output_exists',
    'review_operation_failed'})
_SOURCE_SELECT = ('id,'
    'CASE WHEN length(CAST(episode_id AS BLOB))<=36 THEN episode_id ELSE NULL END,revision,'
    'CASE WHEN length(CAST(store_id AS BLOB))<=10 THEN store_id ELSE NULL END,'
    'CASE WHEN length(CAST(data_origin AS BLOB))<=13 THEN data_origin ELSE NULL END,'
    'CASE WHEN length(CAST(api_profile AS BLOB))<=20 THEN api_profile ELSE NULL END,'
    'CASE WHEN length(CAST(received_at AS BLOB))<=40 THEN received_at ELSE NULL END,'
    'CASE WHEN length(CAST(record_sha256 AS BLOB))<=64 THEN record_sha256 ELSE NULL END,'
    'CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json ELSE NULL END')
_SELECT = ('id,CASE WHEN length(CAST(review_id AS BLOB))<=36 THEN review_id ELSE NULL END,revision,'
    'CASE WHEN length(CAST(received_at AS BLOB))<=40 THEN received_at ELSE NULL END,'
    'CASE WHEN length(CAST(review_sha256 AS BLOB))<=64 THEN review_sha256 ELSE NULL END,'
    'CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json ELSE NULL END')


class ReviewError(ValueError):
    def __init__(self, code, *, commit_status='not_started'):
        self.error_code = code if code in _CODES else 'review_operation_failed'
        self.commit_status = commit_status if commit_status in ('not_started', 'committed', 'unknown') else 'unknown'
        super().__init__(self.error_code)


def _clock():
    return datetime.now(timezone.utc)


def validate_review(value, *, now=None):
    """Normalize bounded human declarations without interpreting them as proof."""
    try:
        clock = _clock() if now is None else now
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or type(value['review_schema_version']) is not int or value['review_schema_version'] != 1
                or not isinstance(clock, datetime) or clock.tzinfo is None):
            raise ValueError
        for key in ('review_id', 'reviewer_id', 'episode_id'):
            _uuid(value[key])
        revision, prior = value['revision'], value['supersedes_revision']
        if (type(revision) is not int or not 1 <= revision <= 2**31-1
                or revision == 1 and prior is not None
                or revision > 1 and (type(prior) is not int or prior != revision-1)
                or type(value['episode_revision']) is not int or not 1 <= value['episode_revision'] <= 2**31-1
                or not isinstance(value['episode_receipt_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', value['episode_receipt_sha256'])):
            raise ValueError
        reviewed = _time(value['reviewed_at'])
        if reviewed > clock:
            raise ValueError
        if (value['decision'] not in ('accept', 'reject', 'insufficient_evidence')
                or value['evidence_kind'] not in ('self_observation_confirmation', 'synthetic')
                or any(type(value[k]) is not bool for k in ('issued_time_bounds_checked',
                    'called_time_bounds_checked', 'store_and_queue_checked'))):
            raise ValueError
        if value['decision'] == 'accept':
            if (value['reason_code'] != 'confirmed_call_interval'
                    or not all(value[k] for k in ('issued_time_bounds_checked',
                        'called_time_bounds_checked', 'store_and_queue_checked'))):
                raise ValueError
        elif value['reason_code'] not in ('missing_call', 'uncertain_time', 'mismatch', 'withdrawn'):
            raise ValueError
        safe = {**value, 'reviewed_at': _utc(reviewed)}
        if len(_canonical(safe).encode()) > MAX_BYTES:
            raise ValueError
        return safe
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ReviewError('review_invalid_record') from None


def read_review(path):
    try:
        body = _read_private_file(path)
        if len(body) > MAX_BYTES:
            raise ReviewError('review_input_too_large')
        return validate_review(_json(body))
    except CredentialError as error:
        mapping = {'credentials_file_unsafe': 'review_input_unsafe',
            'credentials_file_too_large': 'review_input_too_large',
            'credentials_file_changed': 'review_input_changed'}
        raise ReviewError(mapping.get(error.error_code, 'review_input_unavailable')) from None
    except OutcomeError:
        raise ReviewError('review_invalid_record') from None


def _digest(review, received):
    return hashlib.sha256(_canonical({'review': review, 'received_at': received}).encode()).hexdigest()


def _review_record(row, *, now):
    try:
        value = validate_review(_json(row[5]), now=now)
        received = _time(row[3])
        if (type(row[0]) is not int or row[0] < 1 or row[1] != value['review_id']
                or row[2] != value['revision'] or row[5] != _canonical(value)
                or row[4] != _digest(value, row[3]) or _utc(received) != row[3]
                or not _time(value['reviewed_at']) <= received <= now):
            raise ValueError
        return value, received
    except (ValueError, TypeError, IndexError):
        raise ReviewError('review_invalid_record') from None


def _source_rows(source, *, now, limit=MAX_REVIEWS):
    """Complete bounded receipt chains, including later revisions for integrity."""
    if not isinstance(source, OutcomeIntakeStore) or not source.read_only or source.db.in_transaction:
        raise ReviewError('review_requires_idle_reader')
    try:
        source._guard()
        source.db.execute('BEGIN')
        rows = source.db.execute(f'SELECT {_SOURCE_SELECT} FROM intake_revisions ORDER BY id LIMIT ?',
                                 (limit+1,)).fetchall()
        if len(rows) > limit:
            raise ReviewError('review_limit_exceeded')
        checked, latest, last_time = {}, {}, None
        for row in rows:
            episode, received = intake_record(row, now=now)
            if last_time is not None and received < last_time:
                raise ReviewError('review_source_invalid')
            last_time = received
            prior = latest.get(episode['episode_id'])
            if (episode['revision'] != (prior[0]['revision']+1 if prior else 1)
                    or prior and (any(episode[k] != prior[0][k] for k in
                        ('store_id', 'data_origin', 'api_profile', 'queue_type'))
                        or _time(episode['recorded_at']) < _time(prior[0]['recorded_at']))):
                raise ReviewError('review_source_invalid')
            item = (episode, received, row[7])
            checked[(episode['episode_id'], episode['revision'])] = item
            latest[episode['episode_id']] = item
        source._guard()
        return checked, latest
    except ReviewError:
        raise
    except Exception:
        raise ReviewError('review_source_invalid') from None
    finally:
        if source.db.in_transaction:
            source.db.rollback()


def _bind(review, item):
    if item is None:
        raise ReviewError('review_target_missing')
    episode, receipt, digest = item
    if digest != review['episode_receipt_sha256']:
        raise ReviewError('review_target_changed')
    expected = 'synthetic' if episode['data_origin'] == 'synthetic' else 'self_observation_confirmation'
    if review['evidence_kind'] != expected or _time(review['reviewed_at']) < receipt:
        raise ReviewError('review_time_order')
    if review['decision'] == 'accept' and (candidate_targets(episode)['called_wait'] is None
                                         or episode['queue_type'] == 'unknown'):
        raise ReviewError('review_acceptance_not_supported')


def _chain(value, received, prior):
    if (value['revision'] != (prior[0]['revision']+1 if prior else 1)
            or prior and (any(value[k] != prior[0][k] for k in ('episode_id', 'reviewer_id'))
                or value['episode_revision'] < prior[0]['episode_revision']
                or _time(value['reviewed_at']) < _time(prior[0]['reviewed_at'])
                or received < prior[1])):
        raise ReviewError('review_revision_conflict')


def draft_review(source, episode, destination):
    """Write a private, unapproved draft for an exact already-received episode."""
    try:
        now = _clock()
        safe = validate_episode(episode, now=now)
        _, latest = _source_rows(source, now=now)
        item = latest.get(safe['episode_id'])
        if item is None:
            raise ReviewError('review_target_missing')
        if item[0] != safe:
            raise ReviewError('review_target_changed')
        value = {'review_schema_version': 1, 'review_id': str(uuid4()), 'reviewer_id': str(uuid4()),
            'revision': 1, 'supersedes_revision': None, 'episode_id': safe['episode_id'],
            'episode_revision': safe['revision'], 'episode_receipt_sha256': item[2],
            'reviewed_at': _utc(now), 'decision': 'insufficient_evidence',
            'evidence_kind': 'synthetic' if safe['data_origin'] == 'synthetic' else 'self_observation_confirmation',
            'issued_time_bounds_checked': False, 'called_time_bounds_checked': False,
            'store_and_queue_checked': False, 'reason_code': 'uncertain_time'}
        validate_review(value, now=now)
        source._guard()
        result = _write_packet((_canonical(value)+'\n').encode(), Path(destination),
                               before_publish=lambda _: source._guard())
        return {**result, 'draft_written': True, 'draft_approved': False,
            'authenticity_verified': False, 'verified_training_labels': 0,
            'output_requires_private_handling': True, 'network_performed': False}
    except ReviewError:
        raise
    except PacketError as error:
        code = 'review_output_exists' if error.error_code == 'packet_output_exists' else 'review_operation_failed'
        raise ReviewError(code, commit_status='committed' if error.committed else 'not_started') from None
    except Exception:
        raise ReviewError('review_operation_failed') from None


class OutcomeReviewStore(OutcomeStore):
    """Private append-only review receipt ledger; source database remains untouched."""
    def __init__(self, path, *, read_only=False):
        self.path = Path(os.path.abspath(path))
        self.read_only = read_only
        self.parent_fd = self.db = None
        try:
            import fcntl
            self.parent_fd, self.name = _open_parent(self.path, private=True)
            fcntl.flock(self.parent_fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            fd = os.open(self.name, (os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT)
                         | os.O_NOFOLLOW, 0o600, dir_fd=self.parent_fd)
            try:
                info = os.fstat(fd)
                if not _private_file(info) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ReviewError('review_database_unsafe')
                self.identity = info.st_dev, info.st_ino
            finally:
                os.close(fd)
            self.db = sqlite3.connect(self.path.as_uri()+('?mode=ro' if read_only else '?mode=rw'), uri=True, timeout=0)
            self.db.execute('PRAGMA trusted_schema=OFF')
            self._guard()
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            objects = self.db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and not objects and not read_only:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('CREATE TABLE outcome_reviews(id INTEGER PRIMARY KEY,review_id TEXT NOT NULL,'
                    'revision INTEGER NOT NULL,received_at TEXT NOT NULL,review_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL)')
                self.db.execute('CREATE UNIQUE INDEX review_revision ON outcome_reviews(review_id,revision)')
                self.db.execute('PRAGMA user_version=1')
                self.db.commit()
                version, objects = 1, [('table', 'outcome_reviews'), ('index', 'review_revision')]
            if (version != 1 or set(objects) != {('table', 'outcome_reviews'), ('index', 'review_revision')}
                    or {r[1]: r[2].upper() for r in self.db.execute('PRAGMA table_info(outcome_reviews)')} != _COLUMNS
                    or self.db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or {r[1]: (r[2], r[4]) for r in self.db.execute('PRAGMA index_list(outcome_reviews)')}
                       != {'review_revision': (1, 0)}
                    or [r[2] for r in self.db.execute('PRAGMA index_info(review_revision)')]
                       != ['review_id', 'revision']):
                raise ReviewError('review_database_schema')
        except (KeyboardInterrupt, SystemExit):
            self.close()
            raise
        except Exception as error:
            self.close()
            raise ReviewError(error.error_code if isinstance(error, ReviewError) else 'review_database_unsafe') from None

    def _rows(self, *, now, limit=MAX_REVIEWS):
        rows = self.db.execute(f'SELECT {_SELECT} FROM outcome_reviews ORDER BY id LIMIT ?', (limit+1,)).fetchall()
        if len(rows) > limit:
            raise ReviewError('review_limit_exceeded')
        checked, chains, last = [], {}, None
        for row in rows:
            value, received = _review_record(row, now=now)
            if last is not None and received < last:
                raise ReviewError('review_time_order')
            _chain(value, received, chains.get(value['review_id']))
            chains[value['review_id']] = value, received
            checked.append((value, received))
            last = received
        return checked

    def append(self, value, *, source):
        if self.read_only or self.db is None or self.db.in_transaction:
            raise ReviewError('review_requires_idle_writer')
        attempted = committed = False
        try:
            now = _clock()
            review = validate_review(value, now=now)
            sources, latest = _source_rows(source, now=now)
            self._guard()
            self.db.execute('BEGIN IMMEDIATE')
            now = _clock()
            validate_review(review, now=now)
            rows = self._rows(now=now)
            for old, _ in rows:
                _bind(old, sources.get((old['episode_id'], old['episode_revision'])))
            for old, _ in rows:
                if old['review_id'] == review['review_id'] and old['revision'] == review['revision']:
                    if old != review:
                        raise ReviewError('review_revision_conflict')
                    self.db.rollback()
                    return {'committed': False, 'commit_status': 'not_started', 'idempotent': True,
                        'first_receipt_preserved': True, 'authenticity_verified': False, 'verified_training_labels': 0}
            if len(rows) >= MAX_REVIEWS:
                raise ReviewError('review_limit_exceeded')
            prior = next(((v,t) for v,t in reversed(rows) if v['review_id'] == review['review_id']), None)
            _chain(review, now, prior)
            if rows and now < rows[-1][1]:
                raise ReviewError('review_time_order')
            item = sources.get((review['episode_id'], review['episode_revision']))
            _bind(review, item)
            if latest[review['episode_id']][0]['revision'] != review['episode_revision']:
                raise ReviewError('review_target_not_latest')
            self.db.execute('INSERT INTO outcome_reviews(review_id,revision,received_at,review_sha256,payload_json) VALUES(?,?,?,?,?)',
                (review['review_id'], review['revision'], _utc(now), _digest(review, _utc(now)), _canonical(review)))
            source._guard()
            self._guard()
            attempted = True
            self.db.commit()
            committed = True
            self._guard()
            os.fsync(self.parent_fd)
            return {'committed': True, 'commit_status': 'committed', 'idempotent': False,
                'first_receipt_preserved': True, 'durability_confirmed': True,
                'authenticity_verified': False, 'verified_training_labels': 0}
        except (KeyboardInterrupt, SystemExit):
            if not committed:
                self.db.rollback()
            raise
        except Exception as error:
            if not committed:
                self.db.rollback()
            raise ReviewError(error.error_code if isinstance(error, ReviewError) else 'review_operation_failed',
                commit_status='committed' if committed else 'unknown' if attempted else 'not_started') from None

    def cohort(self, *, source, as_of, data_origin, api_profile, max_revisions=MAX_REVIEWS):
        """Aggregate review qualifications; no private episode or targets exported."""
        return self._selection(source=source, as_of=as_of, data_origin=data_origin,
            api_profile=api_profile, max_revisions=max_revisions)[0]

    def _selection(self, *, source, as_of, data_origin, api_profile, max_revisions=MAX_REVIEWS):
        """Internal private candidates for research; the public cohort remains aggregate."""
        begun = False
        try:
            now, cutoff = _clock(), _time(as_of)
            if (cutoff > now or data_origin not in ('self_reported', 'synthetic')
                    or api_profile not in ('legacy', 'miniapp_gateway')
                    or type(max_revisions) is not int or not 1 <= max_revisions <= MAX_REVIEWS):
                raise ReviewError('review_invalid_scope_or_bounds')
            if not self.read_only or self.db.in_transaction:
                raise ReviewError('review_requires_idle_reader')
            sources, _ = _source_rows(source, now=now, limit=max_revisions)
            selected = {}
            for episode, received, digest in sources.values():
                if received <= cutoff and episode['data_origin'] == data_origin and episode['api_profile'] == api_profile:
                    selected[episode['episode_id']] = episode, received, digest
            self._guard()
            self.db.execute('BEGIN')
            begun = True
            rows = self._rows(now=now, limit=max_revisions)
            reviews = {}
            review_receipts = {}
            future = 0
            for value, received in rows:
                _bind(value, sources.get((value['episode_id'], value['episode_revision'])))
                if received <= cutoff:
                    reviews[value['review_id']] = value
                    review_receipts[value['review_id']] = received
                else:
                    future += 1
            attached, stale, decisions = {}, 0, Counter()
            for review in reviews.values():
                item = selected.get(review['episode_id'])
                if item is None:
                    continue
                if review['episode_revision'] != item[0]['revision'] or review['episode_receipt_sha256'] != item[2]:
                    stale += 1
                    continue
                attached.setdefault(review['episode_id'], []).append(review)
                decisions[review['decision']] += 1
            accepted = rejected = insufficient = conflicts = 0
            candidates = []
            for episode_id, group in attached.items():
                states = {v['decision'] for v in group}
                conflicts += int(len(states) > 1)
                if states == {'accept'}:
                    accepted += 1
                    item = selected[episode_id]
                    candidates.append({'episode': item[0], 'source_receipt_sha256': item[2],
                        'available_at': _utc(max(item[1], *(review_receipts[v['review_id']] for v in group)))})
                elif 'reject' in states:
                    rejected += 1
                else:
                    insufficient += 1
            source._guard()
            self._guard()
            report = {'review_schema_version': 1, 'review_policy': POLICY,
                'source_revisions_audited': len(sources), 'review_revisions_audited': len(rows),
                'selected_episodes': len(selected), 'reviewed_episodes': len(attached),
                'episodes_without_current_review': len(selected)-len(attached),
                'stale_current_reviews': stale, 'future_review_receipts_excluded': future,
                'current_review_decisions': dict(sorted(decisions.items())),
                'accepted_human_declared_call_intervals': accepted if data_origin == 'self_reported' else 0,
                'accepted_synthetic_call_intervals': accepted if data_origin == 'synthetic' else 0,
                'rejected_episodes': rejected, 'insufficient_evidence_episodes': insufficient,
                'conflicting_review_episodes': conflicts,
                'availability_basis': 'local_source_and_review_first_receipts',
                'review_identity_authenticated': False, 'authenticity_verified': False,
                'independent_time_attestation': False, 'historical_availability_verified': False,
                'verified_training_labels': 0, 'training_eligible': False,
                'prediction_features_exported': False, 'eta_available': False,
                'output_requires_private_handling': True, 'network_performed': False}
            return report, candidates
        except ReviewError:
            raise
        except Exception:
            raise ReviewError('review_operation_failed') from None
        finally:
            if begun and self.db.in_transaction:
                self.db.rollback()
