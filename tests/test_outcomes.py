import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.outcomes import (OutcomeError, OutcomeStore, candidate_targets,
    public_summary, read_episode, validate_episode)
from sushiwait.storage import SnapshotStore


FIXTURE = Path(__file__).resolve().parents[1] / 'examples/fixtures/outcome-01.synthetic.json'
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def record():
    return json.loads(FIXTURE.read_text())


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.database = self.directory / 'outcomes.sqlite3'
        self.input = self.directory / 'result.json'

    def write(self, value):
        self.input.write_text(json.dumps(value))
        self.input.chmod(0o600)

    def cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('socket.socket', side_effect=AssertionError('no network')), \
                patch('socket.create_connection', side_effect=AssertionError('no network')), \
                patch('sushiwait.cli.SnapshotStore', side_effect=AssertionError('separate database')), \
                patch('sushiwait.cli.SushiroClient', side_effect=AssertionError('no credentials')):
            code = main(args)
        return code, json.loads(out.getvalue())

    def test_intervals_and_utc_preserve_observation_uncertainty(self):
        item = validate_episode(record(), now=NOW)
        targets = candidate_targets(item)
        self.assertEqual(targets['called_wait'], {'lower_seconds': 2400.0, 'upper_seconds': 2460.0})
        self.assertEqual(targets['called_to_seated'], {'lower_seconds': 240.0, 'upper_seconds': 300.0})
        self.assertEqual(item['events'][2]['event_time_lower'], '2020-10-01T02:40:00.000000Z')
        self.assertFalse(targets['training_eligible'])
        self.assertFalse(targets['authenticity_verified'])

    def test_unknown_private_fields_and_claims_are_rejected(self):
        for key in ('phone', 'ticket_number', 'authorization', 'headers', 'free_text'):
            item = record(); item[key] = 'do-not-print-private-marker'
            with self.subTest(key=key), self.assertRaisesRegex(OutcomeError, '^outcome_invalid_record$'):
                validate_episode(item, now=NOW)
        item = record(); item['events'][0]['verification_status'] = 'verified'
        with self.assertRaises(OutcomeError):
            validate_episode(item, now=NOW)

    def test_types_identity_and_bounds_reject_malformed_records(self):
        mutations = [('schema_version', True), ('revision', True), ('party_size', True),
            ('party_size', 0), ('party_size', 2**31), ('store_id', '001'), ('store_id', '13800000000'),
            ('api_profile', ['legacy']), ('data_origin', 'live'), ('episode_id', 'private-free-text'),
            ('queue_type', 'some-private-value'), ('supersedes_revision', 0)]
        for key, value in mutations:
            item = record(); item[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(OutcomeError):
                validate_episode(item, now=NOW)
        item = record(); item['party_size'] = None
        self.assertIsNone(validate_episode(item, now=NOW)['party_size'])

    def test_invalid_unzoned_future_and_unobserved_times_stop(self):
        for value in ('2020-10-01T11:00:00', 'bad-secret-date', '2020-10-01T11:00:00+00:60'):
            item = record(); item['recorded_at'] = value
            with self.subTest(value=value), self.assertRaisesRegex(OutcomeError, '^outcome_invalid_time$'):
                validate_episode(item, now=NOW)
        item = record(); item['recorded_at'] = '2099-01-01T00:00:00Z'
        with self.assertRaisesRegex(OutcomeError, 'outcome_future_record'):
            validate_episode(item, now=NOW)
        item = record(); item['events'][2]['observed_at'] = item['events'][2]['event_time_lower']
        with self.assertRaisesRegex(OutcomeError, 'outcome_time_order'):
            validate_episode(item, now=NOW)

    def test_event_identity_order_and_single_attempt_terminal_rules(self):
        item = record(); item['events'][2]['event_id'] = item['events'][0]['event_id']
        with self.assertRaises(OutcomeError): validate_episode(item, now=NOW)
        item = record(); item['events'][2]['event_type'] = 'issued'
        with self.assertRaises(OutcomeError): validate_episode(item, now=NOW)
        item = record(); item['events'] = list(reversed(item['events']))
        with self.assertRaises(OutcomeError): validate_episode(item, now=NOW)
        item = record(); item['events'][1]['event_type'] = 'cancelled'
        with self.assertRaises(OutcomeError): validate_episode(item, now=NOW)

    def test_overlapping_uncertainty_can_still_have_consistent_event_order(self):
        item = record(); item['events'] = [item['events'][0], item['events'][2]]
        item['events'][0]['event_time_upper'] = '2020-10-01T10:41:00+08:00'
        item['events'][0]['observed_at'] = '2020-10-01T10:41:00+08:00'
        targets = candidate_targets(validate_episode(item, now=NOW))
        self.assertEqual(targets['called_wait']['lower_seconds'], 0)
        self.assertEqual(targets['called_wait']['upper_seconds'], 2460)

    def test_cancel_or_no_show_does_not_fabricate_call_and_censor_is_separate(self):
        for terminal in ('cancelled', 'no_show', 'observation_ended'):
            item = record(); item['events'] = [item['events'][0], item['events'][-1]]
            item['events'][-1]['event_type'] = terminal
            target = candidate_targets(validate_episode(item, now=NOW))
            with self.subTest(terminal=terminal):
                self.assertIsNone(target['called_wait'])
                self.assertEqual(target['right_censored_without_call'], terminal == 'observation_ended')

    def test_private_reader_permissions_and_explicit_synthetic_origin(self):
        item = record(); item['data_origin'] = 'self_reported'
        for event in item['events']: event['evidence_kind'] = 'self_observation'
        self.write(item)
        self.assertEqual(read_episode(self.input, now=NOW)['data_origin'], 'self_reported')
        with self.assertRaisesRegex(OutcomeError, '^outcome_fixture_origin$'):
            read_episode(self.input, synthetic=True, now=NOW)
        self.input.chmod(0o644)
        with self.assertRaisesRegex(OutcomeError, '^outcome_input_unsafe$'):
            read_episode(self.input, now=NOW)

    def test_input_size_duplicate_keys_and_json_errors_never_echo_payload(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"private":"do-not-print-private-marker"', b'{"x":NaN}'):
            self.input.write_bytes(raw); self.input.chmod(0o600)
            with self.subTest(raw=raw):
                code, result = self.cli(['outcome-check', '--input', str(self.input)])
                self.assertEqual(code, 1)
                self.assertEqual(result['error_code'], 'outcome_invalid_json')
                self.assertNotIn('do-not-print-private-marker', json.dumps(result))
        self.input.write_bytes(b' ' * (16384 + 1))
        with self.assertRaisesRegex(OutcomeError, 'outcome_input_too_large'):
            read_episode(self.input, now=NOW)

    def test_check_does_not_create_storage_or_print_identifiers_or_times(self):
        code, result = self.cli(['outcome-check', '--synthetic-fixture', str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertFalse(self.database.exists())
        self.assertEqual(result['event_count'], 4)
        self.assertFalse(result['revision_order_verified'])
        for private in (record()['episode_id'], '900001', '2020-10-01', '10:40'):
            self.assertNotIn(private, json.dumps(result))

    def test_import_is_idempotent_and_keeps_all_revisions(self):
        with OutcomeStore(self.database) as store:
            self.assertTrue(store.append(record(), now=NOW)['committed'])
            same = store.append(record(), now=NOW)
            self.assertFalse(same['committed']); self.assertTrue(same['idempotent'])
            item = record(); item['revision'] = 2; item['supersedes_revision'] = 1
            item['party_size'] = 3
            self.assertTrue(store.append(item, now=NOW)['committed'])
            self.assertEqual(store.db.execute('SELECT revision FROM episodes ORDER BY id').fetchall(), [(1,), (2,)])
            report = store.report()
            self.assertEqual(report['window']['total_revisions'], 2)
            self.assertEqual(report['window']['total_episodes'], 1)
            self.assertEqual(report['origins'], {'synthetic': 1})

    def test_revision_replay_gaps_conflict_and_identity_change_roll_back(self):
        with OutcomeStore(self.database) as store:
            store.append(record(), now=NOW)
            for key, value in (('party_size', 3), ('revision', 3)):
                item = record(); item[key] = value
                if key == 'revision': item['supersedes_revision'] = 2
                with self.assertRaises(OutcomeError): store.append(item, now=NOW)
            for key, value in (('store_id', '900002'), ('api_profile', 'legacy'), ('queue_type', 'reservation')):
                item = record(); item['revision'] = 2; item['supersedes_revision'] = 1; item[key] = value
                with self.assertRaisesRegex(OutcomeError, 'outcome_episode_conflict'):
                    store.append(item, now=NOW)
            self.assertEqual(store.report()['window']['total_revisions'], 1)

    def test_filesystem_change_before_commit_rolls_back_insert(self):
        with OutcomeStore(self.database) as store:
            with patch.object(store, '_guard', side_effect=[None, OutcomeError('outcome_database_changed')]):
                with self.assertRaises(OutcomeError): store.append(record(), now=NOW)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM episodes').fetchone()[0], 0)

    def test_separate_database_rejects_public_snapshot_schema_without_modification(self):
        with SnapshotStore(self.database): pass
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(OutcomeError, 'outcome_database_schema'):
            OutcomeStore(self.database)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_private_storage_refuses_symlinks_hardlinks_and_public_parent(self):
        with OutcomeStore(self.database): pass
        link = self.directory / 'link'; link.symlink_to(self.database)
        with self.assertRaisesRegex(OutcomeError, 'outcome_database_unsafe'): OutcomeStore(link)
        hard = self.directory / 'hard'; hard.hardlink_to(self.database)
        with self.assertRaisesRegex(OutcomeError, 'outcome_database_unsafe'): OutcomeStore(self.database)
        hard.unlink(); self.directory.chmod(0o755)
        with self.assertRaisesRegex(OutcomeError, 'outcome_database_unsafe'): OutcomeStore(self.database)

    def test_directory_lock_serializes_cooperating_writers(self):
        with OutcomeStore(self.database) as first:
            with self.assertRaisesRegex(OutcomeError, 'outcome_database_busy'): OutcomeStore(self.database)
            first.append(record(), now=NOW)

    def test_readonly_report_filters_bad_payload_and_preserves_database_bytes(self):
        with OutcomeStore(self.database) as store:
            store.append(record(), now=NOW)
            store.db.execute("UPDATE episodes SET payload_json=?", ('{"private":"do-not-print-private-marker"}',))
            store.db.commit()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with OutcomeStore(self.database, read_only=True) as store:
            result = store.report()
            self.assertEqual(result['invalid_latest_records'], 1)
            self.assertEqual(result['origins'], {})
            self.assertNotIn('do-not-print-private-marker', json.dumps(result))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_report_uses_latest_episodes_and_separates_synthetic_self_reports(self):
        with OutcomeStore(self.database) as store:
            store.append(record(), now=NOW)
            item = record(); item['episode_id'] = '803fa5bd-8f4f-47b3-b42f-6f57e758df84'
            item['data_origin'] = 'self_reported'
            for event in item['events']: event['evidence_kind'] = 'self_observation'
            store.append(item, now=NOW)
            report = store.report(limit=1)
            self.assertTrue(report['window']['truncated'])
            self.assertEqual(report['origins'], {'self_reported': 1})
            self.assertEqual(report['verified_training_labels'], 0)
            self.assertFalse(report['eta_available'])

    def test_cli_import_report_are_local_and_invalid_input_creates_no_database(self):
        item = record(); item['phone'] = 'do-not-print-private-marker'; self.write(item)
        code, result = self.cli(['outcome-import', '--input', str(self.input), '--db', str(self.database)])
        self.assertEqual(code, 1); self.assertFalse(self.database.exists())
        self.assertNotIn('do-not-print-private-marker', json.dumps(result))
        code, result = self.cli(['outcome-import', '--synthetic-fixture', str(FIXTURE), '--db', str(self.database)])
        self.assertEqual(code, 0); self.assertTrue(result['committed'])
        code, report = self.cli(['outcome-report', '--db', str(self.database)])
        self.assertEqual(code, 0); self.assertEqual(report['origins'], {'synthetic': 1})
        self.assertFalse(report['network_performed'])

    def test_report_bounds_are_validated(self):
        with OutcomeStore(self.database) as store:
            for value in (0, True, 10001):
                with self.subTest(value=value), self.assertRaisesRegex(OutcomeError, 'outcome_invalid_limit'):
                    store.report(limit=value)

    def test_tampered_uniqueness_index_is_rejected(self):
        with OutcomeStore(self.database) as store:
            store.db.execute('DROP INDEX outcome_episode_revision')
            store.db.execute('CREATE INDEX outcome_episode_revision ON episodes(episode_id,revision)')
            store.db.commit()
        with self.assertRaisesRegex(OutcomeError, 'outcome_database_schema'):
            OutcomeStore(self.database)

    def test_readonly_cannot_append(self):
        with OutcomeStore(self.database): pass
        with OutcomeStore(self.database, read_only=True) as store:
            with self.assertRaisesRegex(OutcomeError, 'outcome_database_error'):
                store.append(record(), now=NOW)


if __name__ == '__main__':
    unittest.main()
