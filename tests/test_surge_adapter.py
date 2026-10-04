import json
from pathlib import Path
import shutil
import subprocess
import unittest


class SurgeAdapterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required only for adapter checks")
    def test_adapter_scope_passthrough_and_failure_paths(self):
        script=Path(__file__).with_name("surge_context.test.js")
        result=subprocess.run([shutil.which("node"),str(script)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report["checks"],26)
        self.assertEqual(report["upstreamCalls"],0)
        self.assertFalse(report["realCredentials"])


if __name__ == "__main__":unittest.main()
