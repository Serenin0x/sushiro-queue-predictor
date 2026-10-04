"""Check a wheel install outside the checkout, using only a synthetic fixture."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import sushiwait
from sushiwait.cli import main


def check() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--outcome-fixture", type=Path, required=True)
    args = parser.parse_args()
    expected = args.version_file.read_text(encoding="utf-8").strip()
    if sushiwait.__version__ != expected:
        raise SystemExit("installed_version_mismatch")
    if Path(sushiwait.__file__).resolve().is_relative_to(args.source_root.resolve()):
        raise SystemExit("package_imported_from_checkout")
    help_result = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(command not in help_result.stdout for command in
            ("capture-import", "context-bridge", "outcome-import", "outcome-report")):
        raise SystemExit("installed_cli_missing_commands")
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory).resolve() / "synthetic.sqlite3")
        # Fail immediately if the replay/report smoke path tries any socket I/O.
        with patch("socket.socket", side_effect=AssertionError("unexpected_network")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected_network")):
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["replay", "--fixture", str(args.fixture), "--db", database]) != 0:
                    raise SystemExit("installed_replay_failed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                if main(["report", "--db", database]) != 0:
                    raise SystemExit("installed_report_failed")
            outcome_database = str(Path(directory).resolve() / "outcomes.sqlite3")
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["outcome-check", "--synthetic-fixture", str(args.outcome_fixture)]) != 0:
                    raise SystemExit("installed_outcome_check_failed")
                if main(["outcome-import", "--synthetic-fixture", str(args.outcome_fixture),
                         "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_import_failed")
            outcome_output = io.StringIO()
            with contextlib.redirect_stdout(outcome_output):
                if main(["outcome-report", "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_report_failed")
        report = json.loads(output.getvalue())
        if not report["groups"] or any(g["data_origin"] != "synthetic" for g in report["groups"]):
            raise SystemExit("installed_report_origin_mismatch")
        outcome_report = json.loads(outcome_output.getvalue())
        if (outcome_report["origins"] != {"synthetic": 1} or outcome_report["verified_training_labels"] != 0
                or outcome_report["eta_available"]):
            raise SystemExit("installed_outcome_report_semantics_mismatch")
    print(json.dumps({"installed_version": expected, "import_outside_checkout": True,
        "cli_help_ok": True, "synthetic_replay_ok": True, "readonly_report_ok": True,
        "replay_report_socket_calls": 0, "synthetic_outcomes_ok": True,
        "outcomes_socket_calls": 0, "verified_training_labels": 0}))


if __name__ == "__main__":
    check()
