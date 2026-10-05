"""Offline fault checks: feature-state fakes and bounded, real local child processes."""
import contextlib
import io
import json
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from sushiwait import surgeguard as guard
from sushiwait.cli import main

OFF = {"mitm_enabled": False, "capture_enabled": False, "auto_mitm": False}
PRIVATE = "synthetic-private-environment-marker"


def environment(mitm=False, capture=False, override=False):
    return json.dumps({"environment": {"MitMEnabled": mitm, "Replica": capture,
        "ReplicaSessionParameters": {"mitmOverride": override, "ignored": PRIVATE},
        "private_field": PRIVATE}, "ignored": PRIVATE}).encode()


class StateTests(unittest.TestCase):
    def test_real_boolean_integer_and_string_forms(self):
        for value in (False, 0, "0"):
            with self.subTest(value=value), patch.object(guard, "_command", return_value=environment(value)):
                self.assertEqual(guard.read_state(), OFF)
        for value in (True, 1, "1"):
            with self.subTest(value=value), patch.object(guard, "_command", return_value=environment(value)):
                self.assertTrue(guard.read_state()["mitm_enabled"])

    def test_non_boolean_forms_stop(self):
        for value in (None, 0.0, 1.0, -1, 2, "false", "true", "", [], {}):
            with self.subTest(value=value), patch.object(guard, "_command", return_value=environment(value)):
                with self.assertRaises(guard.GuardError) as caught:
                    guard.read_state()
                self.assertEqual(caught.exception.error_code, "surge_guard_unknown_state")
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_raw_environment_never_leaves_parser(self):
        with patch.object(guard, "_command", return_value=environment()):
            result = guard.read_state()
        self.assertEqual(set(result), set(OFF))
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_missing_malformed_unicode_and_duplicate_keys_stop(self):
        inputs = [b"{}", b"[]", b"null", b"not-json", b"\xff", b'{"environment":0}',
                  b'{"environment":{},"environment":{}}',
                  b'{"environment":{"MitMEnabled":false,"Replica":false,"ReplicaSessionParameters":{"mitmOverride":false,"mitmOverride":true}}}']
        for body in inputs:
            with self.subTest(body=body), patch.object(guard, "_command", return_value=body):
                with self.assertRaises(guard.GuardError):
                    guard.read_state()

    def test_shutdown_requires_all_states_off(self):
        for key in OFF:
            state = {**OFF, key: True}
            with self.subTest(key=key), patch.object(guard, "_command") as command, \
                    patch.object(guard, "read_state", return_value=state):
                with self.assertRaises(guard.GuardError) as caught:
                    guard._shutdown()
                self.assertEqual(caught.exception.error_code, "surge_guard_cleanup_unconfirmed")
                command.assert_called_once_with(("set", "MitMEnabled=0"))


class CommandTests(unittest.TestCase):
    def actual_child(self, code, *, timeout=1, limit=65536):
        original = subprocess.Popen
        spawned = []

        def launch(args, **options):
            self.assertIn(tuple(args[1:]), guard._COMMANDS)
            self.assertEqual(args[0], guard._CLI)
            process = original([sys.executable, "-c", code], **options)
            spawned.append(process)
            return process

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(guard.sys, "platform", "darwin"))
            stack.enter_context(patch.object(guard.os.path, "isfile", return_value=True))
            stack.enter_context(patch.object(guard.os, "access", return_value=True))
            stack.enter_context(patch.object(guard.subprocess, "Popen", side_effect=launch))
            stack.enter_context(patch.object(guard, "_COMMAND_TIMEOUT", timeout))
            stack.enter_context(patch.object(guard, "_MAX_BYTES", limit))
            try:
                return guard._command(("environment", "--raw"))
            finally:
                for process in spawned:
                    self.assertIsNotNone(process.poll())
                    self.assertTrue(process.stdout.closed)

    def test_real_child_success_is_read_and_reaped(self):
        body = environment()
        self.assertEqual(self.actual_child(f"import os;os.write(1,{body!r})"), body)

    def test_hanging_real_child_times_out_and_is_reaped(self):
        with self.assertRaises(guard.GuardError) as caught:
            self.actual_child("import time;time.sleep(10)", timeout=.1)
        self.assertEqual(caught.exception.error_code, "surge_guard_cli_timeout")

    def test_large_real_child_is_stopped_before_collecting_all_output(self):
        with self.assertRaises(guard.GuardError) as caught:
            self.actual_child("import os,time;os.write(1,b'x'*1000000);time.sleep(10)", limit=1024)
        self.assertEqual(caught.exception.error_code, "surge_guard_cli_output_limit")

    def test_nonzero_real_child_has_fixed_error_and_no_stderr(self):
        with self.assertRaises(guard.GuardError) as caught:
            self.actual_child(f"import os;os.write(2,{PRIVATE.encode()!r});raise SystemExit(7)")
        self.assertEqual(caught.exception.error_code, "surge_guard_cli_failed")
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_forbidden_commands_do_not_spawn(self):
        for args in [("set", "MitMEnabled=1"), ("set", "Replica=0"), ("dump", "request", "--raw"),
                     ("environment",), ("set", "MitMEnabled", "0")]:
            with self.subTest(args=args), patch.object(guard.subprocess, "Popen") as spawn:
                with self.assertRaises(guard.GuardError) as caught:
                    guard._command(args)
                self.assertEqual(caught.exception.error_code, "surge_guard_command_forbidden")
                spawn.assert_not_called()

    def test_unsupported_or_missing_cli_does_not_spawn(self):
        with patch.object(guard.sys, "platform", "linux"), patch.object(guard.subprocess, "Popen") as spawn:
            with self.assertRaises(guard.GuardError) as caught:
                guard._command(("environment", "--raw"))
            self.assertEqual(caught.exception.error_code, "surge_guard_unsupported_environment")
            spawn.assert_not_called()
        with patch.object(guard.sys, "platform", "darwin"), \
                patch.object(guard.os.path, "isfile", return_value=False), \
                patch.object(guard.subprocess, "Popen") as spawn:
            with self.assertRaises(guard.GuardError) as caught:
                guard._command(("environment", "--raw"))
            self.assertEqual(caught.exception.error_code, "surge_guard_unavailable")
            spawn.assert_not_called()


