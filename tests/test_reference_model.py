"""Run the same pure model shipped to browsers, without official requests."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ReferenceModelTests(unittest.TestCase):
    def test_forecasts_and_temporal_replay(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node unavailable')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, str(root/'tests/reference-model.test.js'),
            str(root/'src/sushiwait/web/reference-model.js')], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
