"""Private reconstruction of outcome claims, never certified training data.

Outcome schema 1 lacks an immutable ingestion timestamp. recorded_at is a
caller claim, so selecting its historical revisions does not prove they were
actually available at that time. The distinction is part of every result.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import sqlite3

from .outcomes import (MAX_INPUT_BYTES, OutcomeError, OutcomeStore, _json, _time,
                       candidate_targets, validate_episode)


class CohortError(ValueError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


def reconstruct_claims(records: list[dict], *, as_of: str, data_origin: str,
                       api_profile: str, now: datetime | None = None) -> dict:
    """Validate complete bounded revision chains before selecting old claims."""
    if type(records) is not list or len(records) > 10_000:
        raise CohortError("cohort_invalid_bounds")
    if (type(data_origin) is not str or data_origin not in {"synthetic", "self_reported"}
            or type(api_profile) is not str or api_profile not in {"legacy", "miniapp_gateway"}):
        raise CohortError("cohort_invalid_scope")
    clock = datetime.now(timezone.utc) if now is None else now
    try:
        cutoff = _time(as_of)
        if (not isinstance(clock, datetime) or clock.tzinfo is None
                or clock.utcoffset() is None or cutoff > clock):
            raise OutcomeError("outcome_invalid_time")
        chains = {}
        for value in records:
            item = validate_episode(value, now=clock)
            chain = chains.setdefault(item["episode_id"], {})
            if item["revision"] in chain:
                raise CohortError("cohort_revision_conflict")
            chain[item["revision"]] = item
    except (OutcomeError, TypeError, KeyError, OverflowError):
        raise CohortError("cohort_invalid_claim") from None
    selected, future, scoped_revisions = [], 0, 0
    for chain in chains.values():
        versions = sorted(chain)
        if versions != list(range(1, len(versions) + 1)):
            raise CohortError("cohort_revision_conflict")
        first, previous, latest = chain[1], None, None
        for revision in versions:
            item = chain[revision]
            if any(item[key] != first[key] for key in
                   ("store_id", "api_profile", "data_origin", "queue_type")):
                raise CohortError("cohort_episode_conflict")
            recorded = _time(item["recorded_at"])
            if previous is not None and recorded < previous:
                raise CohortError("cohort_revision_time_order")
            previous = recorded
            if item["data_origin"] == data_origin and item["api_profile"] == api_profile:
                scoped_revisions += 1
                if recorded > cutoff:
                    future += 1
                else:
                    latest = item
        if latest is not None:
            selected.append(latest)
    states = Counter()
    calls = exact_calls = censored = call_to_seat = 0
    for item in selected:
        targets = candidate_targets(item)
        kinds = {event["event_type"]: event for event in item["events"]}
        terminal = item["events"][-1]["event_type"]
        states[terminal if terminal in {"seated", "no_show", "cancelled", "observation_ended"}
               else "ongoing"] += 1
        calls += int(targets["called_wait"] is not None)
        call_to_seat += int(targets["called_to_seated"] is not None)
        censored += int(targets["right_censored_without_call"])
        if "called" in kinds:
            called = kinds["called"]
            exact_calls += int(called["event_time_lower"] == called["event_time_upper"])
    return {
        "cohort_schema_version": 1, "data_origin": data_origin, "api_profile": api_profile,
        "revisions_audited": len(records), "scoped_revisions": scoped_revisions,
        "future_claim_revisions_excluded": future, "selected_episodes": len(selected),
        "terminal_states": dict(sorted(states.items())),
        "unverified_called_wait_candidates": calls,
        "unverified_exact_call_claims": exact_calls,
        "unverified_call_to_seat_candidates": call_to_seat,
        "right_censored_without_call_episodes": censored,
        "claim_revision_chains_checked": True, "truncated": False,
        "availability_basis": "caller_recorded_at_claim",
        "historical_availability_verified": False, "authenticity_verified": False,
        "verified_training_labels": 0, "training_eligible": False,
        "prediction_features_exported": False, "eta_available": False,
        "output_requires_private_handling": True, "network_performed": False,
    }


def cohort_report(store: OutcomeStore, *, as_of: str, data_origin: str,
                  api_profile: str, max_revisions: int = 10_000) -> dict:
    """Read one complete bounded private database view; never silently truncate."""
    if type(max_revisions) is not int or not 1 <= max_revisions <= 10_000:
        raise CohortError("cohort_invalid_bounds")
    clock = datetime.now(timezone.utc)
    reconstruct_claims([], as_of=as_of, data_origin=data_origin,
                       api_profile=api_profile, now=clock)
    if not store.read_only or store.db is None or store.db.in_transaction:
        raise CohortError("cohort_requires_idle_readonly_store")
    begun = False
    try:
        store._guard()
        store.db.execute("BEGIN")
        begun = True
        total = store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        if total > max_revisions:
            raise CohortError("cohort_revision_limit_exceeded")
        rows = store.db.execute(
            "SELECT episode_id,revision,store_id,data_origin,api_profile,"
            "CASE WHEN length(CAST(payload_json AS BLOB))<=? THEN payload_json ELSE NULL END "
            "FROM episodes ORDER BY id LIMIT ?", (MAX_INPUT_BYTES, max_revisions))
        records = []
        for identifier, revision, shop, origin, profile, raw in rows:
            item = _json(raw)
            if any(item.get(key) != value for key, value in
                   (("episode_id", identifier), ("revision", revision), ("store_id", shop),
                    ("data_origin", origin), ("api_profile", profile))):
                raise CohortError("cohort_database_record_mismatch")
            records.append(item)
        if len(records) != total:
            raise CohortError("cohort_database_changed")
        result = reconstruct_claims(records, as_of=as_of, data_origin=data_origin,
                                    api_profile=api_profile, now=clock)
        store._guard()
        return {**result, "consistent_database_view": True}
    except (OutcomeError, sqlite3.Error, TypeError, KeyError, ValueError) as error:
        if isinstance(error, CohortError):
            raise
        raise CohortError("cohort_database_invalid") from None
    finally:
        if begun:
            store.db.rollback()