class Clock:
    def __init__(self):
        self.mono = 0.0
        self.wall = 1000.0
        self.waits = 0
        self.jump = 0
        self.signal = False

    def event(self):
        clock = self

        class Event:
            stopped = False
            def set(self):
                self.stopped = True
            def is_set(self):
                return self.stopped
            def wait(self, amount):
                clock.waits += 1
                clock.mono += amount
                clock.wall += amount + clock.jump
                if clock.signal:
                    self.set()

        return Event()


class LifecycleTests(unittest.TestCase):
    def run_fake(self, *, ready=None, clock=None, shutdown=None):
        clock = clock or Clock()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(guard, "read_state", return_value=OFF.copy()))
            ending = stack.enter_context(patch.object(guard, "_shutdown", return_value=OFF.copy()))
            if shutdown:
                ending.side_effect = shutdown
            stack.enter_context(patch.object(guard.time, "monotonic", side_effect=lambda: clock.mono))
            stack.enter_context(patch.object(guard.time, "time", side_effect=lambda: clock.wall))
            stack.enter_context(patch.object(guard.threading, "Event", side_effect=clock.event))
            handlers = stack.enter_context(patch.object(guard.signal, "signal", return_value=signal.SIG_DFL))
            try:
                result = guard.run_guard(seconds=1, on_ready=ready)
                return result, clock
            finally:
                ending.assert_called_once_with()
                self.assertEqual(handlers.call_count, 4)

    def test_deadline_has_safe_ready_receipt_and_verified_finish(self):
        ready = []
        result, clock = self.run_fake(ready=ready.append)
        self.assertEqual(ready[0]["event"], "surge_guard_ready")
        self.assertFalse(ready[0]["can_enable_mitm"])
        self.assertIn("expires_at", ready[0])
        self.assertTrue(result["verified_off"])
        self.assertEqual(result["reason"], "deadline")
        self.assertFalse(result["hostnames_restored"])
        self.assertFalse(result["client_updated"])
        self.assertGreater(clock.waits, 0)

    def test_forward_wall_jump_closes_before_monotonic_deadline(self):
        clock = Clock()
        clock.jump = 1000
        result, clock = self.run_fake(clock=clock)
        self.assertTrue(result["verified_off"])
        self.assertLess(clock.mono, 1)

    def test_backward_wall_jump_stops_with_confirmed_cleanup(self):
        clock = Clock()
        clock.jump = -10
        with self.assertRaises(guard.GuardError) as caught:
            self.run_fake(clock=clock)
        self.assertEqual(caught.exception.error_code, "surge_guard_clock_changed")
        self.assertTrue(caught.exception.cleanup_attempted)
        self.assertTrue(caught.exception.cleanup_confirmed)

    def test_ready_output_failure_still_shuts_down_without_exposing_error(self):
        def fail(_value):
            raise RuntimeError(PRIVATE)
        with self.assertRaises(guard.GuardError) as caught:
            self.run_fake(ready=fail)
        self.assertEqual(caught.exception.error_code, "surge_guard_runtime_failed")
        self.assertTrue(caught.exception.cleanup_confirmed)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_signal_closes_and_restores_handlers(self):
        clock = Clock()
        clock.signal = True
        result, _ = self.run_fake(clock=clock)
        self.assertEqual(result["reason"], "signal")
        self.assertTrue(result["verified_off"])

    def test_failed_shutdown_never_reports_success(self):
        with self.assertRaises(guard.GuardError) as caught:
            self.run_fake(shutdown=RuntimeError(PRIVATE))
        self.assertEqual(caught.exception.error_code, "surge_guard_cleanup_unconfirmed")
        self.assertTrue(caught.exception.cleanup_attempted)
        self.assertFalse(caught.exception.cleanup_confirmed)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_initial_on_or_unknown_state_is_not_taken_over(self):
        for key in OFF:
            with self.subTest(key=key), patch.object(guard, "read_state", return_value={**OFF, key: True}), \
                    patch.object(guard, "_shutdown") as shutdown:
                with self.assertRaises(guard.GuardError) as caught:
                    guard.run_guard(seconds=1)
                self.assertEqual(caught.exception.error_code, "surge_guard_initial_switch_on")
                shutdown.assert_not_called()
        with patch.object(guard, "read_state", side_effect=guard.GuardError("surge_guard_unknown_state")), \
                patch.object(guard, "_shutdown") as shutdown:
            with self.assertRaises(guard.GuardError):
                guard.run_guard(seconds=1)
            shutdown.assert_not_called()

    def test_invalid_window_is_rejected_before_read_or_mutation(self):
        for seconds in (False, True, 0, 46, 60, -1, 1.0, "1", None):
            with self.subTest(seconds=seconds), patch.object(guard, "read_state") as state, \
                    patch.object(guard, "_shutdown") as shutdown:
                with self.assertRaises(guard.GuardError) as caught:
                    guard.run_guard(seconds=seconds)
                self.assertEqual(caught.exception.error_code, "surge_guard_invalid_window")
                state.assert_not_called()
                shutdown.assert_not_called()

    def test_non_main_thread_refuses_before_reading_or_registering_signals(self):
        with patch.object(guard.threading, "current_thread", return_value=object()), \
                patch.object(guard, "read_state") as state, \
                patch.object(guard.signal, "signal") as register:
            with self.assertRaises(guard.GuardError) as caught:
                guard.run_guard(seconds=1)
            self.assertEqual(caught.exception.error_code, "surge_guard_main_thread_required")
            state.assert_not_called()
            register.assert_not_called()


