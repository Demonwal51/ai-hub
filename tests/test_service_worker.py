import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js nécessaire pour les vérifications du service worker')
class ServiceWorkerTest(unittest.TestCase):
    def test_cache_revocation_and_delayed_identity_response(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which('node'), str(root / 'tests/service_worker_checks.js'),
                                 str(root / 'aihub/web/static/sw.js')], capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
