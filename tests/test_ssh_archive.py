from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait import ssharchive as s
from sushiwait.packets import encoded
from test_official_view import make_packet, HOURS, NOW


class SSHArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        (self.root / 'packets').mkdir(mode=0o700)
        self.db = self.root / 'archive.sqlite3'
        self.config = self.root / 'view.json'
        self.config.write_bytes(encoded({'schema_version': 1,
            'mode': 'official_packet_archive_readonly', 'database_file': str(self.db),
            'store_names': {'900001': 'Synthetic store'}, 'data_origin': 'synthetic',
            'hours': HOURS, 'base_interval': 300}))
        self.config.chmod(0o600)
        self.packet = make_packet([(1, '2026-10-10T03:00:00Z', '2026-10-10T03:00:00.1Z')])

    def receive(self, packet=None, **kwargs):
        return s.receive_public_packet(encoded(self.packet if packet is None else packet),
            self.config, original_command=s.COMMAND, now=NOW, **kwargs)

    def test_real_archive_and_exact_receipt_with_idempotent_retry(self):
        with patch('socket.socket', side_effect=AssertionError('no origin calls')):
            first = self.receive()
            self.assertEqual(s.validate_receipt(first, self.packet), first)
            second = self.receive()
        self.assertEqual((first['inserted_records'], second['duplicate_records']), (1, 1))
        self.assertFalse(first['query_credentials_accessed'] or first['source_claims_verified'])
        raw = self.root / 'packets' / (hashlib.sha256(encoded(self.packet)).hexdigest() + '.json')
        self.assertEqual(raw.read_bytes(), encoded(self.packet))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_records').fetchone()[0], 1)

    def test_command_scope_must_match_before_reading_configuration(self):
        with patch.object(s, 'read_config', side_effect=AssertionError('must not read')) as config:
            for command in (None, '', 'sh', s.COMMAND + ';cat /etc/passwd'):
                with self.assertRaises(s.SSHArchiveError):
                    s.receive_public_packet(encoded(self.packet), self.config,
                        original_command=command, now=NOW)
            self.assertEqual(config.call_count, 0)

    def test_other_store_profile_origin_and_empty_packet_cannot_write(self):
        packets = [make_packet([(1,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z')], store='900002'),
                   make_packet([(1,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z')], profile='legacy'),
                   make_packet([(1,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z')], origin='live'),
                   make_packet([])]
        for packet in packets:
            with self.assertRaises(s.SSHArchiveError):
                self.receive(packet)
        self.assertFalse(self.db.exists())
        self.assertEqual(list((self.root/'packets').iterdir()), [])

    def test_private_fields_duplicate_keys_and_nonfinite_input_rejected(self):
        packet = deepcopy(self.packet)
        packet['credential'] = 'PRIVATE_SENTINEL'
        for raw in (encoded(packet), b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.assertRaises(s.SSHArchiveError) as failure:
                s.receive_public_packet(raw, self.config, original_command=s.COMMAND, now=NOW)
            self.assertNotIn('PRIVATE_SENTINEL', str(failure.exception))
        self.assertFalse(self.db.exists())

    def test_symlink_packet_is_rejected_without_following_target(self):
        other = self.root/'other.json'
        other.write_bytes(encoded(self.packet)); other.chmod(0o600)
        target = self.root/'packets'/(hashlib.sha256(encoded(self.packet)).hexdigest()+'.json')
        target.symlink_to(other)
        with self.assertRaises(s.SSHArchiveError):
            self.receive()
        self.assertFalse(self.db.exists())

    def test_conflict_rolls_back_batch_and_keeps_original_archive(self):
        self.receive()
        before = self.db.read_bytes()
        packet = deepcopy(self.packet)
        row = packet['records'][0]
        row['display']['groupQueues']['groups']['mixedQueue']['value'] = ['100']
        row['record_sha256'] = hashlib.sha256(encoded({k:v for k,v in row.items() if k!='record_sha256'})).hexdigest()
        with self.assertRaises(s.SSHArchiveError):
            self.receive(packet)
        self.assertEqual(self.db.read_bytes(), before)

    def test_uncertain_commit_emits_no_success(self):
        with patch.object(s.PacketArchive, 'append', return_value={'durability_confirmed':False}):
            with self.assertRaises(s.SSHArchiveError):
                self.receive()

    def test_retry_cannot_acknowledge_unsynced_original_packet(self):
        self.receive()
        with patch.object(s, '_sync_preserved_packet', side_effect=OSError('PRIVATE_PATH')):
            with self.assertRaises(s.SSHArchiveError) as failure:
                self.receive()
            self.assertNotIn('PRIVATE_PATH', str(failure.exception))

    def test_wrong_receipt_never_confirms_delivery(self):
        receipt = self.receive()
        for key, value in [('packet_sha256','0'*64), ('duplicate_records',1),
                           ('durability_confirmed',False), ('observations',[]),
                           ('query_credentials_accessed',True), ('receipt_schema_version',True)]:
            with self.assertRaises(s.SSHArchiveError):
                s.validate_receipt({**receipt,key:value}, self.packet)

    def pipe(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        return read, write

    def test_stdin_waits_for_eof_and_rejects_size_overrun(self):
        read, write = self.pipe()
        os.write(write, b'abcd'); os.close(write)
        self.assertEqual(s.read_stdin(read, maximum=4), b'abcd')
        read, write = self.pipe()
        os.write(write, b'abcde'); os.close(write)
        with self.assertRaises(s.SSHArchiveError):
            s.read_stdin(read, maximum=4)

    def test_stalled_sender_and_empty_stream_are_bounded(self):
        read, write = self.pipe()
        try:
            with self.assertRaises(s.SSHArchiveError) as failure:
                s.read_stdin(read, timeout=0.01)
            self.assertEqual(str(failure.exception), 'ssh_archive_input_deadline')
        finally:
            os.close(write)
        read, write = self.pipe(); os.close(write)
        with self.assertRaises(s.SSHArchiveError):
            s.read_stdin(read)

    def test_bounds_fail_before_stream_read(self):
        for changes in ({'fd':-1}, {'timeout':0}, {'timeout':16}, {'maximum':0},
                        {'maximum':s.MAX_PACKET_BYTES+1}):
            with self.assertRaises(s.SSHArchiveError):
                s.read_stdin(**{'fd':0,**changes})


if __name__ == '__main__':
    unittest.main()
