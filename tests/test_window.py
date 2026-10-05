"""Synthetic private files and independent child protocol; no Surge/upstream."""
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sushiwait import window
from sushiwait.capture import _write_private
from sushiwait.cli import main
from sushiwait.credentials import read_credentials_file
from sushiwait.surge import SurgeError

OFF = {'mitm_enabled': False, 'capture_enabled': False, 'auto_mitm': False}
REF = 'https://servicewechat.com/wx0000000000000000/1/page-frame.html'


def bundle(revision=1):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = base64.urlsafe_b64encode(json.dumps({'iat': now, 'exp': now+3600, 'private': f'synthetic-window-secret-{revision}'}).encode()).decode().rstrip('=')
    return {'schema_version': 1, 'api_profile': 'miniapp_gateway', 'revision': revision,
            'authorization': 'Bearer e30.'+claims+'.c2ln', 'app_client': 'synthetic-miniapp',
            'app_code': 'synthetic-private-code', 'user_agent': 'Synthetic Agent/1.0',
            'referer': REF, 'content_type': 'application/json'}


def ready(seconds=40):
    return {'event': 'surge_guard_ready', 'seconds': seconds, 'initial': OFF,
            'expires_at': (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),
            'can_enable_mitm': False, 'external_network_performed': False, 'credentials_accessed': False}


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve(); self.folder.chmod(0o700)
        a, b = self.folder/'main', self.folder/'stage'; a.mkdir(mode=0o700); b.mkdir(mode=0o700)
        self.current, self.staging = a/'current.json', b/'staged.json'
        self.save(self.current, bundle()); self.staging.write_bytes(self.current.read_bytes()); self.staging.chmod(0o600)
        self.expected_main = self.current.read_bytes()
        self.guard = MagicMock(); self.guard.poll.return_value = None
        self.platform = patch.object(window.sys, 'platform', 'darwin').start()
        self.state = patch.object(window, 'read_state', return_value=OFF).start()
        self.spawn = patch.object(window, '_spawn_guard', return_value=self.guard).start()
        self.readiness = patch.object(window, '_ready', side_effect=lambda p,s:ready(s)).start()
        self.finish = patch.object(window, '_finish', return_value=True).start()
        self.intake = patch.object(window, 'receive_summary', side_effect=self.receive).start()
        self.socket = patch('socket.socket', side_effect=AssertionError('network')).start()
        self.connect = patch('socket.create_connection', side_effect=AssertionError('network')).start()
        self.native = patch('sushiwait.surgeguard._command', side_effect=AssertionError('native')).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.assertEqual(self.current.read_bytes(), self.expected_main)
        self.assertEqual(self.socket.call_count+self.connect.call_count+self.native.call_count, 0)

    def save(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def receive(self, **kwargs):
        if kwargs['on_ready']: kwargs['on_ready']({'event':'surge_ready'})
        self.save(self.staging, bundle(2))
        return {'committed': True, 'durability_confirmed': True, 'revision': 2}

    def run_window(self, **changes):
        return window.run_window(**{'credentials_file':self.current,'staged_file':self.staging,
                                    'revision':2,'seconds':40,'collector_paused':True,**changes})

    def error(self, code, **changes):
        with self.assertRaises(window.WindowError) as caught: self.run_window(**changes)
        self.assertEqual(caught.exception.error_code, code)
        return caught.exception

    def test_success_preserves_main_commits_only_stage_and_confirms_off(self):
        events=[]; result=self.run_window(on_ready=events.append)
        self.assertTrue(result['staging_committed'] and result['debug_off_confirmed'])
        self.assertFalse(result['main_context_updated'] or result['hostnames_restored'] or result['debug_enabled_by_tool'])
        self.assertEqual(read_credentials_file(self.staging,api_profile='miniapp_gateway').revision,2)
        self.assertEqual(events[0]['event'],'context_window_ready')
        self.assertFalse(events[0]['collector_pause_verified'] or events[0]['hostnames_verified'])
        text=json.dumps([result,events])
        for secret in (REF,'synthetic-private-code',str(self.folder),bundle()['authorization']): self.assertNotIn(secret,text)
        self.finish.assert_called_once_with(self.guard)

    def test_same_directory_refused_before_read_or_native(self):
        self.error('window_separate_staging_required',staged_file=self.current.parent/'other.json')
        self.state.assert_not_called(); self.spawn.assert_not_called()

    def test_invalid_or_unacknowledged_window_is_side_effect_free(self):
        for changes in ({'seconds':0},{'seconds':41},{'seconds':True},{'seconds':5.0},
                        {'revision':True},{'revision':2**63},{'collector_paused':False},{'collector_paused':1},{'on_ready':1}):
            with self.subTest(changes=changes): self.error('window_invalid_input',**changes)
        self.state.assert_not_called(); self.spawn.assert_not_called()

    def test_baseline_or_revision_mismatch_refuses_before_guard(self):
        self.save(self.staging,bundle(2)); self.error('window_baseline_mismatch')
        self.spawn.assert_not_called()

    def test_initial_on_or_unknown_does_not_take_over_existing_debug(self):
        for value in ({**OFF,'mitm_enabled':True},{**OFF,'auto_mitm':True},{**OFF,'capture_enabled':True},{**OFF,'mitm_enabled':0},None):
            self.state.return_value=value; self.error('window_initial_switch_on_or_unknown')
        self.spawn.assert_not_called(); self.finish.assert_not_called()

    def test_private_stage_permissions_and_symlink_refused(self):
        self.staging.chmod(0o644); self.error('window_private_context_invalid')
        self.staging.chmod(0o600)
        link=self.staging.parent/'link'; link.symlink_to(self.staging)
        self.error('window_private_context_invalid',staged_file=link)
        self.spawn.assert_not_called()

    def test_advisory_main_lock_blocks_cooperating_writer_but_stage_can_commit(self):
        previous=self.intake.side_effect
        def locked(**kwargs):
            candidate=bundle(2)
            with self.assertRaises(Exception):
                _write_private(json.dumps(candidate).encode(),self.current,2,
                               read_credentials_file(self.staging,api_profile='miniapp_gateway'),None)
            return previous(**kwargs)
        self.intake.side_effect=locked; self.run_window()

    def test_receiver_error_closes_guard_and_reports_unchanged_staging(self):
        self.intake.side_effect=SurgeError('surge_context_unchanged')
        error=self.error('window_surge_context_unchanged')
        self.assertFalse(error.staging_changed); self.assertTrue(error.cleanup_confirmed)
        self.finish.assert_called_once_with(self.guard)

    def test_callback_exception_closes_guard_and_redacts_text(self):
        def fail(_): raise ValueError('synthetic-private-callback')
        error=self.error('window_local_operation_failed',on_ready=fail)
        self.assertNotIn('synthetic-private',str(error)); self.assertTrue(error.cleanup_confirmed)

    def test_keyboard_interrupt_closes_guard(self):
        self.intake.side_effect=KeyboardInterrupt()
        error=self.error('window_interrupted'); self.assertTrue(error.cleanup_confirmed)

    def test_ready_failure_still_cleans_own_child(self):
        self.readiness.side_effect=window.WindowError('window_guard_not_ready')
        self.error('window_guard_not_ready'); self.finish.assert_called_once_with(self.guard)
        self.intake.assert_not_called()

    def test_guard_finished_before_callback_prevents_action_ready(self):
        self.guard.poll.return_value=0
        error=self.error('window_guard_not_ready'); self.assertFalse(error.staging_changed)

    def test_main_changes_during_intake_are_reported_and_staging_preserved(self):
        previous=self.intake.side_effect
        def mutate(**kwargs):
            result=previous(**kwargs); self.save(self.current,bundle(3));self.expected_main=self.current.read_bytes();return result
        self.intake.side_effect=mutate
        error=self.error('window_main_context_changed');self.assertTrue(error.staging_changed)

    def test_stage_corruption_after_cleanup_is_fixed_error(self):
        def corrupt(_): self.staging.write_text('synthetic-private-invalid');return True
        self.finish.side_effect=corrupt
        error=self.error('window_staging_unconfirmed');self.assertIsNone(error.staging_changed)
        self.assertTrue(error.cleanup_confirmed)

    def test_cleanup_unconfirmed_preserves_new_stage_and_never_promotes(self):
        self.finish.return_value=False
        error=self.error('window_cleanup_unconfirmed');self.assertTrue(error.staging_changed)
        self.assertFalse(error.cleanup_confirmed)

    def test_cleanup_exception_is_unconfirmed(self):
        self.finish.side_effect=OSError('synthetic-private')
        self.error('window_cleanup_unconfirmed')

    def test_switch_reenabled_after_child_cleanup_is_refused(self):
        self.state.side_effect=[OFF,{**OFF,'mitm_enabled':True}]
        self.error('window_cleanup_unconfirmed')

    def test_unknown_error_code_is_redacted(self):
        error=window.WindowError('synthetic-private-error')
        self.assertEqual(error.error_code,'window_runtime_failed')

    def test_cli_invalid_seconds_never_reads_credentials_or_calls_native(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=main(['context-window','--credentials-file','synthetic-private-main',
                         '--staged-file','synthetic-private-stage','--revision','2','--seconds','0','--collector-paused'])
        self.assertEqual(result,1); self.assertNotIn('synthetic-private',output.getvalue())
        self.state.assert_not_called(); self.spawn.assert_not_called()


    def test_durability_unknown_is_preserved_without_main_commit(self):
        prior=self.intake.side_effect
        def uncertain(**kwargs):
            value=prior(**kwargs);value['durability_confirmed']=False;return value
        self.intake.side_effect=uncertain
        result=self.run_window()
        self.assertFalse(result['durability_confirmed'])
        self.assertTrue(result['staging_committed'] and result['debug_off_confirmed'])

    def test_main_changes_during_cleanup_are_detected_under_lock(self):
        def change(_):
            self.save(self.current,bundle(3));self.expected_main=self.current.read_bytes();return True
        self.finish.side_effect=change
        error=self.error('window_main_context_changed');self.assertTrue(error.cleanup_confirmed)

    def test_staging_same_auth_or_other_app_is_not_certified(self):
        original=json.loads(self.current.read_bytes())['authorization']
        for changes in ({'authorization':original},{'referer':REF.replace('wx0000000000000000','wx1111111111111111')}):
            self.staging.write_bytes(self.current.read_bytes())
            prior=self.intake.side_effect
            def replace(**kwargs):
                self.save(self.staging,{**bundle(2),**changes})
                return {'committed':True,'revision':2,'durability_confirmed':True}
            self.intake.side_effect=replace
            self.error('window_staging_unconfirmed')
            self.intake.side_effect=prior

    def test_unsupported_platform_refused_before_native(self):
        with patch.object(window.sys,'platform','linux'):
            self.error('window_unsupported_environment')
        self.spawn.assert_not_called();self.state.assert_not_called()


class GuardProtocolTests(unittest.TestCase):
    def process(self, payload):
        process=subprocess.Popen([sys.executable,'-c','import sys;sys.stdout.buffer.write('+repr(payload)+');sys.stdout.flush()'],
                                 stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(lambda: process.wait(timeout=5))
        return process

    def test_strict_ready_metadata_and_expired_guard_rejection(self):
        process=MagicMock();process.poll.return_value=None
        with patch.object(window,'_read_output',return_value=json.dumps(ready()).encode()):
            self.assertEqual(window._ready(process,40)['seconds'],40)
        value=ready();value['expires_at']='2026-01-01T00:00:00Z'
        with patch.object(window,'_read_output',return_value=json.dumps(value).encode()),self.assertRaises(window.WindowError):
            window._ready(process,40)

    def test_duplicate_keys_and_nonfinite_are_rejected(self):
        for value in (b'{"event":1,"event":2}',b'{"x":NaN}',b'[]',b'synthetic-private-bad-json'):
            with self.assertRaises(window.WindowError):window._json(value)

    def test_pipe_preserves_completion_line_and_output_is_bounded(self):
        process=self.process(b'first\nsecond\n')
        self.assertEqual(window._read_output(process,timeout=2,first_line=True),b'first\n')
        self.assertEqual(window._read_output(process,timeout=2,first_line=False),b'second\n')
        process=self.process(b'x'*65537)
        with self.assertRaises(window.WindowError):window._read_output(process,timeout=2,first_line=False)

    def test_real_finished_pipe_is_verified_with_strict_off_booleans(self):
        value={'ok':True,'event':'surge_guard_finished','verified_off':True,'final':OFF}
        process=self.process(json.dumps(value).encode()+b'\n');process.wait(timeout=2)
        self.assertTrue(window._finish(process))
        value['final']={**OFF,'mitm_enabled':0}
        process=self.process(json.dumps(value).encode()+b'\n');process.wait(timeout=2)
        self.assertFalse(window._finish(process))

    def test_unconfirmed_guard_is_not_hard_killed(self):
        process=MagicMock();process.poll.return_value=None
        with patch.object(window,'_read_output',side_effect=window.WindowError('window_guard_output_timeout')):
            self.assertFalse(window._finish(process))
        process.terminate.assert_called_once();process.kill.assert_not_called()
        process.stdout.close.assert_not_called()

    def test_guard_command_uses_installed_isolated_fixed_entry_and_own_session(self):
        with patch.object(window.subprocess,'Popen') as start:
            window._spawn_guard(40,Path('/synthetic'))
        args=start.call_args
        self.assertEqual(args.args[0],[sys.executable,'-I','-m','sushiwait','surge-guard','--seconds','40'])
        self.assertTrue(args.kwargs['start_new_session']);self.assertEqual(args.kwargs['stderr'],subprocess.DEVNULL)


if __name__=='__main__':unittest.main()
