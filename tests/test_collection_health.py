import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from tests.test_statistics_gateway import gateway, Hub


class CollectionHealthTests(unittest.TestCase):
    def test_private_fields_never_escape_and_old_heartbeat_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp).resolve()/'health.json'
            p.write_text(json.dumps({'state':'running','at':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),
                'successful_slots':5,'failed_slots':0,'secret':'synthetic-not-public','path':'/private/source'}))
            p.chmod(0o600)
            app=gateway.StatisticsGateway(Hub(),collection_status_file=p)
            result=app.collection_health()
            self.assertEqual(result['state'],'needs_attention')
            self.assertTrue(result['heartbeat_stale'])
            self.assertNotIn('secret',result);self.assertNotIn('path',result)
            self.assertTrue(app.allowed('/api/v1/collection/health'))

    def test_collection_display_changes_at_opening_not_midnight(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp).resolve()/'health.json'
            app=gateway.StatisticsGateway(Hub(),collection_status_file=p)
            cases=[('2026-10-18T02:00:00+08:00','2026-10-17','2026-10-18T10:30:00+08:00'),
                   ('2026-10-18T10:29:59+08:00','2026-10-17','2026-10-18T10:30:00+08:00'),
                   ('2026-10-18T10:30:00+08:00','2026-10-18','2026-10-19T11:00:00+08:00'),
                   ('2026-10-19T10:59:59+08:00','2026-10-18','2026-10-19T11:00:00+08:00'),
                   ('2026-10-01T02:00:00+08:00','2026-09-30','2026-10-01T10:30:00+08:00')]
            for stamp,day,switch in cases:
                now=datetime.fromisoformat(stamp)
                p.write_text(json.dumps({'state':'closed','at':stamp}));p.chmod(0o600)
                result=app.collection_health(now=now)
                self.assertEqual(result['display_date'],day)
                self.assertEqual(result['display_switch_at'],switch)

    def test_health_is_disabled_without_an_explicit_file(self):
        self.assertFalse(gateway.StatisticsGateway(Hub()).allowed('/api/v1/collection/health'))


if __name__=='__main__':unittest.main()
