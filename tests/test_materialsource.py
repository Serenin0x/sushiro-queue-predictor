"""Synthetic inputs only. No official material, account or package access."""

import base64
import copy
from datetime import datetime, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sushiwait.materialsource import MaterialSourceError, prepare_material_source, _cipher_backend
from tests.test_credentials import write_bundle


TARGET = 'wx0000000000000000'
PACKAGE = 'a'*64
CODE = 'synthetic-fresh-code'


def bundle():
    return {'schema_version': 1, 'target_app_id': TARGET, 'source_version': 181,
            'source_package_sha256': PACKAGE, 'timezone': 'Asia/Shanghai',
            'signature_key': 'synthetic-signature-secret', 'long_token': 'synthetic-startup-secret',
            'app_client': 'synthetic-client', 'device_id': 'synthetic-device',
            'app_version': 'synthetic-version', 'user_agent': 'Synthetic Runtime',
            'referer': f'https://servicewechat.com/{TARGET}/181/page-frame.html',
            'initialize_content_type': 'application/json',
            'query_content_type': 'application/x-www-form-urlencoded',
            'date_header': {'algorithm': 'aes-cbc-pkcs7-base64',
                'key_hex': '000102030405060708090a0b0c0d0e0f',
                'iv_hex': '000102030405060708090a0b0c0d0e0f', 'base': 5000,
                'pieces': [{'field': 'year'}, {'literal': '|'}, {'field': 'month'}, {'literal': '|'},
                           {'field': 'day'}, {'literal': '|'}, {'field': 'derived'}]}}


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'), 'private POSIX material file')
class MaterialSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.path = self.root/'material.json'
        self.value = bundle(); write_bundle(self.path, self.value)
        self.encrypt = Mock(return_value='synthetic-date-code')
        self.backend = patch('sushiwait.materialsource._cipher_backend', return_value=self.encrypt).start()
        self.addCleanup(patch.stopall)
        self.socket = patch('socket.create_connection', side_effect=AssertionError('no network')).start()

    def prepare(self, **changes):
        return prepare_material_source(**{'bundle_file': self.path, 'expected_app_id': TARGET,
            'expected_source_version': 181, 'expected_package_sha256': PACKAGE, **changes})

    def error(self, code, action=None):
        with self.assertRaises(MaterialSourceError) as caught:
            (action or self.prepare)()
        error = caught.exception
        self.assertEqual(error.error_code, code)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        for value in (str(self.path), self.value['signature_key'], self.value['long_token'], CODE):
            self.assertNotIn(value, repr(error))

    def test_complete_request_uses_fresh_code_second_nonce_and_explicit_material(self):
        before = self.path.read_bytes()
        source = self.prepare()
        at = int(datetime(2026, 10, 11, 12, tzinfo=timezone.utc).timestamp())
        with patch('sushiwait.materialsource.secrets.randbelow', return_value=123):
            body, heads = source(CODE, at)
        unsigned = {'long_token': self.value['long_token'], 'js_code': CODE, 'device_id': self.value['device_id'],
                    'app_version': self.value['app_version'], 'ts': at, 'nonce': '000123'}
        expected = hmac.new(self.value['signature_key'].encode(),
            '&'.join(f'{key}={unsigned[key]}' for key in sorted(unsigned)).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(body, {**unsigned, 'sign': expected})
        self.assertEqual(heads, {'X-App-Client': 'synthetic-client', 'X-App-Code': 'synthetic-date-code',
            'User-Agent': 'Synthetic Runtime', 'Referer': self.value['referer'], 'Content-Type': 'application/json'})
        self.encrypt.assert_called_once_with(bytes.fromhex(self.value['date_header']['key_hex']),
            bytes.fromhex(self.value['date_header']['iv_hex']), b'2026|10|11|2973')
        self.assertEqual((source.target_app_id, source.app_client, source.query_content_type),
                         (TARGET, 'synthetic-client', 'application/x-www-form-urlencoded'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.socket.assert_not_called()

    def test_china_midnight_changes_header_without_new_bundle_or_host_timezone(self):
        source = self.prepare()
        for hour, minute, text in ((15, 59, b'2026|10|11|2973'), (16, 0, b'2026|10|12|2972')):
            at = int(datetime(2026, 10, 11, hour, minute, tzinfo=timezone.utc).timestamp())
            source(CODE, at)
            self.assertEqual(self.encrypt.call_args.args[2], text)

    def test_year_and_leap_day_follow_explicit_calendar(self):
        source = self.prepare()
        for at, text in ((datetime(2026, 12, 31, 16, tzinfo=timezone.utc), b'2027|1|1|2973'),
                         (datetime(2028, 2, 28, 16, tzinfo=timezone.utc), b'2028|2|29|2945')):
            source(CODE, int(at.timestamp()))
            self.assertEqual(self.encrypt.call_args.args[2], text)

    def test_required_caller_pins_checked_before_private_reads(self):
        with patch('sushiwait.materialsource._read', side_effect=AssertionError('no read')):
            for change in ({'expected_app_id': None}, {'expected_app_id': TARGET+'\n'},
                           {'expected_source_version': True}, {'expected_source_version': 0},
                           {'expected_package_sha256': 'bad'}, {'expected_package_sha256': None}):
                self.error('material_invalid_input', lambda: self.prepare(**change))
        self.backend.assert_not_called()

    def test_valid_but_different_app_or_source_pin_cannot_be_used(self):
        self.error('material_target_mismatch', lambda: self.prepare(expected_app_id='wx1111111111111111'))
        self.error('material_source_mismatch', lambda: self.prepare(expected_source_version=182))
        self.error('material_source_mismatch', lambda: self.prepare(expected_package_sha256='b'*64))
        self.backend.assert_not_called()

    def test_private_file_and_duplicate_json_protection(self):
        self.path.chmod(0o644)
        self.error('material_private_file_invalid')
        self.path.chmod(0o600)
        for body in (b'{"schema_version":1,"schema_version":1}', b'{"v":NaN}', b'not-json', b'\xff'):
            self.path.write_bytes(body)
            self.error('material_bundle_invalid')
        self.backend.assert_not_called()

    def test_bundle_rejects_query_credentials_destination_and_unknown_metadata(self):
        for change in ({'authorization': 'synthetic-query-token'}, {'endpoint': 'https://other.invalid'},
                       {'schema_version': True}, {'timezone': 'UTC'}, {'initialize_content_type': 'text/plain'},
                       {'query_content_type': 'application/json'}, {'long_token': ''},
                       {'referer': 'https://other.invalid'}, {'source_version': False}):
            write_bundle(self.path, {**self.value, **change})
            self.error('material_bundle_invalid')
        self.backend.assert_not_called()

    def test_cipher_parameters_and_expression_are_bounded_and_declarative(self):
        original = self.value['date_header']
        for change in ({'algorithm': 'aes-gcm'}, {'key_hex': '0'*30}, {'iv_hex': '0'*34}, {'base': True},
                       {'base': 2**53}, {'pieces': []}, {'pieces': [{'field': 'eval'}]},
                       {'pieces': [{'literal': '\n'}]}, {'pieces': [{'literal': 'x'*128}]*20},
                       {'pieces': [{'literal': 'x', 'field': 'year'}]}, {'code': 'synthetic-script'}):
            write_bundle(self.path, {**self.value, 'date_header': {**original, **change}})
            self.error('material_bundle_invalid')
        self.backend.assert_not_called()

    def test_changed_bundle_stops_instead_of_adopting_new_signing_key(self):
        source = self.prepare()
        write_bundle(self.path, {**self.value, 'signature_key': 'synthetic-replacement-secret'}, atomic=True)
        self.error('material_bundle_changed', lambda: source(CODE, 1791716400))
        self.encrypt.assert_not_called()

    def test_permission_change_after_preparation_stops_before_material_generation(self):
        source = self.prepare(); self.path.chmod(0o644)
        self.error('material_private_file_invalid', lambda: source(CODE, 1791716400))
        self.encrypt.assert_not_called()

    def test_missing_code_or_invalid_second_does_not_read_or_sign(self):
        source = self.prepare()
        with patch('sushiwait.materialsource._read', side_effect=AssertionError('no read')):
            for code, second in ((None, 1), ('bad code', 1), (CODE, False), (CODE, 0), (CODE, float('nan'))):
                self.error('material_invalid_input', lambda: source(code, second))
        self.encrypt.assert_not_called()

    def test_cipher_failure_or_unsafe_response_does_not_expose_material(self):
        source = self.prepare()
        self.encrypt.side_effect = RuntimeError(self.value['signature_key'])
        self.error('material_generation_failed', lambda: source(CODE, 1791716400))
        self.encrypt.side_effect = None; self.encrypt.return_value = 'bad\nheader'
        self.error('material_generation_failed', lambda: source(CODE, 1791716400))

    def test_bootstrap_and_reconstructed_source_renew_existing_reader_across_midnight(self):
        from sushiwait.client import QueryResult
        from sushiwait.credentials import CredentialSource
        from sushiwait.initialization import ENDPOINT
        from sushiwait.sessionverification import bootstrap_from_yyb, renew_from_yyb
        from sushiwait.yyb import ENDPOINT as SUPPLIER
        from tests.test_initialization import Response
        settings = self.root/'supplier.json'
        legacy_settings = self.root/'renewal-supplier.json'
        common = {'ref': '1', 'protocol_token': 'a'*64}
        write_bundle(settings, {**common, 'schema_version': 2, 'app_id': TARGET})
        write_bundle(legacy_settings, {**common, 'schema_version': 1})
        destination = self.root/'session'
        destination.mkdir(mode=0o700)
        context = destination/'context.json'
        opener, client = Mock(), Mock()
        self.encrypt.side_effect = lambda key, iv, text: base64.b64encode(text).decode('ascii')
        previous_reader = None
        for revision, at, text in (
                (1, datetime(2026, 10, 11, 15, 59, tzinfo=timezone.utc), b'2026|10|11|2973'),
                (2, datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc), b'2026|10|12|2972')):
            claim = {'iat': int(at.timestamp()), 'exp': int(at.timestamp())+3600, 'marker': revision}
            issued_token = 'e30.'+base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip('=')+'.c2ln'
            opener.open.reset_mock()
            opener.open.side_effect = [
                Response(url=SUPPLIER, body=json.dumps({'code': 0, 'data': {'result': {
                    'code': CODE+str(revision), 'errMsg': 'login:ok'}}}).encode()),
                Response(url=ENDPOINT, body=json.dumps({'data': {'auth_token': issued_token}}).encode())]
            client.fetch_store.return_value = QueryResult(True, {'id': '900001', 'name': 'Synthetic midnight',
                'groupQueues': {'mixedQueue': ['0012', '0014', '0012']}}, None, 200, at.isoformat(), at.isoformat(), 0)
            # A new source is prepared solely from the private declarative file.
            source = self.prepare()
            options = {'credentials_file': context, 'store_id': '900001',
                       'query_content_type': source.query_content_type}
            with patch('urllib.request.build_opener', return_value=opener), \
                    patch('sushiwait.sessionverification.SushiroClient', return_value=client), \
                    patch('sushiwait.initialization.datetime') as init_clock, \
                    patch('sushiwait.sessionverification.datetime') as verify_clock, \
                    patch('sushiwait.capture._clock', return_value=at), \
                    patch('socket.socket', side_effect=AssertionError('no real network')) as sockets:
                init_clock.now.return_value = verify_clock.now.return_value = at
                if revision == 1:
                    result = bootstrap_from_yyb(source, settings_file=settings,
                        expected_app_id=TARGET, expected_app_client=source.app_client, **options)
                else:
                    result = renew_from_yyb(source, settings_file=legacy_settings,
                        expected_revision=revision-1, **options)
                self.assertTrue(result['ok'] and result['durability_confirmed'])
                self.assertEqual(opener.open.call_count, 2)
                sockets.assert_not_called()
            initialize = opener.open.call_args_list[1].args[0]
            self.assertEqual(initialize.get_header('Content-type'), 'application/json')
            self.assertEqual(base64.b64decode(initialize.get_header('X-app-code')), text)
            self.assertEqual(json.loads(initialize.data)['js_code'], CODE+str(revision))
            if previous_reader is None:
                previous_reader = CredentialSource(credentials_file=context, api_profile='miniapp_gateway')
            saved = previous_reader.current()
            self.assertEqual(saved.revision, revision)
            self.assertEqual(saved.authorization, 'Bearer '+issued_token)
            self.assertEqual(saved.content_type, 'application/x-www-form-urlencoded')
        self.assertEqual(client.fetch_store.call_count, 2)
        self.socket.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'optional protocol cipher backend')
class CipherBackendTests(unittest.TestCase):
    def test_aes_cbc_nist_first_block_and_pkcs7_aligned_padding(self):
        encrypt = _cipher_backend()
        result = base64.b64decode(encrypt(bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c'),
            bytes.fromhex('000102030405060708090a0b0c0d0e0f'), bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')))
        self.assertEqual(result[:16].hex(), '7649abac8119b246cee98e9b12e9197d')
        self.assertEqual(len(result), 32)

    def test_empty_plaintext_is_a_full_padded_block(self):
        encrypt = _cipher_backend()
        self.assertEqual(len(base64.b64decode(encrypt(b'a'*16, b'b'*16, b''))), 16)


if __name__ == '__main__': unittest.main()
