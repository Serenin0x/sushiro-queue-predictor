"""Authorization UI runs against synthetic browser, status and image fixtures."""
from pathlib import Path
import shutil
import subprocess
import unittest

class ReauthorizationUITests(unittest.TestCase):
    def run_script(self,name):
        node=shutil.which('node')
        if node is None:self.skipTest('Node unavailable')
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([node,str(root/'tests'/name)],cwd=root,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_download_native_save_and_browser_handoff(self):
        self.run_script('recovery-download.test.js')

    def test_authorization_states_expiry_race_and_unknown_confirmation(self):
        self.run_script('recovery-motion.test.js')
