"""Synthetic inputs at the new fixed private upload boundary; no cloud access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_credentials import write_bundle
from tests.test_materialsource import bundle, TARGET, PACKAGE

SPEC = importlib.util.spec_from_file_location('cloud_material_receiver',
    Path(__file__).resolve().parents[1] / 'deploy' / 'official_material_receiver.py')
receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receiver)


class CloudMaterialReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        write_bundle(self.root/'source-pins.json',
            {'app_id': TARGET, 'source_version': 181, 'package_sha256': PACKAGE})
        self.body = json.dumps(bundle()).encode()
        self.target = self.root/'initialization-material.json'
        self.socket = patch('socket.create_connection', side_effect=AssertionError('no network')).start()
        self.addCleanup(patch.stopall)

    def test_one_private_durable_publication_and_no_network(self):
        result = receiver.receive(self.root, self.body)
        self.assertTrue(result['ok'] and result['committed'] and result['durability_confirmed'])
        self.assertEqual(json.loads(self.target.read_bytes()), bundle())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.socket.assert_not_called()

    def test_existing_target_is_never_read_or_replaced(self):
        self.target.write_bytes(b'preserve-even-invalid-original')
        self.target.chmod(0o600)
        with self.assertRaises(Exception):
            receiver.receive(self.root, self.body)
        self.assertEqual(self.target.read_bytes(), b'preserve-even-invalid-original')

    def test_symlink_target_does_not_touch_other_file(self):
        other = self.root/'other'; other.write_bytes(b'keep')
        self.target.symlink_to(other)
        with self.assertRaises(Exception):
            receiver.receive(self.root, self.body)
        self.assertEqual(other.read_bytes(), b'keep')

    def test_target_version_and_source_digest_are_independent_pins(self):
        for field, value in [('target_app_id','wx1111111111111111'),
                             ('source_version',182), ('source_package_sha256','b'*64)]:
            with self.subTest(field=field):
                material = bundle(); material[field] = value
                if field=='target_app_id':
                    material['referer']=f'https://servicewechat.com/{value}/181/page-frame.html'
                with self.assertRaises(Exception):
                    receiver.receive(self.root, json.dumps(material).encode())
                self.assertFalse(self.target.exists())

    def test_oversized_or_duplicate_or_credential_extra_is_rejected(self):
        extra = bundle(); extra['authorization'] = 'synthetic-sensitive-extra'
        for body in [b' '*16385, b'{"schema_version":1,"schema_version":1}', json.dumps(extra).encode()]:
            with self.assertRaises(Exception):
                receiver.receive(self.root, body)
            self.assertFalse(self.target.exists())

    def test_unsafe_pin_file_is_rejected(self):
        (self.root/'source-pins.json').chmod(0o644)
        with self.assertRaises(Exception):
            receiver.receive(self.root, self.body)
        self.assertFalse(self.target.exists())

    def test_postcommit_uncertainty_is_reported_without_retry(self):
        with patch.object(receiver,'_write_packet',return_value={'committed':True,'durability_confirmed':False}) as writer:
            result = receiver.receive(self.root,self.body)
        self.assertFalse(result['ok'])
        self.assertTrue(result['committed'])
        self.assertFalse(result['durability_confirmed'] or result['automatic_retry'])
        writer.assert_called_once()
