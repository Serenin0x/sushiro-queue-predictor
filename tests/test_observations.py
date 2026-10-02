"""Synthetic protocol and observation tests; no production requests."""

import copy
import json
import unittest

from sushiwait.observations import (
    QUEUE_NAMES, compute_change, normalize_directory, normalize_snapshot,
)


def snapshot(data, *, received="2026-10-02T10:00:00Z", origin="fixture", store_id="12"):
    return normalize_snapshot(
        data, store_id,
        request_started_at=received,
        received_at=received,
        elapsed_ms=25,
        data_origin=origin,
    )


def store(**fields):
    return {"id": 12, "name": "合成门店", **fields}


class SnapshotTests(unittest.TestCase):
    def test_preserves_complete_string_arrays_and_separate_groups(self):
        queues = {
            "reservationQueue": ["R000", "R001"],
            "counterQueue": ["C009"],
            "boothQueue": [],
            "mixedQueue": ["0007", "A099", "A100", "B001", "B001"],
        }
        result = snapshot(store(groupQueues=queues, groupQueuesCount=0, wait=0))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["store_id"], "12")
        groups = result["normalized"]["groupQueues"]["groups"]
        self.assertEqual(set(groups), set(QUEUE_NAMES))
        for key in QUEUE_NAMES:
            self.assertEqual(groups[key], {"presence": "present", "value": queues[key]})
        self.assertEqual(result["normalized"]["raw_wait"], {
            "presence": "present", "value": 0, "unit": "unknown",
        })
        self.assertEqual(result["normalized"]["groupQueuesCount"]["unit"], "unknown")

    def test_missing_is_distinct_from_zero_and_empty_array(self):
        missing = snapshot(store())
        zero = snapshot(store(wait=0, groupQueuesCount=0, groupQueues={"mixedQueue": []}))
        for key in ("raw_wait", "groupQueuesCount"):
            self.assertEqual(missing["normalized"][key]["presence"], "missing")
            self.assertIsNone(missing["normalized"][key]["value"])
            self.assertEqual(zero["normalized"][key]["presence"], "present")
            self.assertEqual(zero["normalized"][key]["value"], 0)
        self.assertEqual(missing["normalized"]["groupQueues"]["presence"], "missing")
        self.assertEqual(zero["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"], [])

    def test_null_and_invalid_never_become_zero_or_empty(self):
        result = snapshot(store(
            wait=None, groupQueuesCount=True, storeStatus={"private": "DO_NOT_SAVE"},
            groupQueues={"reservationQueue": None, "counterQueue": [1], "boothQueue": "001"},
        ))
        fields = result["normalized"]
        self.assertEqual(fields["raw_wait"]["presence"], "null")
        self.assertEqual(fields["groupQueuesCount"]["presence"], "invalid")
        self.assertEqual(fields["storeStatus"]["presence"], "invalid")
        groups = fields["groupQueues"]["groups"]
        self.assertEqual(groups["reservationQueue"]["presence"], "null")
        self.assertEqual(groups["counterQueue"]["presence"], "invalid")
        self.assertEqual(groups["boothQueue"]["presence"], "invalid")
        self.assertEqual(groups["mixedQueue"]["presence"], "missing")
        self.assertNotIn("DO_NOT_SAVE", json.dumps(result))

    def test_null_or_wrong_parent_does_not_imply_no_queue(self):
        for value, presence in ((None, "null"), ([], "invalid")):
            with self.subTest(value=value):
                result = snapshot(store(groupQueues=value))
                group = result["normalized"]["groupQueues"]
                self.assertEqual(group["presence"], presence)
                for field in group["groups"].values():
                    self.assertEqual(field["presence"], presence)
                    self.assertIsNone(field["value"])

    def test_counts_require_integers_without_bool_or_coercion(self):
        for value in (True, False, "0", 1.5, -1, {"secret": "PRIVATE"}):
            with self.subTest(value=value):
                result = snapshot(store(wait=value, groupQueuesCount=value))
                for key in ("raw_wait", "groupQueuesCount"):
                    self.assertEqual(result["normalized"][key]["presence"], "invalid")
                    self.assertIsNone(result["normalized"][key]["value"])

    def test_known_envelope_and_bare_detail_have_same_public_content(self):
        data = store(groupQueues={"mixedQueue": ["A001"]}, wait=4)
        bare = snapshot(data)
        wrapped = snapshot({"code": 200, "data": data, "personal": {"token": "PRIVATE"}})
        self.assertEqual(bare["normalized"], wrapped["normalized"])
        self.assertEqual(bare["content_hash"], wrapped["content_hash"])
        self.assertNotIn("PRIVATE", json.dumps(wrapped))
        with self.assertRaises(ValueError):
            snapshot({"result": data})
        with self.assertRaises(ValueError):
            snapshot({"data": [data]})
        with self.assertRaises(ValueError):
            snapshot({"code": 200, "message": "success but no store data"})

    def test_explicit_errors_never_produce_a_snapshot(self):
        for error in ({"code": 500}, {"success": False}, {"error": "PRIVATE"}, {"errCode": "DENIED"}):
            with self.subTest(error=error), self.assertRaises(ValueError) as raised:
                snapshot({**error, "data": store(wait=0)})
            self.assertNotIn("PRIVATE", str(raised.exception))

    def test_success_must_be_true_when_present_for_live_and_offline_envelopes(self):
        for value in (None, False, 0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot({"success": value, "data": store()})
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_directory({"success": value, "data": [store()]})
        self.assertEqual(snapshot({"success": True, "data": store()})["store_id"], "12")

    def test_code_and_empty_error_rules_match_transport_gate(self):
        for value in (None, "", False, 0, [], {}):
            with self.subTest(error=value):
                result = snapshot({"error": value, "code": "  OK ", "data": store()})
                self.assertEqual(result["store_id"], "12")
        for value in (True, 200.0, [], {}, "201"):
            with self.subTest(code=value), self.assertRaises(ValueError):
                snapshot({"code": value, "data": store()})

    def test_identity_matches_requested_store_including_alias(self):
        self.assertEqual(snapshot({"storeId": "0012", "name": "合成门店"})["store_id"], "12")
        self.assertEqual(snapshot(store(storeId="12"))["normalized"]["storeId"]["value"], "12")
        for data in (store(id=13), store(storeId=13), store(id=True)):
            with self.subTest(data=data), self.assertRaises(ValueError):
                snapshot(data)

    def test_only_field_names_of_unknown_and_personal_fields_survive(self):
        secret_key = "a" * 90
        data = store(
            phone="PRIVATE_PHONE", openid="PRIVATE_WECHAT", token="PRIVATE_TOKEN",
            netTicket={"id": "PRIVATE_TICKET", "nestedSecret": "PRIVATE_NESTED"},
            groupQueues={"mixedQueue": ["A001"], "personalQueue": {"token": "PRIVATE_QUEUE"}},
            **{secret_key: "PRIVATE_KEY_VALUE", "Bearer_PRIVATE": "PRIVATE_HEADER"},
        )
        result = snapshot(data)
        encoded = json.dumps(result)
        for value in ("PRIVATE_PHONE", "PRIVATE_WECHAT", "PRIVATE_TOKEN", "PRIVATE_TICKET",
                      "PRIVATE_NESTED", "PRIVATE_QUEUE", "PRIVATE_KEY_VALUE", "PRIVATE_HEADER",
                      secret_key, "Bearer_PRIVATE", "nestedSecret"):
            self.assertNotIn(value, encoded)
        self.assertIn("phone", result["field_inventory"]["data"])
        self.assertIn("netTicket", result["field_inventory"]["data"])
        self.assertIn("personalQueue", result["field_inventory"]["groupQueues"])

    def test_source_freshness_is_unknown_even_with_unverified_timestamps(self):
        result = snapshot(store(updatedAt="PRIVATE_SOURCE_TIME", source_updated_at="PRIVATE_TIME", Date="PRIVATE_DATE"))
        self.assertIsNone(result["source_updated_at"])
        self.assertEqual(result["upstream_freshness"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(result["timing"]["received_at"], "2026-10-02T10:00:00.000Z")

    def test_local_timing_is_normalized_to_fixed_utc_milliseconds(self):
        result = normalize_snapshot(
            store(), "12", request_started_at="2026-10-02T18:00:00.123456+08:00",
            received_at="2026-10-02T10:00:01.987654Z", elapsed_ms=1864,
            data_origin="synthetic",
        )
        self.assertEqual(result["timing"]["request_started_at"], "2026-10-02T10:00:00.123Z")
        self.assertEqual(result["timing"]["received_at"], "2026-10-02T10:00:01.987Z")
        before = snapshot(store(), received="2026-10-02T18:00:00+08:00")
        after = snapshot(store(), received="2026-10-02T11:00:00Z")
        self.assertLess(before["timing"]["received_at"], after["timing"]["received_at"])
        self.assertEqual(compute_change(before, after)["observation_interval_ms"], 3600000)
        reversed_time = normalize_snapshot(
            store(), "12", request_started_at="2026-10-02T18:00:00.123456+08:00",
            received_at="2026-10-02T10:00:00.123400Z", elapsed_ms=0,
            data_origin="synthetic",
        )
        self.assertIn({"field": "timing", "code": "received_before_request_started"}, reversed_time["issues"])

    def test_normalization_does_not_mutate_or_alias_input_arrays(self):
        data = store(groupQueues={"mixedQueue": ["A001"]})
        original = copy.deepcopy(data)
        result = snapshot(data)
        result["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"].append("A002")
        self.assertEqual(data, original)

    def test_origin_and_local_timing_metadata_are_validated(self):
        for origin in ("fixture", "synthetic", "live"):
            self.assertEqual(snapshot(store(), origin=origin)["data_origin"], origin)
        with self.assertRaises(ValueError):
            snapshot(store(), origin="unknown")
        with self.assertRaises(ValueError):
            snapshot(store(), received="2026-10-02T10:00:00")


class DirectoryTests(unittest.TestCase):
    def test_bare_or_data_array_directory_retains_only_public_identity_fields(self):
        data = [store(address=None, phone="PRIVATE_PHONE", netTicket={"token": "PRIVATE_TOKEN"})]
        result = normalize_directory({"data": data})
        self.assertEqual(result, normalize_directory(data))
        self.assertEqual(result[0]["store_id"], "12")
        self.assertEqual(result[0]["normalized"]["address"]["presence"], "null")
        self.assertEqual(result[0]["normalized"]["area"]["presence"], "missing")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(normalize_directory({"data": []}), [])

    def test_unrecognized_shapes_and_bad_entries_are_rejected(self):
        for payload in ({"stores": [store()]}, {"data": {"stores": [store()]}},
                        {"data": [None]}, {"data": [store(id=0)]},
                        {"data": [store(name=" ")]}, {"code": 403, "data": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_directory(payload)


class ChangeTests(unittest.TestCase):
    def test_prefixes_resets_and_large_number_gaps_are_only_set_changes(self):
        before = snapshot(store(groupQueues={"mixedQueue": ["A998", "A999", "B001"]}))
        after = snapshot(store(groupQueues={"mixedQueue": ["A001", "B001", "Z90000"]}), received="2026-10-02T10:02:00Z")
        result = compute_change(before, after)
        mixed = result["queues"]["mixedQueue"]
        self.assertEqual(mixed["added"], ["A001", "Z90000"])
        self.assertEqual(mixed["removed"], ["A998", "A999"])
        self.assertTrue(mixed["display_set_changed"])
        self.assertEqual(result["observation_interval_ms"], 120000)
        for forbidden in ("speed", "no_show_rate", "called_count", "people", "global_cursor", "progress"):
            self.assertNotIn(forbidden, result)
            self.assertNotIn(forbidden, mixed)
        self.assertTrue(result["inference_limits"])

    def test_order_changes_are_distinct_from_display_set_changes(self):
        before = snapshot(store(groupQueues={"mixedQueue": ["002", "001"]}))
        after = snapshot(store(groupQueues={"mixedQueue": ["001", "002"]}), received="2026-10-02T10:00:30Z")
        mixed = compute_change(before, after)["queues"]["mixedQueue"]
        self.assertFalse(mixed["display_set_changed"])
        self.assertTrue(mixed["display_values_changed"])
        self.assertEqual(mixed["added"], [])
        self.assertEqual(mixed["removed"], [])

    def test_missing_array_cannot_be_a_baseline_for_additions(self):
        before = snapshot(store())
        after = snapshot(store(groupQueues={"mixedQueue": ["001"]}))
        mixed = compute_change(before, after)["queues"]["mixedQueue"]
        self.assertFalse(mixed["comparable"])
        self.assertIsNone(mixed["added"])
        self.assertIsNone(mixed["removed"])
        self.assertEqual(mixed["previous_presence"], "missing")

    def test_initial_or_different_store_or_origin_is_not_compared(self):
        current = snapshot(store())
        self.assertEqual(compute_change(None, current)["comparison"], "initial")
        others = [snapshot(store(), origin="live"), snapshot(store(id=13), store_id="13")]
        for previous in others:
            with self.subTest(previous=previous):
                result = compute_change(previous, current)
                self.assertEqual(result["comparison"], "incomparable")
                self.assertIsNone(result["observation_interval_ms"])
                self.assertIsNone(result["queues"]["mixedQueue"]["added"])

    def test_same_content_is_unchanged_without_freshness_claim(self):
        before = snapshot(store(wait=0, groupQueues={"mixedQueue": []}))
        after = snapshot(store(wait=0, groupQueues={"mixedQueue": []}), received="2026-10-02T10:01:00Z")
        result = compute_change(before, after)
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(result["content_status"], "unchanged")
        self.assertEqual(result["upstream_freshness"], "unknown")
        self.assertEqual(result["field_changes"], {})

    def test_field_changes_preserve_missing_versus_reported_zero(self):
        result = compute_change(snapshot(store()), snapshot(store(wait=0, groupQueuesCount=0)))
        change = result["field_changes"]["raw_wait"]
        self.assertEqual(change["previous"]["presence"], "missing")
        self.assertIsNone(change["previous"]["value"])
        self.assertEqual(change["current"]["value"], 0)

    def test_reversed_observation_time_has_no_negative_interval(self):
        before = snapshot(store(), received="2026-10-02T10:02:00Z")
        after = snapshot(store(), received="2026-10-02T10:01:00Z")
        result = compute_change(before, after)
        self.assertIsNone(result["observation_interval_ms"])
        self.assertIn({"field": "timing", "code": "observation_order_reversed"}, result["issues"])


if __name__ == "__main__":
    unittest.main()
