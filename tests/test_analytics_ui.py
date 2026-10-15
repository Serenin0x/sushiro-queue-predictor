import shutil
import subprocess
import unittest

class AnalyticsUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js not installed')
    def test_analysis_view_lifecycle_and_store_scope(self):
        subprocess.run(['node','tests/analytics-ui.test.js'],check=True,timeout=15,capture_output=True,text=True)
