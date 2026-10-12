"""Offline image imports must retain the approved content and deployment scope."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
SPEC = importlib.util.spec_from_file_location('official_cloud_image',
    DEPLOY / 'official_cloud_image.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def image_result(identity, *, architecture='amd64', layers=guard.LAYERS):
    body = '\n'.join(json.dumps(v) for v in (identity, architecture, 'linux', list(layers)))
    return subprocess.CompletedProcess([], 0, stdout=body.encode(), stderr=b'')


class OfficialCloudImageTests(unittest.TestCase):
    def test_manifest_import_binds_once_and_verifies_without_pulling(self):
        # Docker's containerd store can keep the manifest ID after an OCI load,
        # without a repository digest or config-ID lookup. This was the cloud case.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = (DEPLOY / 'yyb_cloud_trial.compose.yaml').read_bytes()
            target = root / 'yyb_cloud_trial.compose.yaml'
            target.write_bytes(original)
            def inspect(arguments, **kwargs):
                self.assertEqual(arguments[:3], ['docker', 'image', 'inspect'])
                self.assertEqual(arguments[4], '--format')
                self.assertNotIn('Env', arguments[5])
                if arguments[3] == guard.CONFIG:
                    return subprocess.CompletedProcess(arguments, 1, stdout=b'', stderr=b'')
                self.assertEqual(arguments[3], guard.MANIFEST)
                return image_result(guard.MANIFEST)
            with patch.object(guard.subprocess, 'run', side_effect=inspect) as command:
                result = guard.main(root, 'bind-local')
                self.assertEqual(command.call_count, 2)
                guard.main(root, 'verify')
                self.assertEqual(command.call_count, 3)
            self.assertEqual(result['network_pulls_added'], 0)
            self.assertFalse(result['container_started'])
            self.assertEqual(target.read_bytes(), original.replace(
                guard.SOURCE.encode(), guard.MANIFEST.encode()))
            self.assertEqual((root / 'yyb_cloud_trial.compose.original.yaml').read_bytes(), original)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_present_but_wrong_image_never_falls_back_or_rewrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'yyb_cloud_trial.compose.yaml'
            original = (DEPLOY / 'yyb_cloud_trial.compose.yaml').read_bytes()
            for result in (image_result(guard.CONFIG, architecture='arm64'),
                           image_result(guard.CONFIG, layers=guard.LAYERS[:-1])):
                with self.subTest(result=result.stdout):
                    target.write_bytes(original)
                    with patch.object(guard.subprocess, 'run', return_value=result) as command:
                        with self.assertRaisesRegex(ValueError, 'approved_image_mismatch'):
                            guard.main(root, 'bind-local')
                        self.assertEqual(command.call_count, 1)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertFalse((root / 'yyb_cloud_trial.compose.original.yaml').exists())

    def test_unapproved_compose_stops_before_docker_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'yyb_cloud_trial.compose.yaml'
            changed = (DEPLOY / 'yyb_cloud_trial.compose.yaml').read_bytes().replace(
                b'127.0.0.1:18081', b'0.0.0.0:18081')
            target.write_bytes(changed)
            with patch.object(guard.subprocess, 'run') as command:
                with self.assertRaisesRegex(ValueError, 'approved_compose_mismatch'):
                    guard.main(root, 'bind-local')
                command.assert_not_called()
            self.assertEqual(target.read_bytes(), changed)
            self.assertFalse((root / 'yyb_cloud_trial.compose.original.yaml').exists())
