from copy import deepcopy
from datetime import datetime
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.continuity import read_continuity_bundle, select_continuity_packet
from sushiwait.continuitytransport import COMMAND, receive_public_bundle, validate_bundle_receipt
from sushiwait.observations import normalize_snapshot
from sushiwait.officialview import read_config
from sushiwait.packets import encoded
from sushiwait.receipts import PacketArchive, ReceiptError
from sushiwait.ssharchive import COMMAND as PLAIN_COMMAND, SSHArchiveError, receive_public_packet, validate_receipt
from sushiwait.sshrelay import SSHRelayError, check_public_confirmation, deliver_public_packet
from sushiwait.storage import SnapshotStore

HOURS=json.loads((Path(__file__).resolve().parents[1]/'config/default-business-hours.json').read_bytes())
NOW=datetime.fromisoformat('2026-10-11T12:00:00+00:00')


class ContinuityTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='continuity with spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        for name in ('packets','bundles'): (self.root/name).mkdir(mode=0o700)
        self.source=self.root/'source.sqlite3';self.database=self.root/'archive.sqlite3'
        with SnapshotStore(self.source) as writer:
            for i, labels in enumerate((['12','13','14'],['13','14','16'])):
                at=f'2026-10-11T03:0{i}:00Z'
                writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':labels,
                    'reservationQueue':[],'counterQueue':[],'boothQueue':[]}},'900001',
                    request_started_at=at,received_at=at,elapsed_ms=0,
                    api_profile='miniapp_gateway',data_origin='synthetic'))
        self.scope=dict(as_of=NOW.isoformat(),store_ids=['900001'],api_profile='miniapp_gateway',data_origin='synthetic')
        self.bundle=select_continuity_packet(self.source,after_id=1,**self.scope)[0]
        self.config={'schema_version':1,'mode':'official_packet_archive_with_continuity',
            'database_file':str(self.database),'store_names':{'900001':'Synthetic'},
            'data_origin':'synthetic','hours':HOURS,'base_interval':900}
        self.view=self.save('view.json',self.config)
        self.input=self.save('input.json',self.bundle)
        self.key=self.save('key',b'SYNTHETIC_KEY');self.hosts=self.save('hosts',b'SYNTHETIC_HOST_PIN')
        self.confirmation=self.root/'confirmation.json';self.args=[]

    def save(self,name,value):
        path=self.root/name;path.write_bytes(value if isinstance(value,bytes) else encoded(value));path.chmod(0o600)
        return path

    def receive(self,bundle=None):
        return receive_public_bundle(encoded(self.bundle if bundle is None else bundle),self.view,
                                     original_command=COMMAND,now=NOW)

    def factory(self,suffix='print(json.dumps(receipt))'):
        script=('import sys,json;from datetime import datetime;'
            'from sushiwait.continuitytransport import receive_public_bundle,COMMAND;'
            'receipt=receive_public_bundle(sys.stdin.buffer.read(),'+repr(str(self.view))+
            ',original_command=COMMAND,now=datetime.fromisoformat('+repr(NOW.isoformat())+'));'+suffix)
        def spawn(args,**kwargs):
            self.args.append(args)
            return subprocess.Popen([sys.executable,'-u','-c',script],**kwargs)
        return spawn

    def deliver(self,**kwargs):
        options=dict(ssh_key_file=self.key,known_hosts_file=self.hosts,target='operator@192.0.2.1',
            confirmation_file=self.confirmation,clock=lambda:NOW,with_continuity=True,_spawn=self.factory())
        options.update(kwargs)
        return deliver_public_packet(self.input,**options)

    def test_exact_bundle_receipt_includes_anchor_and_durable_retry_keeps_original_bytes(self):
        with patch('socket.socket',side_effect=AssertionError('no source queries')):
            receipt=self.receive();validate_bundle_receipt(receipt,self.bundle)
            retry=self.receive()
        self.assertEqual((receipt['inserted_records'],receipt['inserted_links']),(2,1))
        self.assertEqual((retry['duplicate_records'],retry['duplicate_links']),(2,1))
        path=self.root/'bundles'/(hashlib.sha256(encoded(self.bundle)).hexdigest()+'.json')
        self.assertEqual(path.read_bytes(),encoded(self.bundle))
        self.assertFalse(receipt['query_credentials_accessed'] or receipt['source_claims_verified'])

    def test_old_receiver_and_wrong_mode_reject_new_format_without_writing(self):
        with self.assertRaises(SSHArchiveError):
            receive_public_packet(encoded(self.bundle),self.view,original_command=PLAIN_COMMAND,now=NOW)
        self.save('view.json',{**self.config,'mode':'official_packet_archive_readonly'})
        with self.assertRaises(SSHArchiveError):self.receive()
        self.assertFalse(self.database.exists());self.assertEqual(list((self.root/'bundles').iterdir()),[])

    def test_wrong_commands_fail_before_configuration_or_stdin_content(self):
        with patch('sushiwait.continuitytransport.read_config',side_effect=AssertionError('not opened')) as config:
            for command in (None,PLAIN_COMMAND,COMMAND+';cat file'):
                with self.assertRaises(SSHArchiveError):
                    receive_public_bundle(encoded(self.bundle),self.view,original_command=command,now=NOW)
            self.assertEqual(config.call_count,0)

    def test_plain_packet_and_later_bundle_coexist_without_rewriting_records(self):
        plain=self.bundle['packet']
        old=receive_public_packet(encoded(plain),self.view,original_command=PLAIN_COMMAND,now=NOW)
        validate_receipt(old,plain)
        self.assertEqual(old['inserted_records'],1)
        value=read_config(self.view).read('/api/v1/official/stores/900001/days/2026-10-11',now=NOW)
        self.assertEqual(value['points'][0]['comparison_state'],'packet_continuity_unknown')
        new=self.receive();self.assertEqual((new['inserted_records'],new['duplicate_records']),(1,1))
        value=read_config(self.view).read('/api/v1/official/stores/900001/days/2026-10-11',now=NOW)
        self.assertEqual(value['points'][1]['comparison_state'],'comparable_display_sets')
        self.assertIsNone(value['summary']['no_show_rate'])

    def test_record_and_link_receipt_tampering_cannot_confirm(self):
        receipt=self.receive()
        for key,value in [('bundle_sha256','0'*64),('links',[]),('observations',[]),
                          ('inserted_links',True),('duplicate_records',-1),('receipt_schema_version',True),
                          ('source_claims_verified',True)]:
            with self.subTest(key=key):
                bad={**receipt,key:value}
                with self.assertRaises(SSHArchiveError):validate_bundle_receipt(bad,self.bundle)
        with self.assertRaises(SSHArchiveError):validate_receipt(receipt,self.bundle['packet'])

    def test_conflicting_alias_relation_rolls_back_and_never_returns_receipt(self):
        self.receive();before=self.database.read_bytes();bad=deepcopy(self.bundle)
        bad['links'][0].update(predecessor_id=None,predecessor_sha256=None,predecessor_writer_id=None)
        bad['anchors']=[]
        link=bad['links'][0];link['link_sha256']=hashlib.sha256(encoded({k:v for k,v in link.items() if k!='link_sha256'})).hexdigest()
        with self.assertRaises(SSHArchiveError):self.receive(bad)
        self.assertEqual(before,self.database.read_bytes())

    def test_unsynced_retry_and_uncertain_commit_do_not_acknowledge(self):
        self.receive()
        with patch('sushiwait.continuitytransport._sync_preserved_packet',side_effect=OSError('PRIVATE')):
            with self.assertRaises(SSHArchiveError) as error:self.receive()
        self.assertNotIn('PRIVATE',str(error.exception))
        with patch('sushiwait.continuitytransport.ContinuityArchive.append_bundle',
                   return_value={'durability_confirmed':False}):
            with self.assertRaises(SSHArchiveError):self.receive()

    def test_real_pipes_exact_relation_confirmation_and_fixed_pinned_command(self):
        result=self.deliver();self.assertEqual(result['records_including_anchors'],2)
        self.assertEqual(result['links'],1)
        self.assertEqual(self.args[0][-1],COMMAND)
        self.assertIn('StrictHostKeyChecking=yes',self.args[0])
        checked=check_public_confirmation(self.input,self.confirmation,as_of=NOW.isoformat(),with_continuity=True)
        self.assertEqual(checked['confirmed_links'],1)
        self.assertFalse(checked['ssh_key_accessed'] or checked['network_attempted'] or checked['cursor_advanced'])
        with self.assertRaises(SSHRelayError):check_public_confirmation(self.input,self.confirmation,as_of=NOW.isoformat())

    def test_lost_reply_preserves_bundle_and_duplicate_retry_confirms_both_parts(self):
        with self.assertRaises(SSHRelayError) as error:self.deliver(_spawn=self.factory('pass'))
        self.assertTrue(error.exception.remote_archive_may_have_committed)
        self.assertFalse(self.confirmation.exists());self.assertEqual(self.input.read_bytes(),encoded(self.bundle))
        result=self.deliver();self.assertEqual((result['duplicate_records'],result['duplicate_links']),(2,1))

    def test_reply_link_mismatch_does_not_save_confirmation(self):
        with self.assertRaises(SSHRelayError):self.deliver(_spawn=self.factory('receipt["links"]=[];print(json.dumps(receipt))'))
        self.assertFalse(self.confirmation.exists())

    def test_private_json_reader_rejects_links_permissions_duplicates_and_extra_keys(self):
        link=self.root/'link.json';link.symlink_to(self.input)
        with self.assertRaises(ReceiptError):read_continuity_bundle(link,as_of=NOW.isoformat())
        self.input.chmod(0o644)
        with self.assertRaises(ReceiptError):read_continuity_bundle(self.input,as_of=NOW.isoformat())
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',encoded({**self.bundle,'credential':'PRIVATE_SENTINEL'})):
            self.save('input.json',raw)
            with self.assertRaises(ValueError) as error:read_continuity_bundle(self.input,as_of=NOW.isoformat())
            self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))

    def test_cli_check_and_archive_explicit_mode_preserve_old_database(self):
        with patch('sushiwait.cli._utc_clock',return_value=NOW),patch('socket.socket',side_effect=AssertionError('no origin')):
            for command in ('packet-check','packet-archive'):
                args=[command,'--input',str(self.input),'--with-continuity']
                if command=='packet-archive':args+=['--db',str(self.database)]
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(main(args),0)
                self.assertTrue(json.loads(output.getvalue())['ok'])
            old=self.root/'old.sqlite3'
            with PacketArchive(old) as archive:archive.append(self.bundle['packet'],as_of=NOW.isoformat())
            before=old.read_bytes();output=io.StringIO()
            with contextlib.redirect_stdout(output):
                result=main(['packet-archive','--input',str(self.input),'--db',str(old),'--with-continuity'])
            self.assertEqual(result,1);self.assertEqual(old.read_bytes(),before)
