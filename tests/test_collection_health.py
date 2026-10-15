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

    def test_health_is_disabled_without_an_explicit_file(self):
        self.assertFalse(gateway.StatisticsGateway(Hub()).allowed('/api/v1/collection/health'))


if __name__=='__main__':unittest.main()
