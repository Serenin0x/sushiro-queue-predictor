from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('collection_alerts',
    Path(__file__).resolve().parents[1] / 'deploy' / 'collection_alerts.py')
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)
NOW = datetime(2026, 10, 15, 7, 30, tzinfo=timezone.utc)
CONFIG = {'schema_version': 1, 'provider': 'serverchan_turbo',
          'sendkey': 'SCTsyntheticTestOnly123456789', 'statistics_url': alerts.STATISTICS_URL}


class CollectionAlertTests(unittest.TestCase):
    def status(self, **changes):
        return {'at': NOW.isoformat(), 'state': 'running', 'failed_slots': 0,
                'last_progress_at': NOW.isoformat(), **changes}

    def test_fresh_heartbeat_with_missing_progress_is_detected(self):
        status = self.status(last_progress_at=(NOW - timedelta(minutes=13)).isoformat())
        self.assertEqual(alerts.condition(status, self.status(), NOW), 'collection_progress_stale')

    def test_source_and_sync_health_are_both_required(self):
        self.assertEqual(alerts.condition(self.status(), self.status(), NOW), 'healthy')
        self.assertEqual(alerts.condition(self.status(), self.status(state='needs_attention'), NOW),
                         'public_sync_stopped')
        self.assertEqual(alerts.condition(self.status(at=(NOW-timedelta(minutes=3)).isoformat()),
                                         self.status(), NOW), 'collection_heartbeat_stale')

    def test_closed_business_window_is_not_an_incident(self):
        self.assertEqual(alerts.condition(self.status(state='closed'), {}, NOW), 'closed')

    def test_restart_deduplicates_and_six_hour_reminder_is_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'state.json'
            sent = []
            sender = lambda *args: sent.append(args) is None
            first = alerts.Monitor(path, CONFIG, sender)
            self.assertEqual(first.step('collection_stopped', NOW), 'accepted')
            restarted = alerts.Monitor(path, CONFIG, sender)
            self.assertEqual(restarted.step('collection_stopped', NOW+timedelta(minutes=1)), 'suppressed')
            self.assertEqual(restarted.step('collection_stopped', NOW+timedelta(hours=6)), 'accepted')
            self.assertEqual(len(sent), 2)
            self.assertNotIn(CONFIG['sendkey'], sent[0][2])

    def test_unknown_delivery_never_creates_a_minutely_send_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'state.json'
            calls = []
            def uncertain(*_):
                calls.append(1)
                raise TimeoutError('synthetic')
            monitor = alerts.Monitor(path, CONFIG, uncertain)
            self.assertEqual(monitor.step('collection_stopped', NOW), 'unknown')
            monitor = alerts.Monitor(path, CONFIG, uncertain)
            self.assertEqual(monitor.step('collection_stopped', NOW+timedelta(minutes=1)), 'suppressed')
            self.assertEqual(len(calls), 1)

    def test_recovery_only_once_and_new_incident_can_notify(self):
        with tempfile.TemporaryDirectory() as temp:
            sent = []
            monitor = alerts.Monitor(Path(temp).resolve()/'state.json', CONFIG,
                                     lambda *args: sent.append(args) is None)
            monitor.step('collection_stopped', NOW)
            monitor.step('healthy', NOW+timedelta(minutes=1))
            monitor.step('healthy', NOW+timedelta(minutes=2))
            monitor.step('collection_stopped', NOW+timedelta(minutes=3))
            self.assertEqual(len(sent), 3)
            self.assertIn('已恢复', sent[1][1])

    def test_daily_limit_survives_restart_and_uses_beijing_date(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve()/'state.json'
            sent = []
            sender = lambda *args: sent.append(args) is None
            monitor = alerts.Monitor(path, CONFIG, sender)
            for index in range(8):
                monitor.step('collection_stopped' if index % 2 == 0 else 'healthy',
                             NOW+timedelta(minutes=index))
            self.assertEqual(len(sent), 5)
            monitor = alerts.Monitor(path, CONFIG, sender)
            monitor.step('collection_stopped', NOW+timedelta(minutes=20))
            self.assertEqual(len(sent), 5)
            monitor.step('collection_stopped', NOW+timedelta(days=1))
            self.assertEqual(len(sent), 6)

    def test_channel_change_does_not_reuse_original_delivery_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp).resolve()/'state.json'
            alerts.Monitor(path, CONFIG, lambda *_: True).step('closed', NOW)
            with self.assertRaises(ValueError):
                alerts.Monitor(path, {**CONFIG, 'sendkey': 'SCTanotherSynthetic123456789'})

    def test_messages_include_no_arbitrary_private_diagnostic(self):
        title, body=alerts.message('incident', 'collection_stopped', NOW)
        self.assertIn('北京时间', body)
        self.assertIn(alerts.STATISTICS_URL, body)
        self.assertNotIn('query-context', body)
        self.assertLessEqual(len(title), 32)


if __name__ == '__main__':
    unittest.main()
