"""Browser drafts retain private, unverified first-receipt semantics."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
import contextlib, io, json, shutil, subprocess, tempfile, unittest
from unittest.mock import patch
from sushiwait.cli import main
from sushiwait.experiencebundle import validate_bundle, receive_bundle, ExperienceBundleError
from sushiwait.intake import OutcomeIntakeStore, IntakeError

BASE=datetime(2026,10,9,4,tzinfo=timezone.utc)

class ExperienceBundleTests(unittest.TestCase):
    def setUp(self):
        node=shutil.which('node')
        if node is None:self.skipTest('Node unavailable')
        root=Path(__file__).resolve().parents[1]
        self.command=[node,str(root/'tests/experience-model.test.js'),str(root/'src/sushiwait/web/experience-model.js')]
        result=subprocess.run(self.command+['--fixture'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.value=json.loads(result.stdout)
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.root.chmod(0o700);self.input=self.root/'bundle.json'
        self.input.write_text(json.dumps(self.value));self.input.chmod(0o600);self.database=self.root/'intake.sqlite3'

    def test_browser_math_and_outcome_compatibility(self):
        result=subprocess.run(self.command,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        records=validate_bundle(self.value,now=BASE,synthetic=True)
        self.assertEqual([r['revision'] for r in records],[1,2,3]);self.assertTrue(all(e['verification_status']=='unverified' for e in records[-1]['events']))

    def test_browser_private_controls_and_import_race(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([self.command[0],str(root/'tests/experience-ui.test.js'),
            str(root/'src/sushiwait/web/experience-model.js'),str(root/'src/sushiwait/web/experience-ui.js')],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_receipt_is_local_import_time_and_duplicates_preserve_it(self):
        before=self.input.read_bytes()
        with patch('sushiwait.intake._clock',return_value=BASE),patch('socket.socket',side_effect=AssertionError('network')):
            first=receive_bundle(self.input,database=self.database,synthetic=True)
        with OutcomeIntakeStore(self.database,read_only=True) as intake:
            rows=intake.db.execute('SELECT received_at FROM intake_revisions ORDER BY id').fetchall()
        self.assertEqual(len(rows),3);self.assertTrue(all(r[0]=='2026-10-09T04:00:00.000000Z' for r in rows))
        with patch('sushiwait.intake._clock',return_value=BASE+timedelta(days=1)):
            again=receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertEqual(first['new_revisions'],3);self.assertEqual(again['duplicate_revisions'],3);self.assertEqual(again['new_revisions'],0)
        with OutcomeIntakeStore(self.database,read_only=True) as intake:
            self.assertEqual(intake.db.execute('SELECT received_at FROM intake_revisions ORDER BY id').fetchall(),rows)
        self.assertEqual(self.input.read_bytes(),before);self.assertEqual(first['verified_training_labels'],0)

    def test_all_revisions_validate_before_any_writer(self):
        for mutate in [lambda d:d['revisions'].pop(0),lambda d:d['revisions'][1].update(party_size=2),lambda d:d.update(exported_at='2027-01-01T00:00:00Z'),lambda d:d['revisions'][1]['events'][0].update(event_time_lower='2026-10-09T03:01:00Z')]:
            value=deepcopy(self.value);mutate(value);self.input.write_text(json.dumps(value))
            with self.subTest(mutate=mutate),patch('sushiwait.experiencebundle.OutcomeIntakeStore',side_effect=AssertionError('writer opened')),self.assertRaises(ExperienceBundleError):
                receive_bundle(self.input,database=self.database,synthetic=True)
            self.assertFalse(self.database.exists())

    def test_wrong_origin_unsafe_file_and_duplicate_json_keys_rejected(self):
        with self.assertRaises(ExperienceBundleError):receive_bundle(self.input,database=self.database)
        self.input.chmod(0o644)
        with self.assertRaises(ExperienceBundleError):receive_bundle(self.input,database=self.database,synthetic=True)
        self.input.chmod(0o600);self.input.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ExperienceBundleError):receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertFalse(self.database.exists())

    def test_partial_prefix_is_preserved_for_idempotent_retry(self):
        original=OutcomeIntakeStore.append;count=0
        def append(store,value):
            nonlocal count
            count+=1
            if count==2:raise IntakeError('outcome_intake_operation_failed')
            return original(store,value)
        with patch.object(OutcomeIntakeStore,'append',append),self.assertRaises(ExperienceBundleError) as raised:
            receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertEqual(raised.exception.saved,1)
        result=receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertEqual(result['duplicate_revisions'],1);self.assertEqual(result['new_revisions'],2)

    def test_committed_but_unconfirmed_append_is_not_rolled_back(self):
        original=OutcomeIntakeStore.append;count=0
        def append(store,value):
            nonlocal count
            count+=1
            result=original(store,value)
            if count==2:raise IntakeError('outcome_intake_operation_failed',commit_status='committed')
            return result
        with patch.object(OutcomeIntakeStore,'append',append),self.assertRaises(ExperienceBundleError) as raised:
            receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertEqual(raised.exception.saved,1)
        self.assertEqual(raised.exception.commit_status,'committed')
        result=receive_bundle(self.input,database=self.database,synthetic=True)
        self.assertEqual(result['duplicate_revisions'],2);self.assertEqual(result['new_revisions'],1)

    def test_cli_summary_contains_no_personal_identifiers_or_times(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['experience-bundle-receive','--input',str(self.input),'--database',str(self.database),'--synthetic']),0)
        summary=output.getvalue();self.assertNotIn(str(self.root),summary);self.assertNotIn(self.value['revisions'][0]['episode_id'],summary);self.assertNotIn('2026-10-09',summary)
        self.assertFalse(json.loads(summary)['review_accepted'])
