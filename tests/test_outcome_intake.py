"""First receipt remains separate from caller claims and retrospective labels."""
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
from sushiwait.intake import IntakeError, OutcomeIntakeStore
from sushiwait.outcomes import OutcomeStore
from test_outcomes import FIXTURE, record

BASE=datetime(2026,10,6,4,30,tzinfo=timezone.utc)


def stamp(seconds):return (BASE+timedelta(seconds=seconds)).isoformat()


def correction():
    value=record();value.update(revision=2,supersedes_revision=1,recorded_at='2020-10-01T12:00:00+08:00')
    event=deepcopy(value['events'][-1]);event['event_type']='cancelled'
    value['events']=[value['events'][0],event]
    return value


class OutcomeIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name).resolve();self.parent.chmod(0o700)
        self.path=self.parent/'received.sqlite3'

    def append(self, value=None, second=0):
        with OutcomeIntakeStore(self.path) as db, patch('sushiwait.intake._clock',return_value=BASE+timedelta(seconds=second)):
            return db.append(record() if value is None else value)

    def cohort(self, second=60, **options):
        with OutcomeIntakeStore(self.path,read_only=True) as db, patch('sushiwait.intake._clock',return_value=BASE+timedelta(seconds=1000)):
            return db.cohort(as_of=stamp(second),data_origin='synthetic',api_profile='miniapp_gateway',**options)

    def test_claimed_2020_record_is_unavailable_before_2026_first_receipt(self):
        result=self.append(second=100)
        self.assertTrue(result['committed']);self.assertTrue(result['durability_confirmed'])
        self.assertEqual(self.cohort(99)['selected_episodes'],0)
        report=self.cohort(100)
        self.assertEqual(report['selected_episodes'],1)
        self.assertEqual(report['unverified_called_wait_candidates'],1)
        self.assertEqual(report['availability_basis'],'local_first_receipt_time')
        self.assertFalse(report['historical_availability_verified'])
        self.assertFalse(report['durable_availability_verified'])
        self.assertFalse(report['training_eligible']);self.assertEqual(report['verified_training_labels'],0)

    def test_correction_uses_receipt_time_and_preserves_earlier_version(self):
        self.append(second=0);self.append(correction(),second=100)
        self.assertEqual(self.cohort(99)['terminal_states'],{'seated':1})
        self.assertEqual(self.cohort(100)['terminal_states'],{'cancelled':1})
        self.assertEqual(self.cohort(100)['unverified_called_wait_candidates'],0)

    def test_repeating_older_revision_after_correction_preserves_first_receipt(self):
        self.append(second=0);self.append(correction(),second=100)
        before=hashlib.sha256(self.path.read_bytes()).digest()
        result=self.append(record(),second=200)
        self.assertTrue(result['idempotent']);self.assertFalse(result['committed'])
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).digest())
        self.assertEqual(self.cohort(99)['terminal_states'],{'seated':1})
        self.assertEqual(self.cohort(200)['revisions_audited'],2)

    def test_same_revision_different_contents_is_not_a_new_receipt(self):
        self.append()
        changed=record();changed['party_size']=3
        with self.assertRaises(IntakeError):self.append(changed,second=100)
        self.assertEqual(self.cohort()['revisions_audited'],1)

    def test_input_cannot_supply_receipt_or_verified_status(self):
        for change in ('received_at','receipt_sha256'):
            value=record();value[change]='secret-receipt-marker'
            with self.subTest(change=change),self.assertRaises(IntakeError):self.append(value)
        value=record();value['events'][0]['verification_status']='verified'
        with self.assertRaises(IntakeError):self.append(value)

    def test_revision_jump_store_scope_change_and_claim_time_rollback_stop(self):
        self.append()
        variants=[]
        value=correction();value.update(revision=3,supersedes_revision=2);variants.append(value)
        value=correction();value['store_id']='900002';variants.append(value)
        value=correction();value['recorded_at']='2020-10-01T10:50:00+08:00';variants.append(value)
        for value in variants:
            with self.subTest(value=value['recorded_at']),self.assertRaises(IntakeError):self.append(value,second=100)
        self.assertEqual(self.cohort()['revisions_audited'],1)

    def test_global_clock_rollback_refuses_new_episode(self):
        self.append(second=100)
        value=record();value['episode_id']=str(uuid4())
        with self.assertRaises(IntakeError):self.append(value,second=99)
        self.assertEqual(self.cohort(200)['revisions_audited'],1)

    def test_equal_receipt_times_choose_higher_revision(self):
        self.append(second=0);self.append(correction(),second=0)
        self.assertEqual(self.cohort(0)['terminal_states'],{'cancelled':1})

    def test_receipt_time_and_payload_tampering_are_detected(self):
        self.append()
        with OutcomeIntakeStore(self.path) as db:
            db.db.execute('UPDATE intake_revisions SET received_at=?',(stamp(30),));db.db.commit()
        with self.assertRaises(IntakeError):self.cohort()

    def test_malformed_earlier_revision_stops_whole_audit(self):
        self.append();self.append(correction(),second=100)
        with OutcomeIntakeStore(self.path) as db:
            db.db.execute("UPDATE intake_revisions SET payload_json='{}' WHERE revision=1");db.db.commit()
        with self.assertRaises(IntakeError):self.cohort(50)

    def test_full_audit_limit_does_not_drop_future_corrections(self):
        self.append();self.append(correction(),second=100)
        with self.assertRaises(IntakeError):self.cohort(50,max_revisions=1)
        self.assertEqual(self.cohort(50,max_revisions=2)['selected_episodes'],1)

    def test_reader_does_not_rollback_callers_existing_transaction(self):
        self.append()
        with OutcomeIntakeStore(self.path,read_only=True) as db:
            db.db.execute('BEGIN')
            with self.assertRaises(IntakeError):
                db.cohort(as_of=stamp(0),data_origin='synthetic',api_profile='miniapp_gateway')
            self.assertTrue(db.db.in_transaction);db.db.rollback()

    def test_invalid_bounds_time_and_origin_do_not_modify_database(self):
        self.append();before=hashlib.sha256(self.path.read_bytes()).digest()
        for extra in ({'max_revisions':0},{'max_revisions':True}):
            with self.assertRaises(IntakeError):self.cohort(**extra)
        with OutcomeIntakeStore(self.path,read_only=True) as db:
            for at,origin in (('2026-10-06','synthetic'),('2099-01-01T00:00:00Z','synthetic'),(stamp(0),'live')):
                with self.assertRaises(IntakeError):db.cohort(as_of=at,data_origin=origin,api_profile='miniapp_gateway')
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).digest())

    def test_readonly_projection_preserves_private_output_and_database(self):
        self.append();before=hashlib.sha256(self.path.read_bytes()).digest()
        report=self.cohort()
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).digest())
        text=json.dumps(report)
        for marker in (record()['episode_id'],record()['store_id'],str(self.parent),record()['events'][0]['event_id']):
            self.assertNotIn(marker,text)
        self.assertTrue(report['output_requires_private_handling']);self.assertFalse(report['authenticity_verified'])

    def test_legacy_outcome_database_is_rejected_without_migration(self):
        with OutcomeStore(self.path) as db:db.append(record())
        before=hashlib.sha256(self.path.read_bytes()).digest()
        with self.assertRaises(IntakeError):OutcomeIntakeStore(self.path,read_only=True)
        with self.assertRaises(IntakeError):OutcomeIntakeStore(self.path)
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).digest())

    def test_unsafe_database_mode_and_symlink_are_rejected(self):
        self.append();self.path.chmod(0o644)
        with self.assertRaises(IntakeError):OutcomeIntakeStore(self.path,read_only=True)
        self.path.chmod(0o600)
        saved=self.path.with_suffix('.saved');self.path.rename(saved);self.path.symlink_to(saved)
        with self.assertRaises(IntakeError):OutcomeIntakeStore(self.path,read_only=True)

    def test_failed_durability_check_reports_already_committed(self):
        with OutcomeIntakeStore(self.path) as db,patch('sushiwait.intake._clock',return_value=BASE), \
                patch('sushiwait.intake.os.fsync',side_effect=OSError('secret-durability-marker')):
            with self.assertRaises(IntakeError) as error:db.append(record())
            self.assertEqual(error.exception.commit_status,'committed')
            self.assertNotIn('secret-durability-marker',str(error.exception))
            self.assertEqual(db.db.execute('SELECT COUNT(*) FROM intake_revisions').fetchone()[0],1)
        self.assertTrue(self.append(second=100)['idempotent'])

    def test_commit_exception_has_unknown_status_even_if_sqlite_stored_row(self):
        class Proxy:
            def __init__(self,wrapped):self.wrapped=wrapped
            def __getattr__(self,name):return getattr(self.wrapped,name)
            def commit(self):self.wrapped.commit();raise OSError('secret-commit-marker')
        with OutcomeIntakeStore(self.path) as db,patch('sushiwait.intake._clock',return_value=BASE):
            db.db=Proxy(db.db)
            with self.assertRaises(IntakeError) as error:db.append(record())
            self.assertEqual(error.exception.commit_status,'unknown')
        self.assertTrue(self.append(second=100)['idempotent'])

    def test_two_cli_paths_are_offline_and_do_not_read_credentials_or_native_ui(self):
        common=['--db',str(self.path)]
        reports=[]
        with contextlib.ExitStack() as stack:
            for target in ('socket.socket','socket.create_connection','sushiwait.cli.read_credentials_file',
                           'sushiwait.cli.SnapshotStore','sushiwait.surgeguard._command','subprocess.Popen'):
                stack.enter_context(patch(target,side_effect=AssertionError('unexpected external operation')))
            for args in (['outcome-receive','--synthetic-fixture',str(FIXTURE)]+common,
                         ['outcome-received-cohort','--as-of',datetime.now(timezone.utc).isoformat(),
                          '--data-origin','synthetic','--api-profile','miniapp_gateway']+common):
                if args[0]=='outcome-received-cohort':
                    args[args.index('--as-of')+1]=datetime.now(timezone.utc).isoformat()
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(main(args),0)
                reports.append(json.loads(output.getvalue()))
        self.assertEqual(reports[1]['selected_episodes'],1)
        self.assertFalse(reports[1]['training_eligible'])

    def test_bad_cli_input_is_rejected_before_database_creation(self):
        p=self.parent/'input.json';value=record();value['received_at']='private-receipt-marker'
        p.write_text(json.dumps(value));p.chmod(0o600);output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['outcome-receive','--input',str(p),'--db',str(self.path)]),1)
        self.assertFalse(self.path.exists());self.assertNotIn('private-receipt-marker',output.getvalue())
