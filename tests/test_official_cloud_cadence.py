"""Do not restart away the very process whose first expiry is being tested."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import contextlib
import io
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('cloud_cadence',
    Path(__file__).resolve().parents[1]/'deploy'/'official_cloud_cadence.py')
cadence=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(cadence)


class OfficialCloudCadenceTests(unittest.TestCase):
    def setUp(self):
        self.before={'revision':1,'expiry':'2026-10-12T06:58:44Z',
            'processes':{'parent':{'pid':10,'start_ticks':123,'parent_pid':1},
                         'reader':{'pid':11,'start_ticks':124,'parent_pid':10}},
            'task':{'completed_slots':9}}
        self.after={'revision':2,'remaining_seconds':3300,'processes':deepcopy(self.before['processes']),
            'task':{'credential_revision':2,'interval_seconds':900,'pending_attempt':False,
                    'failed_slots':0,'uncertain_slots':0,'completed_slots':15},
            'latest_success':{s:'2026-10-12T06:58:45Z' for s in cadence.STORES}}
        self.now=datetime(2026,10,12,7,1,tzinfo=timezone.utc)

    def test_same_reader_and_each_store_after_true_first_expiry_accepts_proof(self):
        proof=cadence.prove(self.before,self.after,now=self.now)
        self.assertTrue(proof['original_reader_crossed_declared_expiry'])
        self.assertEqual(proof['accepted_revision'],2)

    def test_same_pid_reused_or_reader_restarted_is_rejected(self):
        for actor in ('parent','reader'):
            changed=deepcopy(self.after);changed['processes'][actor]['start_ticks']+=1
            with self.subTest(actor=actor),self.assertRaisesRegex(ValueError,'reader_changed'):
                cadence.prove(self.before,changed,now=self.now)

    def test_revision_bump_alone_or_one_store_not_crossing_is_insufficient(self):
        for store in cadence.STORES:
            changed=deepcopy(self.after);changed['latest_success'][store]=self.before['expiry']
            with self.subTest(store=store),self.assertRaisesRegex(ValueError,'proof_incomplete'):
                cadence.prove(self.before,changed,now=self.now)

    def test_failure_unknown_pending_and_unread_revision_cannot_switch(self):
        for key,value in (('failed_slots',1),('uncertain_slots',1),('pending_attempt',True),
                          ('credential_revision',1),('completed_slots',9)):
            changed=deepcopy(self.after);changed['task'][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'proof_incomplete'):
                cadence.prove(self.before,changed,now=self.now)

    def test_fixed_single_switch_window_does_not_run_early_or_after_restart_tomorrow(self):
        for at in (cadence.START-timedelta(seconds=1),cadence.END,cadence.START+timedelta(days=1)):
            with self.subTest(at=at),self.assertRaisesRegex(ValueError,'proof_timing'):
                cadence.prove(self.before,self.after,now=at)

    def test_failed_proof_never_stops_service_or_installs_policy(self):
        changed=deepcopy(self.after);changed['revision']=1
        with patch.object(cadence,'snapshot',return_value=changed), \
             patch.object(cadence,'_read_private_file',return_value=json.dumps(self.before).encode()), \
             patch.object(cadence,'command') as command,patch.object(cadence,'publish') as publish, \
             patch.object(cadence.sys,'argv',['cadence','switch']), \
             patch.object(cadence,'datetime') as clock,contextlib.redirect_stdout(io.StringIO()) as output:
            clock.now.return_value=self.now;clock.fromisoformat.side_effect=datetime.fromisoformat
            self.assertEqual(cadence.main(),1)
        self.assertEqual(json.loads(output.getvalue())['error_code'],'cadence_expiry_proof_incomplete')
        command.assert_not_called();publish.assert_not_called()

    def test_baseline_publication_is_exclusive_and_really_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();root.chmod(0o700)
            with patch.object(cadence,'ROOT',root):
                cadence.publish(self.before,'baseline.json')
                self.assertEqual(json.loads((root/'baseline.json').read_text()),self.before)
                self.assertEqual((root/'baseline.json').stat().st_mode&0o777,0o600)
                with self.assertRaises(ValueError):cadence.publish(self.before,'baseline.json')