class ProcessSignalTests(unittest.TestCase):
    def test_real_standalone_process_closes_on_sigterm(self):
        # Fake local feature state, real independent process and OS signal.
        source = str(Path(__file__).resolve().parents[1] / "src")
        code = f"""
import json, sys
sys.path.insert(0, {source!r})
from sushiwait import surgeguard as g
env = {{'MitMEnabled':False, 'Replica':False, 'ReplicaSessionParameters':{{'mitmOverride':False}}}}
def command(args):
    if args == ('environment','--raw'):
        return json.dumps({{'environment':env}}).encode()
    assert args == ('set','MitMEnabled=0')
    env['MitMEnabled'] = '0'
    return b'{{}}'
g._command = command
def ready(value):
    print(json.dumps(value),flush=True)
    env['MitMEnabled'] = True
print(json.dumps(g.run_guard(seconds=40,on_ready=ready)),flush=True)
"""
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selected:
                selected.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selected.select(3))
            ready = json.loads(child.stdout.readline())
            self.assertEqual(ready["event"], "surge_guard_ready")
            child.send_signal(signal.SIGTERM)
            output, stderr = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, stderr)
            result = json.loads(output)
            self.assertEqual(result["reason"], "signal")
            self.assertEqual(result["final"], OFF)
            self.assertTrue(result["verified_off"])
            self.assertFalse(result["external_network_performed"])
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()


class CliTests(unittest.TestCase):
    def test_cli_fixed_failure_is_redacted(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("sushiwait.cli.run_guard",
            side_effect=guard.GuardError("surge_guard_cleanup_unconfirmed", cleanup_attempted=True)):
            result = main(["surge-guard", "--seconds", "40"])
        value = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertFalse(value["cleanup_confirmed"])
        self.assertTrue(value["cleanup_attempted"])
        self.assertFalse(value["credentials_accessed"])
        self.assertFalse(value["external_network_performed"])

    def test_help_does_not_access_surge_credentials_or_network(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("sushiwait.cli.run_guard") as run:
            with self.assertRaises(SystemExit) as caught:
                main(["surge-guard", "--help"])
        self.assertEqual(caught.exception.code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
