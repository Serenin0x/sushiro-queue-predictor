"""Private, target-pinned initialization material source for a prepared runtime.

No official values, package reader, export, network or query credential are
provided. A separately authorized audited bundle supplies the complete inputs.
The date header algorithm is protocol compatibility, not file encryption.
"""

from datetime import datetime
import base64
import hashlib
import hmac
import json
import re
import secrets
from zoneinfo import ZoneInfo

from .bridge import _app_id
from .credentials import CredentialError, _read_private_file


MAX_BYTES = 16 * 1024
_KEYS = {'schema_version', 'target_app_id', 'source_version', 'source_package_sha256',
         'timezone', 'signature_key', 'long_token', 'app_client', 'device_id',
         'app_version', 'user_agent', 'referer', 'initialize_content_type',
         'query_content_type', 'date_header'}
_ERRORS = {'material_invalid_input', 'material_private_file_invalid', 'material_bundle_invalid',
           'material_target_mismatch', 'material_source_mismatch', 'material_bundle_changed',
           'material_cipher_unavailable', 'material_generation_failed'}


class MaterialSourceError(ValueError):
    def __init__(self, code):
        self.error_code = code if code in _ERRORS else 'material_invalid_input'
        super().__init__(self.error_code)


def _text(value, maximum=4096, *, empty=False):
    return (type(value) is str and (0 if empty else 1) <= len(value) <= maximum
            and all(32 <= ord(c) <= 126 for c in value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError('nonfinite')


def _read(path):
    failed = False
    try:
        body = _read_private_file(path)
    except CredentialError:
        failed = True
    if failed:
        raise MaterialSourceError('material_private_file_invalid')
    if len(body) > MAX_BYTES:
        raise MaterialSourceError('material_bundle_invalid')
    return body


def _bundle(body):
    failed = False
    value = None
    try:
        value = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        failed = True
    if failed or type(value) is not dict or set(value) != _KEYS:
        raise MaterialSourceError('material_bundle_invalid')
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['source_version']) is not int or not 1 <= value['source_version'] < 2**31
            or type(value['target_app_id']) is not str or re.fullmatch(r'wx[0-9a-f]{16}', value['target_app_id']) is None
            or type(value['source_package_sha256']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', value['source_package_sha256']) is None
            or value['timezone'] != 'Asia/Shanghai'
            or any(not _text(value[k]) for k in ('signature_key', 'long_token', 'app_client',
                       'device_id', 'app_version', 'user_agent', 'referer'))
            or _app_id(value['referer']) != value['target_app_id']
            or value['initialize_content_type'] != 'application/json'
            or value['query_content_type'] != 'application/x-www-form-urlencoded'):
        raise MaterialSourceError('material_bundle_invalid')
    header = value['date_header']
    if (type(header) is not dict or set(header) != {'algorithm', 'key_hex', 'iv_hex', 'base', 'pieces'}
            or header['algorithm'] != 'aes-cbc-pkcs7-base64'
            or type(header['key_hex']) is not str or re.fullmatch(r'(?:[0-9a-f]{32}|[0-9a-f]{48}|[0-9a-f]{64})', header['key_hex']) is None
            or type(header['iv_hex']) is not str or re.fullmatch(r'[0-9a-f]{32}', header['iv_hex']) is None
            or type(header['base']) is not int or abs(header['base']) >= 2**53
            or type(header['pieces']) is not list or not 1 <= len(header['pieces']) <= 64):
        raise MaterialSourceError('material_bundle_invalid')
    for piece in header['pieces']:
        if (type(piece) is not dict or not (
                set(piece) == {'literal'} and _text(piece['literal'], 128, empty=True)
                or set(piece) == {'field'} and type(piece['field']) is str
                   and piece['field'] in {'year', 'month', 'day', 'derived'})):
            raise MaterialSourceError('material_bundle_invalid')
    if sum(len(p['literal']) if 'literal' in p else 32 for p in header['pieces']) > 2048:
        raise MaterialSourceError('material_bundle_invalid')
    return value


def _cipher_backend():
    failed = False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except Exception:
        failed = True
    if failed:
        raise MaterialSourceError('material_cipher_unavailable')

    def encrypt(key, iv, plaintext):
        padder = PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode('ascii')
    return encrypt


def prepare_material_source(*, bundle_file, expected_app_id, expected_source_version,
                            expected_package_sha256):
    """Prepare normal requests from a private audited bundle, without old auth.

    Mandatory caller pins are separate from bundle declarations. Each use
    rereads the guarded file; changed bytes stop rather than silently adopting
    new material. This validates a declared source, not an audit attestation.
    Returned body/headers are sensitive and must stay inside the init pipeline.
    """
    if (type(expected_app_id) is not str or re.fullmatch(r'wx[0-9a-f]{16}', expected_app_id) is None
            or type(expected_source_version) is not int or not 1 <= expected_source_version < 2**31
            or type(expected_package_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}', expected_package_sha256) is None):
        raise MaterialSourceError('material_invalid_input')
    original = _read(bundle_file)
    value = _bundle(original)
    if value['target_app_id'] != expected_app_id:
        raise MaterialSourceError('material_target_mismatch')
    if value['source_version'] != expected_source_version or value['source_package_sha256'] != expected_package_sha256:
        raise MaterialSourceError('material_source_mismatch')
    digest = hashlib.sha256(original).digest()
    encrypt = _cipher_backend()
    zone = ZoneInfo(value['timezone'])

    def source(code, second):
        if (not _text(code) or any(ord(c) < 33 for c in code)
                or type(second) is not int or not 1 <= second <= 253402271999):
            raise MaterialSourceError('material_invalid_input')
        if not hmac.compare_digest(hashlib.sha256(_read(bundle_file)).digest(), digest):
            raise MaterialSourceError('material_bundle_changed')
        failed = False
        try:
            date = datetime.fromtimestamp(second, zone)
            header = value['date_header']
            fields = {'year': date.year, 'month': date.month, 'day': date.day,
                      'derived': header['base'] - date.year + date.month - date.day}
            plaintext = ''.join(p['literal'] if 'literal' in p else str(fields[p['field']])
                                for p in header['pieces']).encode('ascii')
            app_code = encrypt(bytes.fromhex(header['key_hex']), bytes.fromhex(header['iv_hex']), plaintext)
            if not _text(app_code):
                raise ValueError('cipher result')
            body = {'long_token': value['long_token'], 'js_code': code, 'device_id': value['device_id'],
                    'app_version': value['app_version'], 'ts': second,
                    'nonce': str(secrets.randbelow(1000000)).zfill(6)}
            message = '&'.join(f'{key}={body[key]}' for key in sorted(body))
            body['sign'] = hmac.new(value['signature_key'].encode('ascii'), message.encode('ascii'), hashlib.sha256).hexdigest()
            heads = {'X-App-Client': value['app_client'], 'X-App-Code': app_code,
                     'User-Agent': value['user_agent'], 'Referer': value['referer'],
                     'Content-Type': value['initialize_content_type']}
        except Exception:
            failed = True
        if failed:
            raise MaterialSourceError('material_generation_failed')
        return body, heads

    source.target_app_id = expected_app_id
    source.app_client = value['app_client']
    source.query_content_type = value['query_content_type']
    return source
