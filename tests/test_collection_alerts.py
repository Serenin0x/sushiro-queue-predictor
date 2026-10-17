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

    def test_store_isolation_and_global_rate_backoff_have_distinct_alerts(self):
        status=self.status(failed_slots=1,isolation_policy='store-isolation-v1',unavailable_store_ids=['3025'])
        self.assertEqual(alerts.condition(status,self.status(),NOW),'collection_partial_failure')
        status['unavailable_store_ids']=[]
        self.assertEqual(alerts.condition(status,self.status(),NOW),'healthy')
        status.update(state='needs_attention',error_code='cloud_rate_limited')
        self.assertEqual(alerts.condition(status,self.status(),NOW),'collection_rate_limited')
        self.assertIn('无需重新扫码',alerts.message('incident','collection_rate_limited',NOW)[1])

    def test_sync_incident_is_not_delayed_by_existing_partial_store_incident(self):
        with tempfile.TemporaryDirectory() as temp:
            sent=[]
            monitor=alerts.Monitor(Path(temp).resolve()/'state.json',CONFIG,lambda *args:sent.append(args) is None)
            monitor.step('collection_partial_failure',NOW)
            self.assertEqual(monitor.step('public_sync_stopped',NOW+timedelta(minutes=1)),'accepted')
            self.assertEqual(monitor.step('public_sync_stopped',NOW+timedelta(minutes=2)),'suppressed')
            self.assertEqual(monitor.step('healthy',NOW+timedelta(minutes=3)),'accepted')
            self.assertEqual(len(sent),3)
            self.assertIn('网页同步',sent[1][2])

    def test_invalid_projection_timestamp_is_reported_as_sync_failure(self):
        for projection in ({'state':'running'},self.status(at='invalid'),self.status(at=None)):
            self.assertEqual(alerts.condition(self.status(),projection,NOW),'public_sync_stopped')

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
        closed = NOW.replace(hour=14)
        self.assertEqual(alerts.condition(self.status(state='closed'), {}, closed), 'closed')

    def test_first_sample_cannot_stay_starting_forever(self):
        at = NOW.replace(hour=3, minute=4)
        for changes in ({'state':'starting'}, {'last_progress_at':None},
                        {'successful_slots':0}, {'state':'closed'}, {'state':'completed'},
                        {'last_progress_at':(at-timedelta(days=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.assertEqual(alerts.condition(self.status(at=at.isoformat(), **changes), {}, at),
                                 'collection_not_started')
        grace = at.replace(minute=2)
        self.assertEqual(alerts.condition(self.status(at=grace.isoformat(), state='starting'), {}, grace),
                         'starting')

    def test_weekend_and_holiday_opening_are_independent_of_reported_state(self):
        for at in (datetime(2026,10,17,2,34,tzinfo=timezone.utc),
                   datetime(2026,10,1,2,34,tzinfo=timezone.utc)):
            self.assertEqual(alerts.condition(self.status(at=at.isoformat(),state='closed'), {}, at),
                             'collection_not_started')

    def test_calendar_progress_alarm_uses_declared_day_profile(self):
        progress=datetime.fromisoformat('2026-10-18T12:00:00+08:00')
        for age,expected in [(239,'healthy'),(241,'collection_progress_stale')]:
            now=progress+timedelta(seconds=age)
            status=self.status(at=now.isoformat(),last_progress_at=progress.isoformat(),cadence='calendar-meal-peaks-v1')
            self.assertEqual(alerts.condition(status,self.status(at=now.isoformat()),now),expected)

    def test_peak_progress_alarm_follows_smooth_cadence(self):
        progress=NOW.replace(hour=4,minute=0)
        due=alerts.next_sample_at(progress)+timedelta(seconds=120)
        current=self.status(at=due.isoformat(),last_progress_at=progress.isoformat(),cadence='meal-peaks-v1')
        self.assertEqual(alerts.condition(current,self.status(at=due.isoformat()),due),'healthy')
        current['at']=(due+timedelta(seconds=1)).isoformat()
        self.assertEqual(alerts.condition(current,{},due+timedelta(seconds=1)),
                         'collection_progress_stale')
        self.assertEqual((due-progress).total_seconds(),300)

    def test_future_progress_is_not_treated_as_healthy(self):
        self.assertEqual(alerts.condition(self.status(last_progress_at=(NOW+timedelta(minutes=2)).isoformat()),
                                         self.status(),NOW),'collection_status_unavailable')

    def test_authorization_alert_escalates_without_waiting_six_hours(self):
        with tempfile.TemporaryDirectory() as temp:
            sent=[]
            monitor=alerts.Monitor(Path(temp).resolve()/'state.json',CONFIG,
                                   lambda *args: sent.append(args) is None)
            monitor.step('collection_heartbeat_stale',NOW)
            link='https://124.222.57.236/recover/#'+'A'*43
            self.assertEqual(monitor.step('collection_stopped',NOW+timedelta(minutes=1),lambda:link),
                             'accepted')
            self.assertIn(link,sent[1][2])
            self.assertEqual(monitor.step('collection_stopped',NOW+timedelta(minutes=2)), 'suppressed')

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

    def test_recovery_link_is_fixed_host_and_only_generated_when_due(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]; sent=[]
            link='https://124.222.57.236/recover/#'+'A'*43
            def issue(): calls.append(1); return link
            monitor=alerts.Monitor(Path(temp).resolve()/'state.json', CONFIG,
                                   lambda *args: sent.append(args) is None)
            monitor.step('collection_stopped', NOW, issue)
            monitor.step('collection_stopped', NOW+timedelta(minutes=1), issue)
            monitor.step('healthy', NOW+timedelta(minutes=2), issue)
            self.assertEqual(len(calls),1)
            self.assertIn(link,sent[0][2]); self.assertNotIn(link,sent[1][2])
        for link in ('http://124.222.57.236/recover/#'+'A'*43,
                     'https://example.com/recover/#'+'A'*43):
            with self.assertRaises(ValueError): alerts.message('incident','collection_stopped', NOW,link)


if __name__ == '__main__':
    unittest.main()
