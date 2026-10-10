"""Precaution state transitions and the same module's actual page integration."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ReferencePrecautionTests(unittest.TestCase):
    def test_private_precautions_and_page_integration(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node unavailable')
        root = Path(__file__).resolve().parents[1]
        web = root/'src/sushiwait/web'
        result = subprocess.run([node, str(root/'tests/reference-alerts.test.js'),
            str(web/'reference-alerts.js'), str(web/'statistics.js'), str(web/'reference-model.js')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
