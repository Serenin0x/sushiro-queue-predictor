"""Cold-process trial cannot reset budget or confuse service start with recovery."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cloud_recovery',
    Path(__file__).resolve().parents[1]/'deploy'/'official_cloud_recovery.py')
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class OfficialCloudRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'at': '2026-10-12T07:43:00+00:00', 'revision': 2,
            'expiry': '2026-10-12T07:57:15Z', 'remaining_seconds': 855,
            'ordinary_used': 30, 'renewal_attempts': 2,
            'policy': {'interval_seconds': 600, 'ordinary_query_limit': 396},
            'ledger_sha256': 'a'*64, 'context_sha256': 'b'*64,
            'units': {recovery.TRIAL: {'pid': 10, 'active': 'active', 'sub': 'running', 'result': 'success'},
                      recovery.SYNC: {'pid': 12, 'active': 'active', 'sub': 'running', 'result': 'success'},
                      recovery.SUPPLIER: {'pid': 0, 'active': 'active', 'sub': 'exited', 'result': 'success'}},
            'container': {'id': 'c'*64, 'pid': 15, 'started': '2026-10-12T05:00:00Z', 'running': True},
            'task': {'interval_seconds': 600, 'credential_revision': 2, 'state': 'ready',
                     'stop_reason': None,
                     'pending_attempt': False, 'failed_slots': 0, 'uncertain_slots': 0,
                     'completed_slots': 15, 'target_slots': 198, 'target_rounds': 66},
            'latest_success': {s: '2026-10-12T07:41:03Z' for s in ('3014', '3004', '2009')}}
        self.proof = {'original_reader_crossed_declared_expiry': True, 'accepted_revision': 2}

    def recovered_states(self):
        before = deepcopy(self.state)
        cold = deepcopy(before)
        cold['units'][recovery.TRIAL].update(pid=0, active='inactive', sub='dead')
        cold['container'].update(running=False, pid=0)
        cold['units'][recovery.SUPPLIER].update(active='inactive', sub='dead')
        after = deepcopy(before)
        after['units'][recovery.TRIAL]['pid'] = 20
        after['container'].update(pid=25, started='2026-10-12T07:43:05Z')
        current = deepcopy(after)
        current.update(at='2026-10-12T08:03:10+00:00', revision=3, renewal_attempts=3,
                       context_sha256='d'*64, ledger_sha256='e'*64, remaining_seconds=3300,
                       ordinary_used=36)
        current['task'].update(credential_revision=3, completed_slots=21)
        current['latest_success'] = {s: '2026-10-12T08:03:05Z' for s in before['latest_success']}
        return before, cold, after, current

    def test_only_safe_checkpoint_inside_fixed_once_window_can_restart(self):
        recovery.preflight(self.state, self.proof, now=recovery.START)
        for at in (recovery.START-timedelta(seconds=1), recovery.END,
                   recovery.START+timedelta(days=1)):
            with self.subTest(at=at), self.assertRaisesRegex(ValueError, 'window_closed'):
                recovery.preflight(self.state, self.proof, now=at)

    def test_pending_unknown_failure_exhaustion_and_near_expiry_are_rejected(self):
        for key, value in (('pending_attempt', True), ('uncertain_slots', 1), ('failed_slots', 1),
                           ('credential_revision', 1), ('state', 'completed')):
            state = deepcopy(self.state); state['task'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'checkpoint_unsafe'):
                recovery.preflight(state, self.proof, now=recovery.START)
        for key, value in (('ordinary_used', 396), ('remaining_seconds', 630)):
            state = deepcopy(self.state); state[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'checkpoint_unsafe'):
                recovery.preflight(state, self.proof, now=recovery.START)

    def test_original_expiry_proof_and_live_processes_required(self):
        with self.assertRaisesRegex(ValueError, 'expiry_proof_missing'):
            recovery.preflight(self.state, {}, now=recovery.START)
        for name in (recovery.TRIAL, recovery.SYNC):
            state = deepcopy(self.state); state['units'][name]['pid'] = 0
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'not_running'):
                recovery.preflight(state, self.proof, now=recovery.START)

    def test_changed_checkpoint_stops_before_supplier_is_stopped(self):
        stopped = deepcopy(self.state); stopped['ordinary_used'] += 1
        with patch.object(recovery, 'publish'), patch.object(recovery, 'stop_reader') as stop, \
                patch.object(recovery, 'snapshot', return_value=stopped), \
                patch.object(recovery, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'checkpoint_changed'):
                recovery.restart(None, self.state)
        stop.assert_called_once_with(self.state)
        command.assert_not_called()

    def test_only_main_controller_is_signalled_and_timeout_never_restarts_it(self):
        original = self.state['units'][recovery.TRIAL]
        with patch.object(recovery, 'unit', side_effect=[original, {'pid': 0}]), \
                patch.object(recovery.os, 'kill') as kill:
            recovery.stop_reader(self.state)
        kill.assert_called_once_with(10, recovery.signal.SIGTERM)
        with patch.object(recovery, 'unit', return_value=original), \
                patch.object(recovery.os, 'kill'), \
                patch.object(recovery.time, 'monotonic', side_effect=[0, 31]), \
                patch.object(recovery, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'reader_not_stopped'):
                recovery.stop_reader(self.state)
        command.assert_not_called()

    def test_same_supplier_process_or_start_without_new_source_is_not_recovery(self):
        before, cold, after, current = self.recovered_states()
        with self.assertRaisesRegex(ValueError, 'natural_renewal_unproven'):
            recovery.prove_recovery(before, cold, after, after)
        current['container']['pid'] = before['container']['pid']
        with self.assertRaisesRegex(ValueError, 'cold_process_unproven'):
            recovery.prove_recovery(before, cold, after, current)

    def test_each_store_must_have_new_accepted_records_after_old_expiry(self):
        for store in self.state['latest_success']:
            before, cold, after, current = self.recovered_states()
            current['latest_success'][store] = before['expiry']
            with self.subTest(store=store), self.assertRaisesRegex(ValueError, 'records_missing'):
                recovery.prove_recovery(before, cold, after, current)

    def test_proof_requires_renewal_budget_increment_and_task_acceptance(self):
        for key, value in (('revision', 2), ('renewal_attempts', 2), ('context_sha256', 'b'*64)):
            before, cold, after, current = self.recovered_states(); current[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'natural_renewal_unproven'):
                recovery.prove_recovery(before, cold, after, current)
        before, cold, after, current = self.recovered_states()
        current['task']['credential_revision'] = 2
        with self.assertRaisesRegex(ValueError, 'natural_renewal_unproven'):
            recovery.prove_recovery(before, cold, after, current)

    def test_real_cold_renewal_proves_process_recovery_but_not_mac_offline_or_reboot(self):
        proof = recovery.prove_recovery(*self.recovered_states())
        self.assertTrue(proof['cloud_process_cold_recovery_proven'])
        self.assertFalse(proof['mac_actual_offline_proven'])
        self.assertFalse(proof['host_reboot_proven'])

    def test_once_baseline_is_private_durable_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700)
            with patch.object(recovery, 'ROOT', root):
                recovery.publish(self.state, 'recovery-before.json')
                self.assertEqual((root/'recovery-before.json').stat().st_mode & 0o777, 0o600)
                with self.assertRaises(ValueError):
                    recovery.publish(self.state, 'recovery-before.json')

    def test_pinned_public_program_above_credential_size_limit_is_read_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700)
            path = root/'official_cloud_controller.py'
            body = b'VALUE = 1\n#'+b'x'*17000+b'\n'
            path.write_bytes(body); path.chmod(0o600)
            with patch.object(recovery, 'ROOT', root), \
                    patch.object(recovery, 'CONTROLLER_SHA', hashlib.sha256(body).hexdigest()):
                self.assertEqual(recovery.load_controller().VALUE, 1)
                path.write_bytes(b'raise AssertionError("wrong code must not execute")\n')
                with self.assertRaisesRegex(ValueError, 'program_changed'):
                    recovery.load_controller()
