"""Execute calendar and out-of-order UI response cases in Node."""
from pathlib import Path
import shutil,subprocess,unittest
class StatisticsUITests(unittest.TestCase):
    def test_calendar_and_stale_detail_behavior(self):
        node=shutil.which('node')
        if node is None:self.skipTest('Node unavailable')
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([node,str(root/'tests/statistics.test.js'),str(root/'src/sushiwait/web/statistics.js'),str(root/'src/sushiwait/web/reference-model.js')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
