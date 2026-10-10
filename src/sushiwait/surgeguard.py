"""Bounded, explicit local MitM shutdown; never enable a feature or read credentials."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

_CLI = "/Applications/Surge.app/Contents/Applications/surge-cli"
_MAX_BYTES = 65536
_COMMAND_TIMEOUT = 3.0
_SHUTDOWN_COMMANDS = (("set", "MitMEnabled=0"), ("set", "Replica=0"))
_COMMANDS = (("environment", "--raw"), *_SHUTDOWN_COMMANDS)


class GuardError(Exception):
    def __init__(self, error_code: str, *, cleanup_attempted: bool = False,
                 cleanup_confirmed: bool = False):
        super().__init__(error_code)
        self.error_code = error_code
        self.cleanup_attempted = cleanup_attempted
        self.cleanup_confirmed = cleanup_confirmed


def _command(arguments: tuple[str, ...]) -> bytes:
    if arguments not in _COMMANDS:
        raise GuardError("surge_guard_command_forbidden")
    if sys.platform != "darwin":
        raise GuardError("surge_guard_unsupported_environment")
    if not os.path.isfile(_CLI) or not os.access(_CLI, os.X_OK):
        raise GuardError("surge_guard_unavailable")
    process = None
    try:
        process = subprocess.Popen([_CLI, *arguments], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + _COMMAND_TIMEOUT
        parts: list[bytes] = []
        size = 0
        with selectors.DefaultSelector() as selected:
            selected.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selected.select(remaining):
                    raise GuardError("surge_guard_cli_timeout")
                data = os.read(process.stdout.fileno(), min(4096, _MAX_BYTES + 1 - size))
                if not data:
                    break
                size += len(data)
                if size > _MAX_BYTES:
                    raise GuardError("surge_guard_cli_output_limit")
                parts.append(data)
        if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise GuardError("surge_guard_cli_failed")
        return b"".join(parts)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GuardError("surge_guard_cli_failed") from error
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is str and value in ("0", "1"):
        return value == "1"
    raise GuardError("surge_guard_unknown_state")


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise GuardError("surge_guard_unknown_state")
        result[key] = value
    return result


def read_state() -> dict[str, bool]:
    """Only three feature states may leave this parser, never the raw environment."""
    try:
        value = json.loads(_command(_COMMANDS[0]).decode("utf-8"), object_pairs_hook=_pairs)
        environment = value["environment"]
        return {"mitm_enabled": _boolean(environment["MitMEnabled"]),
                "capture_enabled": _boolean(environment["Replica"]),
                "auto_mitm": _boolean(environment["ReplicaSessionParameters"]["mitmOverride"])}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise GuardError("surge_guard_unknown_state") from error


def _shutdown() -> dict[str, bool]:
    # Always try both OFF operations: a failed MitM call must not leave disk
    # capture running. Neither command can enable a feature or quit Surge.
    failed = False
    for command in _SHUTDOWN_COMMANDS:
        try:
            _command(command)
        except Exception:
            failed = True
    try:
        result = read_state()
    except Exception:
        raise GuardError("surge_guard_cleanup_unconfirmed", cleanup_attempted=True) from None
    if failed or any(result.values()):
        raise GuardError("surge_guard_cleanup_unconfirmed", cleanup_attempted=True)
    return result


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_guard(*, seconds: int, on_ready: Callable[[dict], None] | None = None) -> dict:
    """Run as a separate CLI process before a separately authorized operator enables MitM.

    This is a deadline for issuing OFF, not a guarantee against process kill, OS sleep,
    an unavailable controller or a subsequent operator enabling it after this process ends.
    It does not inspect/change hostnames or restore the operator's original host list.
    """
    if type(seconds) is not int or not 1 <= seconds <= 45:
        raise GuardError("surge_guard_invalid_window")
    if threading.current_thread() is not threading.main_thread():
        raise GuardError("surge_guard_main_thread_required")
    initial = read_state()
    if any(initial.values()):
        # Existing unrelated debugging must never be taken over.
        raise GuardError("surge_guard_initial_switch_on")
    stopped = threading.Event()
    previous = {}
    start_mono, start_wall = time.monotonic(), time.time()
    armed = False
    failure: str | None = None
    finished = None

    def stop(_number: int, _frame: object) -> None:
        stopped.set()

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop)
        armed = True
        if on_ready is not None:
            on_ready({"event": "surge_guard_ready", "seconds": seconds, "started_at": _utc(),
                      "expires_at": datetime.fromtimestamp(start_wall + seconds, timezone.utc)
                                            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                      "initial": initial, "can_enable_mitm": False,
                      "external_network_performed": False, "credentials_accessed": False})
        last_wall = start_wall
        while not stopped.is_set():
            elapsed = time.monotonic() - start_mono
            wall = time.time()
            if wall < last_wall or elapsed < 0:
                failure = "surge_guard_clock_changed"
                break
            if elapsed >= seconds or wall - start_wall >= seconds:
                break
            last_wall = wall
            stopped.wait(min(.1, seconds - elapsed, seconds - (wall - start_wall)))
    except KeyboardInterrupt:
        stopped.set()
    except Exception:
        failure = "surge_guard_runtime_failed"
    finally:
        # A ready-output exception or normal-client failure must still issue OFF.
        if armed:
            try:
                finished = _shutdown()
            except Exception:
                failure = "surge_guard_cleanup_unconfirmed"
        for number, handler in previous.items():
            signal.signal(number, handler)
    if failure is not None:
        raise GuardError(failure, cleanup_attempted=armed, cleanup_confirmed=finished is not None)
    return {"event": "surge_guard_finished", "verified_off": True, "final": finished,
            "elapsed_seconds": round(time.monotonic() - start_mono, 3), "finished_at": _utc(),
            "reason": "signal" if stopped.is_set() else "deadline",
            "external_network_performed": False, "credentials_accessed": False,
            "hostnames_restored": False, "client_updated": False}
