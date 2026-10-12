"""The one file to download (tools/build_release.py): what runs the app, nothing else."""

import subprocess
import sys
import unittest
import zipfile
import tarfile
from pathlib import Path

from .helpers import TempDirMixin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def in_git():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipUnless(in_git(), "le paquet se fait depuis le dernier commit : pas de dépôt git ici")
class ReleaseTest(TempDirMixin, unittest.TestCase):
    def test_only_what_runs_the_app_and_the_windows_scripts(self):
        import build_release
        target, names = build_release.build(self.make_tempdir())
        self.assertEqual(target.name, f"ai-hub-{build_release.version()}.zip")
        with zipfile.ZipFile(target) as archive:
            listed = archive.namelist()
            prefix = f"ai-hub-{build_release.version()}/"
            self.assertTrue(all(n.startswith(prefix) for n in listed))
            inside = {n[len(prefix):] for n in listed}
            for needed in ("aihub/__init__.py", "aihub/web/static/app.js", "requirements.txt", "LICENSE",
                           "installer.bat", "Lancer AI Hub.bat"):
                self.assertIn(needed, inside)
            self.assertFalse([n for n in inside if n.startswith(("tests/", "tools/", "data/")) or "__pycache__" in n])
            for script in ("installer.bat", "Lancer AI Hub.bat"):
                data = archive.read(prefix + script)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""), script)  # CRLF only: cmd needs it
            for script in ('installer.sh', 'lancer.sh'):
                self.assertTrue((archive.getinfo(prefix + script).external_attr >> 16) & 0o111)
        with tarfile.open(target.with_name(target.stem + '-linux.tar.gz')) as archive:
            self.assertEqual({m.name for m in archive.getmembers()}, set(listed))
            for script in ('installer.sh', 'lancer.sh'):
                self.assertTrue(archive.getmember(prefix + script).mode & 0o111)
            self.assertTrue(all(m.uid == 0 and not m.uname for m in archive.getmembers()))


if __name__ == "__main__":
    unittest.main()
