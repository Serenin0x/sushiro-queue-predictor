"""One fixed private bundle upload; intended as an SSH forced command.

No account database, shell command, URL, token refresh, package scan or API
request is accepted. Install/configure only after the owner approves the new
cloud material destination. Never use the existing public archive SSH key.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import sys
import time

from sushiwait.credentials import _read_private_file
from sushiwait.materialsource import MAX_BYTES, _bundle
from sushiwait.packets import _write_packet, encoded


def receive(root, body):
    """Validate against separately installed pins, then publish once."""
    root = Path(root)
    if not root.is_absolute() or len(body) > MAX_BYTES:
        raise ValueError('material_upload_invalid')
    pins = json.loads(_read_private_file(root / 'source-pins.json'))
    if set(pins) != {'app_id', 'source_version', 'package_sha256'}:
        raise ValueError('material_upload_invalid')
    bundle = _bundle(body)
    if (bundle['target_app_id'] != pins['app_id']
            or bundle['source_version'] != pins['source_version']
            or bundle['source_package_sha256'] != pins['package_sha256']):
        raise ValueError('material_upload_mismatch')
    target = root / 'initialization-material.json'
    # Atomic hard-link publication rejects any existing name without reading
    # it, including a file created concurrently after validation.
    result = _write_packet(encoded(bundle), target)
    return {'ok': result.get('durability_confirmed') is True,
            'committed': result.get('committed') is True,
            'durability_confirmed': result.get('durability_confirmed') is True,
            'automatic_retry': False,
            'official_code_executed': False, 'origin_queries_added': 0}


def read_stdin(timeout=8):
    deadline = time.monotonic() + timeout
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            piece = os.read(sys.stdin.fileno(), min(4096, MAX_BYTES + 1 - len(data)))
            if not piece:
                if data:
                    return bytes(data)
                break
            data.extend(piece)
            if len(data) > MAX_BYTES:
                break
    raise ValueError('material_upload_input_invalid')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if os.environ.get('SSH_ORIGINAL_COMMAND', ''):
            raise ValueError('material_upload_command_rejected')
        result = receive(args.root, read_stdin())
    except Exception as error:
        # Do not print paths, original exceptions, bundle hashes or values.
        print(json.dumps({'ok': False, 'error_code': 'material_upload_rejected',
                          'committed': getattr(error, 'committed', False) is True,
                          'automatic_retry': False, 'origin_queries_added': 0}))
        return 1
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
