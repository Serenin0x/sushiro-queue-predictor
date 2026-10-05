"""Diagnostic dispatch uses synthetic metadata and no receiver or network."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from sushiwait.bridge import BridgeError
from sushiwait.capture import CaptureError
from sushiwait.cli import main


class BridgeCliTests(unittest.TestCase):
    def run_cli(self, *, diagnostic=False, result=None, error=None):
        args = ["context-bridge", "--credentials-file", "/synthetic/private-context",
                "--session-file", "/synthetic/private-session", "--revision", "2",
                "--store-id", "900001", "--seconds", "1"]
        if diagnostic:
            args.append("--diagnostics")
        output = io.StringIO()
        with patch("sushiwait.cli.receive_context", return_value=result,
                   side_effect=error) as receiver, \
                patch("socket.socket", side_effect=AssertionError("no network")), \
                contextlib.redirect_stdout(output):
            code = main(args)
        self.assertEqual(receiver.call_args.kwargs["diagnostics"], diagnostic)
        self.assertNotIn("/synthetic/private", output.getvalue())
        return code, json.loads(output.getvalue())

    def test_timeout_diagnostic_is_preserved_without_request_details(self):
        diagnostics = {"local_connections": 0, "authenticated_deliveries": 0,
                       "validated_observations": 0, "rejected_observations": 0,
                       "last_rejection": None}
        code, row = self.run_cli(diagnostic=True,
            error=BridgeError("bridge_timeout", diagnostics=diagnostics))
        self.assertEqual(code, 1)
        self.assertEqual(row, {"ok": False, "error_code": "bridge_timeout",
                              "external_network_performed": False, "diagnostics": diagnostics})

    def test_default_timeout_output_retains_previous_contract(self):
        code, row = self.run_cli(error=BridgeError("bridge_timeout"))
        self.assertEqual(code, 1)
        self.assertEqual(row, {"ok": False, "error_code": "bridge_timeout",
                              "external_network_performed": False})

    def test_input_failure_does_not_invent_delivery_statistics(self):
        code, row = self.run_cli(diagnostic=True,
                                error=CaptureError("capture_revision_conflict"))
        self.assertEqual(code, 1)
        self.assertEqual(row["error_code"], "capture_revision_conflict")
        self.assertNotIn("diagnostics", row)

    def test_success_retains_durability_and_unverified_server_acceptance(self):
        result = {"committed": True, "durability_confirmed": True,
                  "server_acceptance": "unverified", "external_network_performed": False,
                  "diagnostics": {"local_connections": 1, "authenticated_deliveries": 1,
                                  "validated_observations": 1, "rejected_observations": 0,
                                  "last_rejection": None}}
        code, row = self.run_cli(diagnostic=True, result=result)
        self.assertEqual(code, 0)
        self.assertEqual(row, {"ok": True, **result})


if __name__ == "__main__":
    unittest.main()
