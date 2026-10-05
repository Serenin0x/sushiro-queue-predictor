from copy import deepcopy
from datetime import datetime, timezone
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cohort import CohortError, cohort_report, reconstruct_claims
from sushiwait.cli import main
from sushiwait.outcomes import OutcomeStore


FIXTURE = Path(__file__).resolve().parents[1] / "examples/fixtures/outcome-01.synthetic.json"
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def record():
    return json.loads(FIXTURE.read_text())


def revision(first, number=2, recorded="2020-10-01T12:00:00+08:00"):
    value = deepcopy(first)
    value.update(revision=number, supersedes_revision=number-1, recorded_at=recorded)
    return value


class CohortClaimTests(unittest.TestCase):
    def report(self, rows, as_of="2020-10-01T11:30:00+08:00", **changes):
        options = {"as_of": as_of, "data_origin": "synthetic", "api_profile": "miniapp_gateway", "now": NOW}
        options.update(changes)
        return reconstruct_claims(rows, **options)

    def test_after_cutoff_correction_does_not_replace_older_result(self):
        first = record(); corrected = revision(first)
        corrected["events"] = [corrected["events"][0], corrected["events"][-1]]
        corrected["events"][-1]["event_type"] = "cancelled"
        early = self.report([corrected, first])
        late = self.report([first, corrected], "2020-10-01T12:00:00+08:00")
        self.assertEqual(early["unverified_called_wait_candidates"], 1)
        self.assertEqual(early["future_claim_revisions_excluded"], 1)
        self.assertEqual(late["unverified_called_wait_candidates"], 0)
        self.assertEqual(late["terminal_states"], {"cancelled": 1})

    def test_before_first_claim_is_empty(self):
        result = self.report([record()], "2020-10-01T10:59:59+08:00")
        self.assertEqual(result["selected_episodes"], 0)
        self.assertEqual(result["terminal_states"], {})

    def test_equal_recorded_timestamp_uses_higher_revision(self):
        first = record(); later = revision(first, recorded=first["recorded_at"])
        later["events"] = later["events"][:1]
        self.assertEqual(self.report([later, first])["terminal_states"], {"ongoing": 1})

    def test_duplicate_and_missing_revisions_rejected(self):
        first = record()
        for rows in [[first, first], [revision(first)], [first, revision(first, 3)]]:
            with self.subTest(rows=len(rows)), self.assertRaises(CohortError):
                self.report(rows)

    def test_immutable_episode_scope_conflict_rejected(self):
        first = record()
        for key, value in [("store_id", "900002"), ("api_profile", "legacy"),
                           ("queue_type", "reservation"), ("data_origin", "self_reported")]:
            later = revision(first); later[key] = value
            if key == "data_origin":
                for event in later["events"]:
                    event["evidence_kind"] = "self_observation"
            with self.subTest(key=key), self.assertRaises(CohortError):
                self.report([first, later])

    def test_revision_recorded_time_cannot_go_backwards(self):
        first = record()
        with self.assertRaises(CohortError) as error:
            self.report([first, revision(first, recorded="2020-10-01T10:59:00+08:00")])
        self.assertEqual(error.exception.error_code, "cohort_revision_time_order")

    def test_origins_and_profiles_are_selected_separately(self):
        synthetic = record(); human = record()
        human["episode_id"] = "00000000-0000-4000-8000-000000000001"
        human["data_origin"] = "self_reported"
        for event in human["events"]:
            event["evidence_kind"] = "self_observation"
        old = record(); old["episode_id"] = "00000000-0000-4000-8000-000000000002"
        old["api_profile"] = "legacy"
        result = self.report([synthetic, human, old])
        self.assertEqual(result["selected_episodes"], 1)
        self.assertEqual(result["scoped_revisions"], 1)
        self.assertEqual(result["revisions_audited"], 3)

    def test_censoring_cancel_and_no_show_do_not_create_calls(self):
        for terminal in ["observation_ended", "cancelled", "no_show"]:
            value = record(); value["events"] = [value["events"][0], value["events"][-1]]
            value["events"][-1]["event_type"] = terminal
            result = self.report([value])
            self.assertEqual(result["unverified_called_wait_candidates"], 0)
            self.assertEqual(result["right_censored_without_call_episodes"], int(terminal == "observation_ended"))

    def test_exact_call_and_ongoing_claims(self):
        value = record(); value["events"][2]["event_time_upper"] = value["events"][2]["event_time_lower"]
        self.assertEqual(self.report([value])["unverified_exact_call_claims"], 1)
        self.assertEqual(self.report([value])["unverified_call_to_seat_candidates"], 1)
        value["events"] = value["events"][:1]
        self.assertEqual(self.report([value])["terminal_states"], {"ongoing": 1})

    def test_no_identity_or_itinerary_and_no_certification(self):
        value = record(); before = deepcopy(value); result = self.report([value])
        self.assertEqual(value, before)
        for private in [value["episode_id"], "900001", "2020-10-01", "event_id"]:
            self.assertNotIn(private, json.dumps(result))
        self.assertFalse(result["historical_availability_verified"])
        self.assertFalse(result["authenticity_verified"])
        self.assertEqual(result["verified_training_labels"], 0)
        self.assertTrue(result["output_requires_private_handling"])

    def test_explicit_time_scope_and_bounds(self):
        for changes in [{"as_of": "2020-10-01T11:30:00"}, {"as_of": "2099-01-01T00:00:00Z"},
                        {"data_origin": []}, {"api_profile": True}, {"now": datetime(2026, 10, 4)}]:
            with self.subTest(changes=changes), self.assertRaises(CohortError):
                self.report([], **changes)
        for rows in [(), [None] * 10001]:
            with self.assertRaises(CohortError):
                self.report(rows)

    def test_invalid_claim_is_masked(self):
        value = record(); value["recorded_at"] = "PRIVATE_NEVER_PRINT"
        with self.assertRaises(CohortError) as error:
            self.report([value])
        self.assertNotIn("PRIVATE_NEVER_PRINT", str(error.exception))


class CohortDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.path = self.directory / "outcomes.sqlite3"
        with OutcomeStore(self.path) as store:
            store.append(record(), now=NOW)

    def report(self, store, **changes):
        options = {"as_of": "2020-10-01T11:30:00+08:00", "data_origin": "synthetic", "api_profile": "miniapp_gateway"}
        options.update(changes)
        return cohort_report(store, **options)

    def test_readonly_complete_view_does_not_change_database(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with OutcomeStore(self.path, read_only=True) as store:
            result = self.report(store)
            self.assertFalse(store.db.in_transaction)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertTrue(result["consistent_database_view"])
        self.assertEqual(result["selected_episodes"], 1)

    def test_writable_or_open_transaction_is_refused(self):
        with OutcomeStore(self.path) as store:
            with self.assertRaises(CohortError): self.report(store)
        with OutcomeStore(self.path, read_only=True) as store:
            store.db.execute("BEGIN")
            with self.assertRaises(CohortError): self.report(store)
            self.assertTrue(store.db.in_transaction)

    def test_revision_limit_refuses_instead_of_truncating(self):
        with OutcomeStore(self.path) as store:
            store.append(revision(record()), now=NOW)
        with OutcomeStore(self.path, read_only=True) as store:
            with self.assertRaises(CohortError) as error: self.report(store, max_revisions=1)
            self.assertEqual(error.exception.error_code, "cohort_revision_limit_exceeded")
            self.assertFalse(store.db.in_transaction)

    def test_payload_column_mismatch_is_not_skipped(self):
        with OutcomeStore(self.path) as store:
            store.db.execute("UPDATE episodes SET store_id='900002'"); store.db.commit()
        with OutcomeStore(self.path, read_only=True) as store:
            with self.assertRaises(CohortError) as error: self.report(store)
            self.assertEqual(error.exception.error_code, "cohort_database_record_mismatch")
            self.assertFalse(store.db.in_transaction)

    def test_oversized_payload_is_bounded_and_masked(self):
        with OutcomeStore(self.path) as store:
            store.db.execute("UPDATE episodes SET payload_json=?", ("PRIVATE_NEVER_PRINT" * 1000,)); store.db.commit()
        with OutcomeStore(self.path, read_only=True) as store:
            with self.assertRaises(CohortError) as error: self.report(store)
            self.assertEqual(error.exception.error_code, "cohort_database_invalid")
            self.assertNotIn("PRIVATE_NEVER_PRINT", str(error.exception))

    def test_file_alias_and_public_permissions_refused(self):
        alias = self.directory / "alias.sqlite3"; alias.symlink_to(self.path)
        from sushiwait.outcomes import OutcomeError
        with self.assertRaises(OutcomeError): OutcomeStore(alias, read_only=True)
        self.path.chmod(0o644)
        with self.assertRaises(OutcomeError): OutcomeStore(self.path, read_only=True)

    def cli(self, **changes):
        options = {"db": str(self.path), "as-of": "2020-10-01T11:30:00+08:00",
                   "data-origin": "synthetic", "api-profile": "miniapp_gateway"}
        options.update(changes)
        args = ["outcome-cohort"]
        for key, value in options.items():
            args.extend(["--" + key, str(value)])
        output = io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("no_network")), \
                patch("socket.create_connection", side_effect=AssertionError("no_network")), \
                patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("no_auth")), \
                patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("no_public_db")), \
                patch("sushiwait.cli.client_for", side_effect=AssertionError("no_client")), \
                patch("sushiwait.surgeguard._command", side_effect=AssertionError("no_native")), \
                patch("subprocess.Popen", side_effect=AssertionError("no_child")), \
                contextlib.redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_cli_is_readonly_and_no_external_side_effects(self):
        before = self.path.read_bytes()
        code, result = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(result["selected_episodes"], 1)
        self.assertEqual(result["verified_training_labels"], 0)
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn("2020-10-01", json.dumps(result))

    def test_invalid_cli_time_or_limit_does_not_open_database(self):
        with patch("sushiwait.cli.OutcomeStore", side_effect=AssertionError("no_open")):
            for changes in [{"as-of": "PRIVATE_NEVER_PRINT"}, {"max-revisions": 0}]:
                code, result = self.cli(**changes)
                self.assertEqual(code, 1)
                self.assertNotIn("PRIVATE_NEVER_PRINT", str(result))

    def test_missing_cli_database_is_not_created(self):
        path = self.directory / "missing.sqlite3"
        code, result = self.cli(db=path)
        self.assertEqual(code, 1)
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), json.dumps(result))

    def test_cli_limit_returns_no_partial_cohort(self):
        with OutcomeStore(self.path) as store:
            store.append(revision(record()), now=NOW)
        code, result = self.cli(**{"max-revisions": 1})
        self.assertEqual(code, 1)
        self.assertEqual(result["error_code"], "cohort_revision_limit_exceeded")
        self.assertNotIn("selected_episodes", result)

    def test_cli_scope_empty_is_known_and_unverified(self):
        code, result = self.cli(**{"data-origin": "self_reported"})
        self.assertEqual(code, 0)
        self.assertEqual(result["selected_episodes"], 0)
        self.assertFalse(result["historical_availability_verified"])

    def test_bad_future_revision_does_not_silently_certify_history(self):
        with OutcomeStore(self.path) as store:
            store.append(revision(record()), now=NOW)
            store.db.execute("UPDATE episodes SET revision=3 WHERE revision=2"); store.db.commit()
        code, result = self.cli()
        self.assertEqual(code, 1)
        self.assertNotIn("selected_episodes", result)


if __name__ == "__main__":
    unittest.main()
