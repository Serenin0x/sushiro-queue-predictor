"""Expired trial restarts stay closed; this never launches a supplier."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('official_cloud_lifetime',
    Path(__file__).resolve().parents[1] / 'deploy' / 'official_cloud_lifetime.py')
lifetime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifetime)


class OfficialCloudLifetimeTests(unittest.TestCase):
    def test_reboot_boundary_is_absolute_and_expiry_cannot_restart_supplier(self):
        for at, expected in [
            (lifetime.START - timedelta(microseconds=1), False),
            (lifetime.START, True),
            (lifetime.END - timedelta(microseconds=1), True),
            (lifetime.END, False),
            (lifetime.END + timedelta(days=1), False),
        ]:
            with self.subTest(at=at):
                self.assertIs(lifetime.allowed_start(at), expected)
                self.assertIs(lifetime.allowed_start(at.astimezone(timezone.utc)), expected)

    def test_missing_clock_timezone_is_rejected_instead_of_assumed_local(self):
        for at in (None, '2026-10-12', datetime(2026, 10, 12)):
            with self.subTest(at=at), self.assertRaises(ValueError):
                lifetime.allowed_start(at)
