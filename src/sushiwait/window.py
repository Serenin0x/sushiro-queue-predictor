"""One bounded staging window with an independent shutdown child; never enable."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from .capture import CaptureError, _check_parent, _open_parent
from .credentials import CredentialError, read_credentials_file
from .surge import SurgeError, _app, receive_summary
from .surgeguard import GuardError, read_state


_ERRORS = frozenset({
    'window_guard_protocol_invalid',
    'window_guard_output_timeout',
    'window_guard_not_ready',
    'window_guard_output_limit',
    'window_invalid_input',
    'window_separate_staging_required',
    'window_unsupported_environment',
    'window_baseline_mismatch',
    'window_baseline_changed',
    'window_initial_switch_on_or_unknown',
    'window_staging_result_invalid',
    'window_main_context_changed',
    'window_private_context_invalid',
    'window_native_intake_failed',
    'window_local_operation_failed',
    'window_interrupted',
    'window_runtime_failed',
    'window_staging_unconfirmed',
    'window_cleanup_unconfirmed',
    'window_surge_context_unchanged',
    'window_surge_candidate_not_found',
})


class WindowError(ValueError):
    def __init__(self, error_code: str, *, staging_changed: bool | None = False,
                 cleanup_confirmed: bool = False):
        self.error_code = error_code if error_code in _ERRORS else "window_runtime_failed"
        super().__init__(self.error_code)
        self.staging_changed = staging_changed
        self.cleanup_confirmed = cleanup_confirmed


_FIELDS = {'mitm_enabled', 'capture_enabled', 'auto_mitm'}


def _off(value: dict) -> bool:
    return (type(value) is dict and set(value) == _FIELDS
            and all(type(v) is bool and not v for v in value.values()))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise WindowError('window_guard_protocol_invalid') from None


def _spawn_guard(seconds: int, directory: Path):
    return subprocess.Popen([sys.executable, '-I', '-m', 'sushiwait', 'surge-guard',
                             '--seconds', str(seconds)], cwd=directory,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _read_output(process, *, timeout: float, first_line: bool) -> bytes:
    deadline = time.monotonic() + timeout
    pieces = []
    size = 0
    with selectors.DefaultSelector() as selected:
        selected.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selected.select(remaining):
                raise WindowError('window_guard_output_timeout')
            # One byte at readiness avoids consuming the later completion line.
            part = os.read(process.stdout.fileno(), 1 if first_line else min(4096, 65537 - size))
            if not part:
                if first_line:
                    raise WindowError('window_guard_not_ready')
                break
            size += len(part)
            if size > 65536:
                raise WindowError('window_guard_output_limit')
            pieces.append(part)
            if first_line and part == b'\n':
                break
    return b''.join(pieces)


def _ready(process, seconds: int) -> dict:
    value = _json(_read_output(process, timeout=4, first_line=True))
    try:
        expires = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
        remaining = (expires.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise WindowError('window_guard_protocol_invalid') from None
    if (value.get('event') != 'surge_guard_ready' or type(value.get('seconds')) is not int
            or value['seconds'] != seconds or expires.utcoffset() is None
            or not _off(value.get('initial')) or process.poll() is not None
            or not 1 < remaining <= seconds
            or value.get('can_enable_mitm') is not False
            or value.get('external_network_performed') is not False
            or value.get('credentials_accessed') is not False):
        raise WindowError('window_guard_not_ready')
    return value


def _finish(process) -> bool:
    """Signal only our shutdown child; never kill an unconfirmed protection child."""
    try:
        if process.poll() is None:
            process.terminate()
        body = _read_output(process, timeout=8, first_line=False)
        process.wait(timeout=.1)
        lines = [line for line in body.splitlines() if line]
        if len(lines) != 1:
            return False
        result = _json(lines[0])
        return (process.returncode == 0 and result.get('ok') is True
                and result.get('event') == 'surge_guard_finished'
                and result.get('verified_off') is True and _off(result.get('final')))
    except (OSError, subprocess.SubprocessError, WindowError):
        return False
    finally:
        if process.poll() is not None:
            process.stdout.close()


def _changed(path, previous) -> bool | None:
    try:
        return read_credentials_file(path, api_profile='miniapp_gateway') != previous
    except CredentialError:
        return None


def run_window(*, credentials_file: str | Path, staged_file: str | Path,
               revision: int, seconds: int = 40, collector_paused: bool = False,
               on_ready=None) -> dict:
    """Needs installed package and operator-controlled normal client action.

    Caller must actually pause all relevant collectors and select only SAPI.
    The explicit flag is an acknowledgement, not proof of either prerequisite.
    Current/staging must be different private directories, initially identical.
    Main directory advisory lock coordinates other capture/promotion writers.
    It is not a global Surge lock and cannot bind uncooperative operators.
    """
    if (type(seconds) is not int or not 5 <= seconds <= 40
            or type(revision) is not int or not 1 <= revision < 2**63
            or collector_paused is not True or (on_ready is not None and not callable(on_ready))):
        raise WindowError('window_invalid_input')
    try:
        current_path, stage_path = Path(os.path.abspath(credentials_file)), Path(os.path.abspath(staged_file))
    except (TypeError, ValueError, OSError):
        raise WindowError('window_invalid_input') from None
    if current_path.parent == stage_path.parent:
        raise WindowError('window_separate_staging_required')
    if sys.platform != 'darwin':
        raise WindowError('window_unsupported_environment')
    current = previous = None
    parent_fd = stage_fd = None
    guard = None
    result = None
    failure = None
    cleaned = False
    try:
        current = read_credentials_file(current_path, api_profile='miniapp_gateway')
        previous = read_credentials_file(stage_path, api_profile='miniapp_gateway')
        if current != previous or revision <= current.revision:
            raise WindowError('window_baseline_mismatch')
        parent_fd, _ = _open_parent(current_path, private=True)
        stage_fd, _ = _open_parent(stage_path, private=True)
        if (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino) == (os.fstat(stage_fd).st_dev, os.fstat(stage_fd).st_ino):
            raise WindowError('window_separate_staging_required')
        import fcntl
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _changed(current_path, current) is not False or _changed(stage_path, previous) is not False:
            raise WindowError('window_baseline_changed')
        state = read_state()
        if not _off(state):
            raise WindowError('window_initial_switch_on_or_unknown')
        guard = _spawn_guard(seconds, stage_path.parent)
        ready = _ready(guard, seconds)

        def receiving(_metadata):
            if guard.poll() is not None:
                raise WindowError('window_guard_not_ready')
            expires = datetime.fromisoformat(ready['expires_at'].replace('Z', '+00:00'))
            if (expires - datetime.now(timezone.utc)).total_seconds() <= 1:
                raise WindowError('window_guard_not_ready')
            if on_ready:
                on_ready({'event': 'context_window_ready', 'seconds': seconds,
                          'expires_at': ready['expires_at'], 'baseline_revision': current.revision,
                          'requested_revision': revision, 'normal_client_action_required': True,
                          'collector_pause_verified': False, 'hostnames_verified': False,
                          'debug_enabled_by_tool': False, 'raw_logging': False,
                          'main_context_updated': False, 'external_network_performed': False})

        result = receive_summary(credentials_file=stage_path, revision=revision,
                                 seconds=seconds, on_ready=receiving)
        if (result.get('committed') is not True or result.get('revision') != revision
                or type(result.get('durability_confirmed')) is not bool):
            raise WindowError('window_staging_result_invalid')
        _check_parent(current_path, parent_fd, private=True)
        _check_parent(stage_path, stage_fd, private=True)
        if _changed(current_path, current) is not False:
            raise WindowError('window_main_context_changed')
    except WindowError as error:
        failure = error.error_code
    except (CredentialError, CaptureError):
        failure = 'window_private_context_invalid'
    except SurgeError as error:
        failure = ('window_' + error.error_code if error.error_code in
                   {'surge_context_unchanged', 'surge_candidate_not_found'} else 'window_native_intake_failed')
    except GuardError:
        failure = 'window_native_intake_failed'
    except (OSError, ValueError, subprocess.SubprocessError):
        failure = 'window_local_operation_failed'
    except KeyboardInterrupt:
        failure = 'window_interrupted'
    except Exception:
        failure = 'window_runtime_failed'
    finally:
        if guard is not None:
            try:
                cleaned = _finish(guard)
            except Exception:
                cleaned = False
            if cleaned:
                try:
                    cleaned = _off(read_state())
                except GuardError:
                    cleaned = False
        # Verify again after cleanup, while the main advisory lock is held.
        if result is not None and failure is None and cleaned:
            try:
                _check_parent(current_path, parent_fd, private=True)
                _check_parent(stage_path, stage_fd, private=True)
                candidate = read_credentials_file(stage_path, api_profile='miniapp_gateway')
                if _changed(current_path, current) is not False:
                    failure = 'window_main_context_changed'
                elif (candidate.revision != revision or candidate.authorization == previous.authorization
                      or _app(candidate.referer) != _app(current.referer)
                      or candidate.app_client != current.app_client):
                    failure = 'window_staging_unconfirmed'
            except (CredentialError, CaptureError):
                failure = 'window_staging_unconfirmed'
        for descriptor in (stage_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)
    changed = _changed(stage_path, previous) if previous is not None else False
    if failure or not cleaned:
        raise WindowError(failure or 'window_cleanup_unconfirmed', staging_changed=changed,
                          cleanup_confirmed=cleaned) from None
    return {'event': 'staged_context_window_finished', 'staging_committed': True,
            'durability_confirmed': result['durability_confirmed'], 'staged_revision': revision,
            'debug_off_confirmed': True, 'main_context_updated': False, 'staging_preserved': True,
            'hostnames_restored': False, 'client_updated': False, 'server_acceptance': 'unverified',
            'external_network_performed': False, 'debug_enabled_by_tool': False, 'raw_logging': False}
