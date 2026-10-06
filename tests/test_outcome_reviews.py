"""Review receipt cutoffs, correction invalidation and honest evidence scope."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.cli import main
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.reviews import OutcomeReviewStore, ReviewError, draft_review, read_review, validate_review
import test_outcomes as fixtures

BASE = datetime(2026, 10, 6, 4, 30, tzinfo=timezone.utc)

def stamp(second):
    return (BASE+timedelta(seconds=second)).isoformat()


class OutcomeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.source_parent, self.review_parent = self.parent/'source', self.parent/'reviews'
        self.source_parent.mkdir(mode=0o700)
        self.review_parent.mkdir(mode=0o700)
        self.source_path = self.source_parent/'source.sqlite3'
        self.review_path = self.review_parent/'reviews.sqlite3'
        self.draft_path = self.review_parent/'draft.json'
        self.value = fixtures.record()
        self.add_source(self.value, 0)
        self.review = self.draft(self.value, 10)

    def add_source(self, value, second):
        with OutcomeIntakeStore(self.source_path) as source, patch('sushiwait.intake._clock', return_value=BASE+timedelta(seconds=second)):
            source.append(value)

    def draft(self, value, second=10):
        if self.draft_path.exists():
            self.draft_path.unlink()
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, patch('sushiwait.reviews._clock', return_value=BASE+timedelta(seconds=second)):
            result = draft_review(source, value, self.draft_path)
        self.assertTrue(result['draft_written'])
        self.assertFalse(result['draft_approved'])
        return json.loads(self.draft_path.read_text())

    def accept(self, review=None):
        result = deepcopy(self.review if review is None else review)
        result.update(decision='accept', reason_code='confirmed_call_interval',
            issued_time_bounds_checked=True, called_time_bounds_checked=True, store_and_queue_checked=True)
        return result

    def receive(self, review=None, second=20):
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, OutcomeReviewStore(self.review_path) as store, \
                patch('sushiwait.reviews._clock', return_value=BASE+timedelta(seconds=second)):
            return store.append(self.accept() if review is None else review, source=source)

    def cohort(self, second=30, **options):
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, OutcomeReviewStore(self.review_path, read_only=True) as store, \
                patch('sushiwait.reviews._clock', return_value=BASE+timedelta(seconds=1000)):
            return store.cohort(source=source, as_of=stamp(second), data_origin=options.pop('data_origin', 'synthetic'),
                api_profile=options.pop('api_profile', 'miniapp_gateway'), **options)

    def test_private_draft_defaults_to_unapproved_and_has_exact_receipt(self):
        self.assertEqual(self.draft_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.review['decision'], 'insufficient_evidence')
        self.assertFalse(self.review['called_time_bounds_checked'])
        with OutcomeIntakeStore(self.source_path, read_only=True) as source:
            digest = source.db.execute('SELECT record_sha256 FROM intake_revisions').fetchone()[0]
        self.assertEqual(self.review['episode_receipt_sha256'], digest)

    def test_unapproved_draft_remains_insufficient(self):
        self.receive(self.review)
        result = self.cohort()
        self.assertEqual(result['insufficient_evidence_episodes'], 1)
        self.assertEqual(result['accepted_synthetic_call_intervals'], 0)

    def test_synthetic_acceptance_is_not_real_or_verified_training(self):
        self.receive()
        result = self.cohort()
        self.assertEqual(result['accepted_synthetic_call_intervals'], 1)
        self.assertEqual(result['accepted_human_declared_call_intervals'], 0)
        self.assertEqual(result['verified_training_labels'], 0)
        for key in ('training_eligible', 'authenticity_verified', 'review_identity_authenticated', 'eta_available'):
            self.assertFalse(result[key])
        body = json.dumps(result)
        for private in (self.review['episode_id'], self.review['reviewer_id'], self.review['episode_receipt_sha256'], '900001', '2020-10'):
            self.assertNotIn(private, body)

    def test_mock_self_report_confirmation_is_distinct_from_authentication(self):
        value = deepcopy(self.value)
        value['episode_id'] = str(uuid4())
        value['data_origin'] = 'self_reported'
        for event in value['events']:
            event['evidence_kind'] = 'self_observation'
        self.add_source(value, 2)
        review = self.draft(value)
        self.receive(self.accept(review))
        report = self.cohort(data_origin='self_reported')
        self.assertEqual(report['accepted_human_declared_call_intervals'], 1)
        self.assertEqual(report['verified_training_labels'], 0)
        self.assertFalse(report['authenticity_verified'])

    def test_no_backdating_before_source_and_review_receipts(self):
        self.receive(second=100)
        self.assertEqual(self.cohort(-1)['selected_episodes'], 0)
        self.assertEqual(self.cohort(99)['reviewed_episodes'], 0)
        self.assertEqual(self.cohort(100)['accepted_synthetic_call_intervals'], 1)

    def test_review_correction_preserves_earlier_cutoff(self):
        self.receive()
        changed = self.accept()
        changed.update(revision=2, supersedes_revision=1, reviewed_at=stamp(80), decision='reject', reason_code='withdrawn')
        self.receive(changed, 100)
        self.assertEqual(self.cohort(99)['accepted_synthetic_call_intervals'], 1)
        self.assertEqual(self.cohort(100)['rejected_episodes'], 1)

    def test_source_correction_invalidates_old_acceptance(self):
        self.receive()
        changed = deepcopy(self.value)
        changed.update(revision=2, supersedes_revision=1, recorded_at='2020-10-01T12:00:00+08:00')
        changed['party_size'] = 3
        self.add_source(changed, 100)
        self.assertEqual(self.cohort(99)['accepted_synthetic_call_intervals'], 1)
        result = self.cohort(100)
        self.assertEqual(result['accepted_synthetic_call_intervals'], 0)
        self.assertEqual(result['stale_current_reviews'], 1)
        self.assertEqual(result['episodes_without_current_review'], 1)

    def test_new_review_of_stale_source_revision_is_rejected(self):
        changed = deepcopy(self.value)
        changed.update(revision=2, supersedes_revision=1, recorded_at='2020-10-01T12:00:00+08:00')
        self.add_source(changed, 5)
        with self.assertRaises(ReviewError) as caught:
            self.receive()
        self.assertEqual(caught.exception.error_code, 'review_target_not_latest')

    def test_idempotent_old_review_after_correction_keeps_first_receipt(self):
        self.receive()
        changed = self.accept()
        changed.update(revision=2, supersedes_revision=1, reviewed_at=stamp(80), decision='reject', reason_code='withdrawn')
        self.receive(changed, 100)
        before = self.review_path.read_bytes()
        result = self.receive(self.accept(), 200)
        self.assertTrue(result['idempotent'])
        self.assertEqual(before, self.review_path.read_bytes())

    def test_digest_change_is_rejected(self):
        changed = self.accept()
        changed['episode_receipt_sha256'] = '0'*64
        with self.assertRaises(ReviewError) as caught:
            self.receive(changed)
        self.assertEqual(caught.exception.error_code, 'review_target_changed')

    def test_missing_called_event_or_unknown_queue_cannot_be_accepted(self):
        for variant in ('missing', 'unknown'):
            value = deepcopy(self.value)
            value['episode_id'] = str(uuid4())
            if variant == 'missing':
                value['events'] = [value['events'][0]]
            else:
                value['queue_type'] = 'unknown'
            self.add_source(value, 2)
            review = self.draft(value)
            with self.subTest(variant=variant), self.assertRaises(ReviewError) as caught:
                self.receive(self.accept(review))
            self.assertEqual(caught.exception.error_code, 'review_acceptance_not_supported')

    def test_conflicting_reviewers_do_not_vote_an_acceptance(self):
        self.receive()
        changed = self.accept()
        changed.update(review_id=str(uuid4()), reviewer_id=str(uuid4()), decision='reject', reason_code='mismatch')
        self.receive(changed, 30)
        result = self.cohort(40)
        self.assertEqual(result['conflicting_review_episodes'], 1)
        self.assertEqual(result['accepted_synthetic_call_intervals'], 0)
        self.assertEqual(result['rejected_episodes'], 1)

    def test_insufficient_reviewer_blocks_other_acceptance(self):
        self.receive()
        changed = deepcopy(self.review)
        changed.update(review_id=str(uuid4()), reviewer_id=str(uuid4()))
        self.receive(changed, 30)
        result = self.cohort(40)
        self.assertEqual(result['accepted_synthetic_call_intervals'], 0)
        self.assertEqual(result['insufficient_evidence_episodes'], 1)

    def test_origin_and_review_time_binding_are_checked(self):
        for key, value in [('evidence_kind', 'self_observation_confirmation'), ('reviewed_at', stamp(-1))]:
            changed = self.accept()
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ReviewError):
                self.receive(changed)

    def test_strict_schema_checks_and_error_does_not_expose_input(self):
        for key, value in [('revision', True), ('reviewer_id', 'secret-private-marker'),
                ('decision', 'verified'), ('reviewed_at', stamp(1001)), ('called_time_bounds_checked', 1)]:
            changed = self.accept()
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ReviewError) as caught:
                validate_review(changed, now=BASE+timedelta(seconds=1000))
            self.assertNotIn('secret-private-marker', str(caught.exception))
        changed = self.accept()
        changed['phone'] = 'secret-private-marker'
        with self.assertRaises(ReviewError):
            validate_review(changed)

    def test_incomplete_checklist_is_not_acceptance(self):
        for key in ('issued_time_bounds_checked', 'called_time_bounds_checked', 'store_and_queue_checked'):
            changed = self.accept()
            changed[key] = False
            with self.subTest(key=key), self.assertRaises(ReviewError):
                self.receive(changed)

    def test_oversized_source_column_and_changed_payload_are_rejected(self):
        for column in ('episode_id', 'payload_json'):
            with OutcomeIntakeStore(self.source_path) as source:
                saved = source.db.execute(f'SELECT {column} FROM intake_revisions').fetchone()[0]
                source.db.execute(f'UPDATE intake_revisions SET {column}=?', ('x'*20000,))
                source.db.commit()
            with self.subTest(column=column), self.assertRaises(ReviewError):
                self.receive()
            with OutcomeIntakeStore(self.source_path) as source:
                source.db.execute(f'UPDATE intake_revisions SET {column}=?', (saved,))
                source.db.commit()

    def test_review_damage_is_rejected_for_append_and_report(self):
        self.receive()
        with OutcomeReviewStore(self.review_path) as store:
            store.db.execute("UPDATE outcome_reviews SET review_id=?", ('x'*20000,))
            store.db.commit()
        with self.assertRaises(ReviewError):
            self.cohort()
        with self.assertRaises(ReviewError):
            self.receive(second=30)

    def test_revision_jump_and_reviewer_change_are_rejected(self):
        self.receive()
        for key, value in [('revision', 3), ('reviewer_id', str(uuid4()))]:
            changed = self.accept()
            changed.update(revision=2, supersedes_revision=1, reviewed_at=stamp(25))
            changed[key] = value
            if key == 'revision':
                changed['supersedes_revision'] = 2
            with self.subTest(key=key), self.assertRaises(ReviewError):
                self.receive(changed, 30)

    def test_read_only_report_never_modifies_files(self):
        self.receive()
        before = self.source_path.read_bytes(), self.review_path.read_bytes()
        self.cohort()
        self.assertEqual(before, (self.source_path.read_bytes(), self.review_path.read_bytes()))

    def test_complete_bound_audit_does_not_hide_other_source_revisions(self):
        self.receive()
        value = deepcopy(self.value)
        value['episode_id'] = str(uuid4())
        self.add_source(value, 2)
        with self.assertRaises(ReviewError) as caught:
            self.cohort(max_revisions=1)
        self.assertEqual(caught.exception.error_code, 'review_limit_exceeded')

    def test_private_reader_rejects_permissions_and_duplicate_keys(self):
        self.draft_path.chmod(0o644)
        with self.assertRaises(ReviewError):
            read_review(self.draft_path)
        self.draft_path.chmod(0o600)
        self.draft_path.write_text('{"revision":1,"revision":2}')
        with self.assertRaises(ReviewError):
            read_review(self.draft_path)

    def test_draft_does_not_replace_existing_file(self):
        before = self.draft_path.read_bytes()
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, patch('sushiwait.reviews._clock', return_value=BASE+timedelta(seconds=10)):
            with self.assertRaises(ReviewError) as caught:
                draft_review(source, self.value, self.draft_path)
        self.assertEqual(caught.exception.error_code, 'review_output_exists')
        self.assertEqual(before, self.draft_path.read_bytes())

    def test_commit_durability_failure_reports_committed(self):
        with patch('sushiwait.reviews.os.fsync', side_effect=OSError('secret-private-marker')):
            with self.assertRaises(ReviewError) as caught:
                self.receive()
        self.assertEqual(caught.exception.commit_status, 'committed')
        self.assertEqual(self.cohort()['accepted_synthetic_call_intervals'], 1)

    def test_clock_rollback_rejects_new_review_without_changing_files(self):
        self.receive(second=50)
        before = self.review_path.read_bytes()
        changed = self.accept()
        changed.update(review_id=str(uuid4()), reviewer_id=str(uuid4()))
        with self.assertRaises(ReviewError):
            self.receive(changed, second=30)
        self.assertEqual(before, self.review_path.read_bytes())

    def test_post_publication_draft_durability_failure_is_explicit(self):
        self.draft_path.unlink()
        output = io.StringIO()
        with patch('sushiwait.packets.os.fsync', side_effect=[None,None,OSError('secret-private-marker')]), \
                contextlib.redirect_stdout(output):
            code = main(['outcome-review-draft','--source-db',str(self.source_path),'--synthetic-fixture',str(fixtures.FIXTURE),
                '--output',str(self.draft_path)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertTrue(result['committed'])
        self.assertFalse(result['durability_confirmed'])
        self.assertFalse(result['ok'])
        self.assertTrue(self.draft_path.exists())
        self.assertNotIn('secret-private-marker', output.getvalue())

    def test_invalid_scope_and_read_write_connection_are_rejected(self):
        self.receive()
        for option in ({'max_revisions':True}, {'max_revisions':0}, {'api_profile':'crm_remote_v1_1'}):
            with self.subTest(option=option), self.assertRaises(ReviewError):
                self.cohort(**option)
        with OutcomeIntakeStore(self.source_path, read_only=True) as source, OutcomeReviewStore(self.review_path) as store:
            with self.assertRaises(ReviewError) as caught:
                store.cohort(source=source, as_of=stamp(30), data_origin='synthetic', api_profile='miniapp_gateway')
            self.assertEqual(caught.exception.error_code, 'review_requires_idle_reader')

    def test_cli_three_paths_are_offline_and_do_not_log_identifiers(self):
        self.draft_path.unlink()
        output = io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('no network')) as sock, \
                patch('sushiwait.cli.client_for', side_effect=AssertionError('no auth')) as client, \
                patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('no credentials')) as auth, \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(['outcome-review-draft','--source-db',str(self.source_path),'--synthetic-fixture',str(fixtures.FIXTURE),
                '--output',str(self.draft_path)]), 0)
            review = self.accept(json.loads(self.draft_path.read_text()))
            self.draft_path.write_text(json.dumps(review))
            self.assertEqual(main(['outcome-review-receive','--source-db',str(self.source_path),'--db',str(self.review_path),
                '--input',str(self.draft_path)]), 0)
            before = self.source_path.read_bytes(), self.review_path.read_bytes()
            self.assertEqual(main(['outcome-reviewed-cohort','--source-db',str(self.source_path),'--db',str(self.review_path),
                '--as-of',datetime.now(timezone.utc).isoformat(),'--data-origin','synthetic','--api-profile','miniapp_gateway']), 0)
        self.assertEqual(before, (self.source_path.read_bytes(), self.review_path.read_bytes()))
        self.assertEqual((sock.call_count,client.call_count,auth.call_count), (0,0,0))
        reports = [json.loads(v) for v in output.getvalue().splitlines()]
        self.assertEqual(reports[-1]['accepted_synthetic_call_intervals'], 1)
        for secret in (review['review_id'], review['reviewer_id'], review['episode_id'], review['episode_receipt_sha256']):
            self.assertNotIn(secret, output.getvalue())


if __name__ == '__main__':
    unittest.main()
