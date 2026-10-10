"""Design adapter behavior and its fixture use the actual public projection."""
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from sushiwait.dailyfleet import SUMMARY_KEYS
import test_daily_view as daily_fixture
from test_remote_service import BASE


class DesignIntegrationTests(unittest.TestCase):
    def test_framework_neutral_adapter(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the design adapter checks')
        script = Path(__file__).with_name('statistics-client.test.mjs')
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('15 integration cases passed', result.stdout)

    def test_fixture_matches_public_daily_view_not_invented_fields(self):
        case = daily_fixture.DailyTests()
        view = case.view()
        for i, labels in enumerate([('0012', '0011', '0014'), ('0014', '0016', '0015'), ('0016', '0017', '0017')]):
            record = case.record(i * 60, labels)
            record['queries']['groupqueues']['payload']['queues']['reservationQueue'] = [str(7000 + i), str(7002 + i), str(7001 + i)]
            view.publish(record, key=('synthetic', i), run='synthetic')
        now = BASE + timedelta(seconds=125)
        root = Path(__file__).resolve().parents[1]
        fixture = json.loads((root / 'docs/design/integration/statistics.synthetic.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        self.assertEqual(fixture['day_detail'], view.detail('900001', '2026-10-06', now=now))
        expected = view.batch_index(now=now)
        expected.update(month='2026-10', store_names={'900001': '合成示例门店'})
        expected['days'] = {d: {s: {k: r[k] for k in SUMMARY_KEYS} for s, r in rows.items()} for d, rows in expected['days'].items()}
        self.assertEqual(fixture['month_index'], expected)


if __name__ == '__main__':
    unittest.main()
