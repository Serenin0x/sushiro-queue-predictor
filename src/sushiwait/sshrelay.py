"""One explicit public-packet delivery over an independently approved SSH key.

No key creation, login refresh, source queries, cursor movement or retries.
An unknown delivery keeps the original packet for an idempotent retry.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import ipaddress
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

from .capture import _open_parent, _check_parent
from .credentials import _identity, _private_file
from .monitoring import _stamp, _time
from .packets import encoded, _write_packet
from .receipts import read_packet
from .ssharchive import COMMAND, validate_receipt
from .storage import _unique_object

MAX_REPLY_BYTES = 262144


class SSHRelayError(ValueError):
    def __init__(self, code, *, network_attempted=False, local_confirmation_may_exist=False):
        self.error_code = code
        self.network_attempted = network_attempted
        # Once SSH was started, a missing/invalid reply cannot prove no commit.
        self.remote_archive_may_have_committed = network_attempted
        self.local_confirmation_may_exist = local_confirmation_may_exist
        super().__init__(code)


def _target(value):
    if type(value) is not str or len(value) > 290 or value.count('@') != 1:
        raise SSHRelayError('ssh_relay_invalid_target')
    user, host = value.split('@')
    if re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]{0,31}', user) is None:
        raise SSHRelayError('ssh_relay_invalid_target')
    try:
        valid = isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        valid = (not re.fullmatch(r'[0-9.]+', host) and len(host) <= 253 and all(re.fullmatch(
            r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
            for label in host.split('.')))
    if not valid:
        raise SSHRelayError('ssh_relay_invalid_target')
    return value


def _path(value):
    try:
        path = Path(os.path.abspath(value))
        if any(ord(c) < 32 or ord(c) == 127 for c in str(path)):
            raise ValueError
        return path
    except (TypeError, ValueError, OSError):
        raise SSHRelayError('ssh_relay_invalid_path') from None


@contextmanager
def _guarded_file(path, maximum):
    """Hold metadata guards without reading any private SSH-key bytes."""
    parent = descriptor = None
    try:
        parent, name = _open_parent(path, private=True)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        before = os.fstat(descriptor)
        if not _private_file(before) or not 0 < before.st_size <= maximum:
            raise SSHRelayError('ssh_relay_unsafe_access_file')

        def check():
            _check_parent(path, parent, private=True)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (_identity(before) != _identity(named)
                    or _identity(before) != _identity(os.fstat(descriptor))):
                raise SSHRelayError('ssh_relay_access_file_changed')

        check()
        yield check, descriptor
        check()
    except SSHRelayError:
        raise
    except Exception:
        raise SSHRelayError('ssh_relay_unsafe_access_file') from None
    finally:
        for fd in (descriptor, parent):
            if fd is not None:
                os.close(fd)


@contextmanager
def _guarded_public_file(path, maximum):
    """Guard public packet/receipt bytes without treating ctime as content.

    The private key and pinned-host files keep the original metadata guard.
    Public files use stable identity, permissions, size and mtime plus complete
    descriptor hashes before and after each use, including same-mtime rewrites.
    """
    parent = descriptor = None
    try:
        parent, name = _open_parent(path, private=True)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        before = os.fstat(descriptor)
        if not _private_file(before) or not 0 < before.st_size <= maximum:
            raise SSHRelayError('ssh_relay_unsafe_access_file')
        identity = _identity(before)[:-1]

        def metadata():
            _check_parent(path, parent, private=True)
            for current in (os.fstat(descriptor),
                            os.stat(name, dir_fd=parent, follow_symlinks=False)):
                if not _private_file(current) or _identity(current)[:-1] != identity:
                    raise SSHRelayError('ssh_relay_access_file_changed')

        def digest():
            value, offset = hashlib.sha256(), 0
            while offset < before.st_size:
                part = os.pread(descriptor, min(131072, before.st_size-offset), offset)
                if not part:
                    raise SSHRelayError('ssh_relay_access_file_changed')
                value.update(part); offset += len(part)
            metadata()
            return value.digest()

        metadata()
        original = digest()

        def check():
            metadata()
            if digest() != original:
                raise SSHRelayError('ssh_relay_access_file_changed')

        check()
        yield check, descriptor
        check()
    except SSHRelayError:
        raise
    except Exception:
        raise SSHRelayError('ssh_relay_unsafe_access_file') from None
    finally:
        for fd in (descriptor, parent):
            if fd is not None:
                os.close(fd)


def _argv(target, key_file, known_hosts_file, command=COMMAND):
    # OpenSSH parses -o values as configuration text, including its quoting.
    pinned = '"' + str(known_hosts_file).replace('\\', '\\\\').replace('"', '\\"') + '"'
    options = {'BatchMode': 'yes', 'IdentitiesOnly': 'yes', 'IdentityAgent': 'none',
        'PreferredAuthentications': 'publickey', 'PasswordAuthentication': 'no',
        'KbdInteractiveAuthentication': 'no', 'StrictHostKeyChecking': 'yes',
        'UserKnownHostsFile': pinned, 'GlobalKnownHostsFile': '/dev/null',
        'HostKeyAlgorithms': 'ssh-ed25519', 'ClearAllForwardings': 'yes',
        'ConnectTimeout': '5', 'ConnectionAttempts': '1',
        'ServerAliveInterval': '5', 'ServerAliveCountMax': '2'}
    args = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(key_file)]
    for key, value in options.items():
        args += ['-o', key + '=' + value]
    return [*args, target, command]


def _exchange(process, body, timeout):
    """Bound both pipe directions; a stalled reader cannot block stdin.write."""
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    sent, received, chunks = 0, 0, []
    try:
        for stream, event in ((process.stdin, selectors.EVENT_WRITE),
                              (process.stdout, selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SSHRelayError('ssh_relay_deadline')
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                if stream is process.stdin:
                    try:
                        amount = os.write(stream.fileno(), body[sent:sent + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise SSHRelayError('ssh_relay_incomplete_input') from None
                    sent += amount
                    if sent == len(body):
                        selector.unregister(stream); stream.close()
                else:
                    try:
                        part = os.read(stream.fileno(), min(65536, MAX_REPLY_BYTES + 1 - received))
                    except BlockingIOError:
                        continue
                    if not part:
                        selector.unregister(stream); stream.close()
                    else:
                        received += len(part)
                        if received > MAX_REPLY_BYTES:
                            raise SSHRelayError('ssh_relay_reply_limit')
                        chunks.append(part)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SSHRelayError('ssh_relay_deadline')
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise SSHRelayError('ssh_relay_deadline') from None
        if code != 0 or sent != len(body):
            raise SSHRelayError('ssh_relay_transport_exit')
        return b''.join(chunks)
    finally:
        selector.close()


def _close(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)
    for stream in (process.stdin, process.stdout):
        if stream and not stream.closed:
            stream.close()


def deliver_public_packet(packet_file, *, ssh_key_file, known_hosts_file, target,
                          confirmation_file, timeout=30, clock=None, _spawn=None,
                          with_continuity=False):
    """Deliver once, verify an exact durable receipt, then save it locally.

    The operator must separately approve the destination and configure a
    restricted key plus pinned host. This function cannot create that access.
    No query credentials or original source response are opened or sent.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    process, attempted, write_attempted = None, False, False
    try:
        if (os.name != 'posix' or type(with_continuity) is not bool or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 1 <= timeout <= 60):
            raise SSHRelayError('ssh_relay_invalid_bounds')
        destination = _target(target)
        paths = [_path(p) for p in (packet_file, ssh_key_file, known_hosts_file, confirmation_file)]
        source, key_file, hosts_file, confirmation = paths
        if len(set(paths)) != 4:
            raise SSHRelayError('ssh_relay_conflicting_paths')
        now = _time(clock().isoformat())
        with ExitStack() as stack:
            guards = [stack.enter_context(_guarded_public_file(source, 4 * 1024 * 1024)),
                      stack.enter_context(_guarded_file(key_file, 16384)),
                      stack.enter_context(_guarded_file(hosts_file, 262144))]
            parent, name = _open_parent(confirmation, private=True)
            stack.callback(os.close, parent)
            _check_parent(confirmation, parent, private=True)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SSHRelayError('ssh_relay_confirmation_exists')
            reader, validator, command = read_packet, validate_receipt, COMMAND
            if with_continuity:
                from .continuity import read_continuity_bundle
                from .continuitytransport import COMMAND as BUNDLE_COMMAND, validate_bundle_receipt
                reader, validator, command = read_continuity_bundle, validate_bundle_receipt, BUNDLE_COMMAND
            value = reader(source, as_of=now.isoformat())
            packet = value['packet'] if with_continuity else value
            if packet['api_profile'] != 'miniapp_gateway' or not packet['records']:
                raise SSHRelayError('ssh_relay_invalid_packet_scope')
            for check, _ in guards:
                check()
            body = encoded(value)
            spawn = subprocess.Popen if _spawn is None else _spawn
            process = spawn(_argv(destination, key_file, hosts_file, command),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            attempted = True
            raw = _exchange(process, body, timeout)
            for check, _ in guards:
                check()
            try:
                receipt = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                validator(receipt, value)
            except Exception:
                raise SSHRelayError('ssh_relay_receipt_mismatch') from None
            acknowledged = _time(clock().isoformat())
            if acknowledged < now:
                raise SSHRelayError('ssh_relay_clock_reversed')
            confirmation_body = {'relay_confirmation_schema_version': 1,
                'transport': 'openssh_forced_command_pinned_ed25519',
                'acknowledged_at': _stamp(acknowledged), 'receipt': receipt}
            write_attempted = True
            written = _write_packet(encoded(confirmation_body), confirmation)
            if written['durability_confirmed'] is not True:
                raise SSHRelayError('ssh_relay_local_confirmation_unconfirmed')
        result = {'ok': True, 'network_attempted': True, 'server_archive_commit_confirmed': True,
                'confirmation_saved': True, 'durability_confirmed': True,
                'packet_sha256': receipt['packet_sha256'], 'records': len(packet['records']),
                'inserted_records': receipt['inserted_records'],
                'duplicate_records': receipt['duplicate_records'],
                'original_packet_preserved': True, 'query_credentials_accessed': False,
                'source_claims_verified': False, 'retry_performed': False}
        if with_continuity:
            result.update(bundle_sha256=receipt['bundle_sha256'],
                records_including_anchors=len(receipt['observations']), links=len(receipt['links']),
                inserted_links=receipt['inserted_links'], duplicate_links=receipt['duplicate_links'])
        return result
    except SSHRelayError as error:
        raise SSHRelayError(error.error_code, network_attempted=attempted,
                            local_confirmation_may_exist=write_attempted) from None
    except Exception:
        raise SSHRelayError('ssh_relay_input_or_transport_error', network_attempted=attempted,
                            local_confirmation_may_exist=write_attempted) from None
    finally:
        _close(process)


def check_public_confirmation(packet_file, confirmation_file, *, as_of, with_continuity=False):
    """Recheck a saved exact receipt without SSH, key access or cursor changes.

    This establishes historical correspondence only. It cannot prove that the
    current server still retains the archive or authenticate a new connection.
    """
    try:
        if type(with_continuity) is not bool:
            raise SSHRelayError('ssh_relay_invalid_bounds')
        current = _time(as_of)
        source, confirmation = [_path(p) for p in (packet_file, confirmation_file)]
        if source == confirmation:
            raise SSHRelayError('ssh_relay_conflicting_paths')
        with ExitStack() as stack:
            guards = [stack.enter_context(_guarded_public_file(p, size)) for p, size in
                ((source, 4 * 1024 * 1024), (confirmation, MAX_REPLY_BYTES + 1024))]
            reader, validator = read_packet, validate_receipt
            if with_continuity:
                from .continuity import read_continuity_bundle
                from .continuitytransport import validate_bundle_receipt
                reader, validator = read_continuity_bundle, validate_bundle_receipt
            source_value = reader(source, as_of=_stamp(current))
            packet = source_value['packet'] if with_continuity else source_value
            if packet['api_profile'] != 'miniapp_gateway' or not packet['records']:
                raise SSHRelayError('ssh_relay_invalid_packet_scope')
            _, fd = guards[1]
            chunks, remaining = [], MAX_REPLY_BYTES + 1025
            while remaining:
                part = os.read(fd, remaining)
                if not part:
                    break
                chunks.append(part); remaining -= len(part)
            for check, _ in guards:
                check()
            value = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (type(value) is not dict or set(value) != {
                'relay_confirmation_schema_version', 'transport', 'acknowledged_at', 'receipt'}
                or type(value['relay_confirmation_schema_version']) is not int
                or value['relay_confirmation_schema_version'] != 1
                or value['transport'] != 'openssh_forced_command_pinned_ed25519'):
                raise SSHRelayError('ssh_relay_confirmation_invalid')
            acknowledged = _time(value['acknowledged_at'])
            if (_stamp(acknowledged) != value['acknowledged_at']
                    or not _time(packet['as_of']) <= acknowledged <= current):
                raise SSHRelayError('ssh_relay_confirmation_time_invalid')
            receipt = validator(value['receipt'], source_value)
        result = {'ok':True, 'historical_confirmation_only':True,
            'exact_packet_receipt_match':True, 'confirmed_records':len(packet['records']),
            'packet_sha256':receipt['packet_sha256'], 'network_attempted':False,
            'current_server_retention_verified':False, 'query_credentials_accessed':False,
            'ssh_key_accessed':False, 'cursor_advanced':False, 'original_packet_preserved':True}
        if with_continuity:
            result.update(bundle_sha256=receipt['bundle_sha256'],
                confirmed_records_including_anchors=len(receipt['observations']),
                confirmed_links=len(receipt['links']))
        return result
    except SSHRelayError:
        raise
    except Exception:
        raise SSHRelayError('ssh_relay_confirmation_invalid') from None
