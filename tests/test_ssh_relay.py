import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sushiwait import sshrelay as relay
from sushiwait.cli import main
from sushiwait.packets import encoded
from test_official_view import make_packet, HOURS, NOW


class SSHRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay with spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        (self.root/'packets').mkdir(mode=0o700)
        self.packet = make_packet([(1,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z')])
        self.input = self.save('input.json', self.packet)
        self.key = self.save('key', b'SYNTHETIC_KEY_NOT_AUTHENTICATED')
        self.hosts = self.save('known hosts', b'SYNTHETIC_PIN_NOT_AUTHENTICATED')
        self.confirmation = self.root/'confirmation.json'
        self.db = self.root/'archive.sqlite3'
        self.view = self.save('view.json', {'schema_version':1,
            'mode':'official_packet_archive_readonly', 'database_file':str(self.db),
            'store_names':{'900001':'Synthetic store'}, 'data_origin':'synthetic',
            'hours':HOURS, 'base_interval':300})
        self.args, self.children = [], []
        self.addCleanup(self.cleanup_children)

    def save(self, name, value):
        path = self.root/name
        path.write_bytes(value if isinstance(value, bytes) else encoded(value))
        path.chmod(0o600)
        return path

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill(); child.wait(timeout=2)

    def factory(self, suffix='print(json.dumps(receipt))', before=None):
        # Real pipes and SQLite receiver; synthetic local subprocess, no SSH
        # authentication or source-network acceptance is claimed by this test.
        script = ('import sys,json; from datetime import datetime; '
            'from sushiwait.ssharchive import receive_public_packet,COMMAND; '
            'receipt=receive_public_packet(sys.stdin.buffer.read(),'+repr(str(self.view))+
            ',original_command=COMMAND,now=datetime.fromisoformat('+repr(NOW.isoformat())+')); '+suffix)
        def spawn(args, **kwargs):
            self.args.append(args)
            if before:
                before()
            child = subprocess.Popen([sys.executable,'-u','-c',script], **kwargs)
            self.children.append(child)
            return child
        return spawn

    def deliver(self, **changes):
        values = {'ssh_key_file':self.key, 'known_hosts_file':self.hosts,
            'target':'test_operator@192.0.2.10', 'confirmation_file':self.confirmation,
            'clock':lambda:NOW, '_spawn':self.factory()}
        values.update(changes)
        return relay.deliver_public_packet(self.input, **values)

    def test_real_pipes_archive_and_immutable_exact_confirmation(self):
        with patch('socket.socket', side_effect=AssertionError('no source sockets')):
            result = self.deliver()
        self.assertTrue(result['server_archive_commit_confirmed'])
        self.assertEqual((result['inserted_records'],result['duplicate_records']), (1,0))
        saved = json.loads(self.confirmation.read_bytes())
        self.assertEqual(saved['receipt']['observations'][0]['observation_id'], self.packet['records'][0]['observation_id'])
        self.assertEqual(self.input.read_bytes(), encoded(self.packet))
        self.assertEqual(self.confirmation.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM archive_records').fetchone()[0],1)
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver()
        self.assertFalse(error.exception.network_attempted)
        self.assertEqual(len(self.args),1)

    def test_fixed_command_pins_and_space_quoting_no_agent_or_shell(self):
        self.deliver()
        args = self.args[0]
        self.assertEqual(args[0],'/usr/bin/ssh')
        self.assertEqual(args[-2:],['test_operator@192.0.2.10','sushiwait-official-packet-v1'])
        self.assertIn('UserKnownHostsFile="'+str(self.hosts)+'"',args)
        for option in ('StrictHostKeyChecking=yes','BatchMode=yes','IdentityAgent=none',
            'PasswordAuthentication=no','KbdInteractiveAuthentication=no','ClearAllForwardings=yes'):
            self.assertIn(option,args)
        self.assertEqual(args[1:4],['-F','/dev/null','-T'])

    def test_lost_ack_preserves_packet_and_retry_really_deduplicates(self):
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory('pass'))
        self.assertTrue(error.exception.remote_archive_may_have_committed)
        self.assertFalse(self.confirmation.exists())
        self.assertEqual(self.input.read_bytes(), encoded(self.packet))
        result = self.deliver()
        self.assertEqual((result['inserted_records'],result['duplicate_records']), (0,1))

    def test_receipt_mismatch_and_duplicate_json_keys_never_confirm(self):
        for suffix in ('receipt["observations"]=[]; print(json.dumps(receipt))',
                       'print(\'{"x":1,"x":2}\')', 'print(\'{"x":NaN}\')'):
            with self.assertRaises(relay.SSHRelayError) as error:
                self.deliver(_spawn=self.factory(suffix))
            self.assertEqual(error.exception.error_code,'ssh_relay_receipt_mismatch')
            self.assertFalse(self.confirmation.exists())

    def test_zero_exit_without_complete_input_is_not_acknowledgement(self):
        def spawn(args, **kwargs):
            child = subprocess.Popen([sys.executable,'-c','pass'],**kwargs)
            self.children.append(child);return child
        with self.assertRaises(relay.SSHRelayError):
            self.deliver(_spawn=spawn)
        self.assertFalse(self.confirmation.exists())

    def test_bounded_stdout(self):
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory('sys.stdout.write("x"*262145)'))
        self.assertEqual(error.exception.error_code,'ssh_relay_reply_limit')
        self.assertFalse(self.confirmation.exists())
        self.assertIsNotNone(self.children[-1].poll())

    def test_stalled_reader_large_stdin_deadline_kills_owned_child(self):
        rows=[(i,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z') for i in range(1,301)]
        packet=make_packet(rows)
        self.assertGreater(len(encoded(packet)),65536)
        self.input.write_bytes(encoded(packet))
        def spawn(args, **kwargs):
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'],**kwargs)
            self.children.append(child);return child
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=spawn,timeout=1)
        self.assertEqual(error.exception.error_code,'ssh_relay_deadline')
        self.assertIsNotNone(self.children[-1].poll())
        self.assertFalse(self.confirmation.exists())

    def test_private_fields_and_empty_packets_fail_before_process_start(self):
        for packet in ({**self.packet,'authorization':'PRIVATE_SENTINEL'}, make_packet([])):
            self.input.write_bytes(encoded(packet))
            with self.assertRaises(relay.SSHRelayError) as error:
                self.deliver()
            self.assertFalse(error.exception.network_attempted)
            self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))
        self.assertEqual(self.args,[])

    def test_unsafe_access_files_symlinks_and_conflicting_paths_do_not_contact(self):
        self.key.chmod(0o644)
        with self.assertRaises(relay.SSHRelayError):self.deliver()
        self.key.chmod(0o600)
        link=self.root/'linked';link.symlink_to(self.key)
        with self.assertRaises(relay.SSHRelayError):self.deliver(ssh_key_file=link)
        with self.assertRaises(relay.SSHRelayError):self.deliver(confirmation_file=self.input)
        self.assertEqual(self.args,[])

    def test_access_file_mutation_during_exchange_is_not_confirmed(self):
        def changed():self.key.write_bytes(b'SYNTHETIC_CHANGED_KEY')
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory(before=changed))
        self.assertTrue(error.exception.network_attempted)
        self.assertEqual(error.exception.error_code,'ssh_relay_access_file_changed')
        self.assertFalse(self.confirmation.exists())

    def test_public_packet_ctime_only_change_keeps_exact_confirmation(self):
        def changed():self.input.chmod(0o600)
        result=self.deliver(_spawn=self.factory(before=changed))
        self.assertTrue(result['server_archive_commit_confirmed'])
        self.assertTrue(result['durability_confirmed'])

    def test_public_packet_changed_bytes_with_restored_mtime_are_rejected(self):
        original=self.input.read_bytes();before=self.input.stat()
        def changed():
            self.input.write_bytes(b'X'+original[1:])
            os.utime(self.input,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory(before=changed))
        self.assertTrue(error.exception.network_attempted)
        self.assertEqual(error.exception.error_code,'ssh_relay_access_file_changed')
        self.assertFalse(self.confirmation.exists())

    def test_private_key_ctime_only_change_still_rejects(self):
        def changed():self.key.chmod(0o600)
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory(before=changed))
        self.assertEqual(error.exception.error_code,'ssh_relay_access_file_changed')
        self.assertFalse(self.confirmation.exists())

    def test_pinned_host_ctime_only_change_still_rejects(self):
        def changed():self.hosts.chmod(0o600)
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory(before=changed))
        self.assertEqual(error.exception.error_code,'ssh_relay_access_file_changed')
        self.assertFalse(self.confirmation.exists())

    def test_public_path_replacement_is_not_confirmed(self):
        def changed():
            replacement=self.save('replacement',self.input.read_bytes())
            os.replace(replacement,self.input)
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(_spawn=self.factory(before=changed))
        self.assertEqual(error.exception.error_code,'ssh_relay_access_file_changed')
        self.assertFalse(self.confirmation.exists())

    def test_unconfirmed_local_fsync_never_advances_or_deletes_packet(self):
        with patch.object(relay,'_write_packet',return_value={'durability_confirmed':False}):
            with self.assertRaises(relay.SSHRelayError) as error:self.deliver()
        self.assertTrue(error.exception.remote_archive_may_have_committed)
        self.assertTrue(error.exception.local_confirmation_may_exist)
        self.assertEqual(self.input.read_bytes(),encoded(self.packet))

    def test_invalid_target_bounds_and_paths_are_no_network(self):
        changes=[{'target':x} for x in ('host','root@host;sh','root@-oProxyCommand','-F@host','root@[::1]','root@999.999.999.999')]
        changes += [{'timeout':x} for x in (True,0,61,float('nan'))]
        changes += [{'known_hosts_file':str(self.hosts)+'\n'}]
        for values in changes:
            with self.subTest(values=values),self.assertRaises(relay.SSHRelayError) as error:
                self.deliver(**values)
            self.assertFalse(error.exception.network_attempted)
        self.assertEqual(self.args,[])

    def test_spawn_failure_and_untrusted_exception_text_are_redacted(self):
        def spawn(*a,**kw):raise OSError('PRIVATE_PATH_SENTINEL')
        with self.assertRaises(relay.SSHRelayError) as error:self.deliver(_spawn=spawn)
        self.assertFalse(error.exception.network_attempted)
        self.assertNotIn('PRIVATE_PATH_SENTINEL',str(error.exception))

    def test_cli_uses_explicit_fields_and_no_secret_error_echo(self):
        args=['packet-deliver-ssh','--input',str(self.input),'--ssh-key-file',str(self.key),
              '--known-hosts-file',str(self.hosts),'--target','test_operator@192.0.2.10',
              '--confirmation-file',str(self.confirmation)]
        out=io.StringIO()
        failure=relay.SSHRelayError('ssh_relay_transport_exit',network_attempted=True)
        with patch.object(relay,'deliver_public_packet',side_effect=failure) as send,contextlib.redirect_stdout(out):
            self.assertEqual(main(args),1)
        body=json.loads(out.getvalue())
        self.assertTrue(body['remote_archive_may_have_committed'])
        self.assertFalse(body['retry_performed'] or body['query_credentials_accessed'])
        self.assertEqual(send.call_args.kwargs['target'],'test_operator@192.0.2.10')
        self.assertNotIn(str(self.key),out.getvalue())

    def test_clock_reversal_after_remote_commit_keeps_unconfirmed_original(self):
        ticks=iter((NOW,NOW-timedelta(seconds=1)))
        with self.assertRaises(relay.SSHRelayError) as error:
            self.deliver(clock=lambda:next(ticks))
        self.assertEqual(error.exception.error_code,'ssh_relay_clock_reversed')
        self.assertTrue(error.exception.remote_archive_may_have_committed)
        self.assertFalse(self.confirmation.exists())

    def check(self, **changes):
        values={'as_of':NOW.isoformat()};values.update(changes)
        return relay.check_public_confirmation(self.input,self.confirmation,**values)

    def test_saved_receipt_rechecked_without_key_process_network_or_writes(self):
        self.deliver()
        before=self.input.read_bytes(),self.confirmation.read_bytes(),self.db.read_bytes()
        self.key.unlink();self.hosts.unlink()
        with patch('socket.socket',side_effect=AssertionError('no sockets')), \
                patch('subprocess.Popen',side_effect=AssertionError('no processes')):
            result=self.check()
        self.assertTrue(result['historical_confirmation_only'] and result['exact_packet_receipt_match'])
        self.assertFalse(result['current_server_retention_verified'] or result['cursor_advanced'])
        self.assertEqual(result['confirmed_records'],1)
        self.assertEqual(before,(self.input.read_bytes(),self.confirmation.read_bytes(),self.db.read_bytes()))

    def test_saved_receipt_structure_and_exact_packet_binding_reject_edits(self):
        self.deliver(); original=json.loads(self.confirmation.read_bytes())
        mutations=[{**original,'authorization':'PRIVATE_SENTINEL'},
                   {**original,'relay_confirmation_schema_version':True},
                   {**original,'transport':'unverified'},
                   {**original,'receipt':{**original['receipt'],'observations':[]}}]
        for value in mutations:
            self.confirmation.write_bytes(encoded(value))
            with self.assertRaises(relay.SSHRelayError) as error:self.check()
            self.assertFalse(error.exception.network_attempted)
            self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))

    def test_saved_receipt_time_is_canonical_and_bounded(self):
        self.deliver(); original=json.loads(self.confirmation.read_bytes())
        for at in ('2026-10-10T02:59:59Z','2026-10-11T00:00:00Z',NOW.isoformat()):
            self.confirmation.write_bytes(encoded({**original,'acknowledged_at':at}))
            with self.assertRaises(relay.SSHRelayError):self.check()

    def test_saved_receipt_private_regular_file_and_bounded_bytes(self):
        self.deliver();saved=self.confirmation.read_bytes()
        self.confirmation.chmod(0o644)
        with self.assertRaises(relay.SSHRelayError):self.check()
        self.confirmation.chmod(0o600)
        self.confirmation.write_bytes(b'x'*(relay.MAX_REPLY_BYTES+1025))
        with self.assertRaises(relay.SSHRelayError):self.check()
        self.confirmation.write_bytes(saved)
        moved=self.root/'saved';self.confirmation.rename(moved);self.confirmation.symlink_to(moved)
        with self.assertRaises(relay.SSHRelayError):self.check()

    def test_cli_rechecks_historical_receipt_without_current_server_claim(self):
        self.deliver();out=io.StringIO()
        with contextlib.redirect_stdout(out),patch('subprocess.Popen',side_effect=AssertionError('no processes')):
            self.assertEqual(main(['packet-confirmation-ssh','--input',str(self.input),
                '--confirmation-file',str(self.confirmation),'--as-of',NOW.isoformat()]),0)
        value=json.loads(out.getvalue())
        self.assertTrue(value['exact_packet_receipt_match'])
        self.assertFalse(value['current_server_retention_verified'])


if __name__=='__main__':unittest.main()
