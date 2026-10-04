import contextlib
from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import hashlib
from importlib.resources import files
import io
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from sushiwait.calendar import CalendarError, _load_calendar, _parse_calendar, date_features
from sushiwait.cli import main

AS_OF = "2026-10-04T12:00:00Z"


def features(day, as_of=AS_OF):
    return date_features(day + "T12:00:00+08:00", as_of=as_of)


class CalendarTests(unittest.TestCase):
    def test_all_announced_holiday_dates_and_indices(self):
        expected = (("new_year", "2026-01-01", 3), ("spring_festival", "2026-02-15", 9),
            ("qingming", "2026-04-04", 3), ("labour_day", "2026-05-01", 5),
            ("dragon_boat", "2026-06-19", 3), ("mid_autumn", "2026-09-25", 3),
            ("national_day", "2026-10-01", 7))
        for name, start, length in expected:
            for index in range(length):
                day = (date.fromisoformat(start) + timedelta(days=index)).isoformat()
                with self.subTest(day=day):
                    result = features(day)
                    self.assertEqual(result["date_type"], "holiday")
                    self.assertEqual(result["holiday"], {"name": name, "day_index": index + 1,
                                                         "length_days": length})
                    self.assertIsNone(result["makeup_for"])

    def test_all_six_weekend_workdays(self):
        for day, name in (("2026-01-04", "new_year"), ("2026-02-14", "spring_festival"),
                ("2026-02-28", "spring_festival"), ("2026-05-09", "labour_day"),
                ("2026-09-20", "national_day"), ("2026-10-10", "national_day")):
            with self.subTest(day=day):
                result = features(day)
                self.assertEqual(result["date_type"], "makeup_workday")
                self.assertTrue(result["is_weekend_by_weekday"])
                self.assertEqual(result["makeup_for"], name)
                self.assertIsNone(result["holiday"])

    def test_whole_year_partition_and_ordinary_weekends(self):
        counts = Counter(features((date(2026, 1, 1) + timedelta(days=i)).isoformat())["date_type"]
                         for i in range(365))
        self.assertEqual(counts, {"ordinary_workday": 242, "ordinary_weekend": 84,
                                  "holiday": 33, "makeup_workday": 6})
        self.assertEqual(features("2026-10-11")["date_type"], "ordinary_weekend")
        self.assertEqual(features("2026-10-12")["date_type"], "ordinary_workday")

    def test_timezone_conversion_crosses_midnight_and_year(self):
        result = date_features("2026-09-30T16:00:00Z", as_of=AS_OF)
        self.assertEqual((result["local_date"], result["minute_of_day"], result["date_type"]),
                         ("2026-10-01", 0, "holiday"))
        result = date_features("2026-12-31T16:00:00Z", as_of=AS_OF)
        self.assertEqual((result["local_date"], result["date_type"], result["calendar_status"]),
                         ("2027-01-01", "unknown", "outside_coverage"))

    def test_equivalent_instants_give_identical_features(self):
        self.assertEqual(date_features("2026-10-04T03:00:00Z", as_of=AS_OF),
                         date_features("2026-10-04T11:00:00+08:00", as_of=AS_OF))

    def test_no_holiday_lookahead_before_publication_availability(self):
        before = features("2026-10-04", "2025-11-04T15:59:59.999999Z")
        self.assertEqual((before["date_type"], before["calendar_status"]),
                         ("unknown", "not_yet_published"))
        self.assertIsNone(before["holiday"])
        self.assertEqual(before["iso_weekday"], 7)
        after = features("2026-10-04", "2025-11-04T16:00:00Z")
        self.assertEqual(after["date_type"], "holiday")

    def test_unknown_years_do_not_inherit_weekday_or_holiday_classification(self):
        for day in ("2025-10-01", "2027-10-01", "2028-02-29"):
            result = features(day)
            self.assertEqual(result["date_type"], "unknown")
            self.assertEqual(result["calendar_status"], "outside_coverage")
            self.assertIsNone(result["holiday"])
            self.assertIsNone(result["makeup_for"])

    def test_calendar_season_month_quarter_and_time_remain_distinct(self):
        for month, season in ((1, "winter"), (3, "spring"), (6, "summer"), (9, "autumn"), (12, "winter")):
            result = date_features(f"2026-{month:02d}-12T18:45:00+08:00", as_of=AS_OF)
            self.assertEqual((result["calendar_season"], result["month"], result["quarter"]),
                             (season, month, (month - 1) // 3 + 1))
            self.assertEqual(result["minute_of_day"], 1125)
            self.assertEqual(result["store_open_status"], "unknown")
            self.assertFalse(result["eta_available"])

    def test_invalid_instants_do_not_echo_input_or_infer_timezone(self):
        for value in ("private-marker", "2026-10-04", "2026-10-04T12:00:00",
                "2026-02-29T12:00:00Z", "2026-10-04T12:00:00+24:00",
                "2026-10-04T12:00:00Z ", "2026-10-04T12:00:60Z",
                "2026-10-04T12:00:00.1234567Z", "２０２６-10-04T12:00:00Z", True):
            with self.subTest(value=value), self.assertRaises(CalendarError) as raised:
                date_features(value, as_of=AS_OF)
            self.assertEqual(str(raised.exception), "invalid_calendar_time")
        with self.assertRaises(CalendarError):
            date_features("2026-10-04T12:00:00Z", as_of="private-marker")

    def test_metadata_hash_and_review_are_not_claimed_as_historical_system_use(self):
        raw = files("sushiwait").joinpath("data", "cn-mainland-2026.json").read_bytes()
        result = features("2026-10-04")
        provenance = result["calendar_provenance"]
        self.assertEqual(provenance["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(provenance["reviewed_on"], "2026-10-04")
        self.assertEqual(provenance["published_on"], "2025-11-04")
        self.assertEqual(provenance["knowledge_basis"], "official_publication_reconstruction")
        self.assertEqual(provenance["coverage_year"], 2026)
        self.assertFalse(result["network_performed"])

    def test_bad_calendar_conflicts_and_identity_fail_instead_of_guessing(self):
        baseline = json.loads(files("sushiwait").joinpath("data", "cn-mainland-2026.json").read_bytes())
        for modify in (
                lambda d: d["holidays"][1].update(start="2026-01-02", end="2026-01-03"),
                lambda d: d["holidays"][0].update(end="2026-01-20"),
                lambda d: d["makeup_workdays"][0].update(date="2026-01-03"),
                lambda d: d["makeup_workdays"][0].update(date="2026-01-05"),
                lambda d: d["makeup_workdays"][1].update(date="2026-01-04"),
                lambda d: d.update(year=True), lambda d: d.update(region="HK"),
                lambda d: d.update(source_url="https://unreviewed.invalid"),
                lambda d: d.update(available_after="2025-11-04T00:00:00+08:00")):
            data = json.loads(json.dumps(baseline)); modify(data)
            with self.assertRaises(CalendarError) as raised:
                _parse_calendar(json.dumps(data).encode())
            self.assertEqual(str(raised.exception), "calendar_data_invalid")

    def test_bad_resource_size_duplicate_keys_and_numbers(self):
        for raw in (b"", b"x" * 32769, b'{"year":2026,"year":2026}', b'{"year":NaN}',
                    b'[]', b'\xff', b'{"private-marker":1}'):
            with self.assertRaises(CalendarError) as raised:
                _parse_calendar(raw)
            self.assertEqual(str(raised.exception), "calendar_data_invalid")

    def test_cached_calendar_and_outputs_cannot_modify_later_classification(self):
        with self.assertRaises(FrozenInstanceError):
            _load_calendar().year = 2027
        original = features("2026-10-04")
        original["holiday"]["name"] = "wrong"
        original["calendar_provenance"]["revision"] = "wrong"
        self.assertEqual(features("2026-10-04")["holiday"]["name"], "national_day")

    def test_unavailable_timezone_or_resource_is_explicit(self):
        with patch("sushiwait.calendar.ZoneInfo", side_effect=ZoneInfoNotFoundError("private-marker")):
            with self.assertRaises(CalendarError) as raised:
                features("2026-10-04")
            self.assertEqual(str(raised.exception), "calendar_timezone_unavailable")
        _load_calendar.cache_clear()
        try:
            with patch("sushiwait.calendar.files", side_effect=OSError("private-marker")):
                with self.assertRaises(CalendarError) as raised:
                    features("2026-10-04")
                self.assertEqual(str(raised.exception), "calendar_data_unavailable")
        finally:
            _load_calendar.cache_clear()

    def test_cli_is_offline_and_does_not_construct_client_credentials_or_database(self):
        with patch("socket.socket", side_effect=AssertionError("unexpected_network")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected_network")), \
                patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("unexpected_client")), \
                patch("sushiwait.cli.CredentialSource", side_effect=AssertionError("unexpected_credentials")), \
                patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("unexpected_database")), \
                patch("sushiwait.cli.OutcomeStore", side_effect=AssertionError("unexpected_outcomes")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["date-features", "--at", "2026-10-10T12:00:00+08:00", "--as-of", AS_OF])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["date_type"], "makeup_workday")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["date-features", "--at", "private-marker", "--as-of", AS_OF])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue()), {"ok": False,
                "error_code": "invalid_calendar_time", "network_performed": False})
