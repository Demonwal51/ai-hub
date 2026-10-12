"""The one-click update: which release, the file checked, put in place, undone when it fails."""

import io
import json
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from aihub import updater

from .helpers import TempDirMixin
from .test_sharing import H, SharingTestCase

URL = f"https://github.com/{updater.REPO}/releases/download/v/ai-hub-9.0.0.zip"


def release_zip(version="9.0.0", folder=None, extra=None):
    folder = folder or f"ai-hub-{version}"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"{folder}/aihub/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr(f"{folder}/aihub/nouveau.py", "# la nouvelle version\n")
        archive.writestr(f"{folder}/requirements.txt", "fastapi\n")
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return data.getvalue()


def releases(*versions, draft=False):
    return [{"name": f"v{v}", "body": "nouveautés", "draft": draft, "html_url": "https://github.com/x",
             "assets": [{"name": f"ai-hub-{v}.zip", "browser_download_url": URL.replace("9.0.0", v), "size": 100}]}
            for v in versions]


class Answer:
    def __init__(self, data):
        self.data = data
        self.content = data if isinstance(data, bytes) else b""

    def json(self):
        return self.data


class ChoiceTest(TempDirMixin, unittest.TestCase):
    def test_backslash_traversal_duplicate_and_symlink_archives_are_rejected(self):
        tmp = self.make_tempdir()
        for name in ('ai-hub-9.0.0/aihub/../../dehors', 'ai-hub-9.0.0/..\\dehors',
                     'ai-hub-9.0.0/C:/dehors', 'ai-hub-9.0.0/aihub/__init__.py'):
            path = tmp / 'bad.zip'
            path.write_bytes(release_zip(extra={name: 'bad'}))
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                updater.verify(path, '9.0.0')
        entry = zipfile.ZipInfo('ai-hub-9.0.0/aihub/link')
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ai-hub-9.0.0/aihub/__init__.py', '__version__ = "9.0.0"')
            archive.writestr(entry, '/outside')
        with self.assertRaises(updater.UpdateError):
            updater.verify(path, '9.0.0')
    def test_extra_root_cannot_hide_beside_a_valid_release(self):
        target = self.make_tempdir() / 'unexpected-root.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('ai-hub-9.0.0/aihub/__init__.py', '__version__ = "9.0.0"')
            archive.writestr('other-root/extra.py', 'unexpected sibling')
        with self.assertRaises(updater.UpdateError):
            updater.verify(target, '9.0.0')

    def test_windows_case_aliases_and_special_entries_are_refused(self):
        import stat
        for name, mode in [('AIHUB/__init__.py', stat.S_IFREG), ('aihub/pipe', stat.S_IFIFO),
                           ('aihub/./extra.py', stat.S_IFREG), ('aihub/extra.py.', stat.S_IFREG)]:
            with self.subTest(name=name):
                target = self.make_tempdir() / 'bad.zip'
                with zipfile.ZipFile(target, 'w') as archive:
                    archive.writestr('ai-hub-9.0.0/aihub/__init__.py', '__version__ = "9.0.0"')
                    entry = zipfile.ZipInfo('ai-hub-9.0.0/' + name)
                    entry.external_attr = mode << 16
                    archive.writestr(entry, 'unexpected')
                with self.assertRaises(updater.UpdateError):
                    updater.verify(target, '9.0.0')

    def test_versions_in_order_and_the_newest_newer_one(self):
        keys = [updater.version_key(v) for v in ("0.3.0b1", "0.3.0", "0.3.1b2", "0.3.1", "1.0.0")]
        self.assertEqual(keys, sorted(keys))
        self.assertIsNone(updater.version_key("pas une version"))
        found = updater.latest(releases("0.2.0b1", "0.4.0b1", "0.3.5") + releases("9.9.9", draft=True), "0.3.0b1")
        self.assertEqual(found["version"], "0.4.0b1")
        self.assertIsNone(updater.latest(releases("0.3.0b1"), "0.3.0b1"))  # the same: nothing newer
        odd = [{"name": "x", "assets": [{"name": "autre-chose.zip"}]}, "pas un dict"]
        self.assertIsNone(updater.latest(odd, "0.1.0"))

    def test_github_asked_at_most_every_six_hours(self):
        calls = []
        clock = [1000.0]
        get = lambda url: calls.append(url) or Answer(releases("9.0.0"))
        with mock.patch.dict(updater._cache, clear=True):
            self.assertEqual(updater.check(get, lambda: clock[0], "0.3.0")["latest"]["version"], "9.0.0")
            clock[0] += 3600
            updater.check(get, lambda: clock[0], "0.3.0")
            clock[0] += updater.CHECK_EVERY_S
            updater.check(get, lambda: clock[0], "0.3.0")
            self.assertEqual(len(calls), 2)
            broken = updater.check(lambda url: (_ for _ in ()).throw(OSError("pas de réseau")), lambda: 99e9, "0.1.0")
            self.assertIsNone(broken["latest"])
            self.assertIn("pas de réseau", broken["error"])

    def test_only_ai_hub_of_that_version_from_its_own_releases(self):
        tmp = self.make_tempdir()
        cases = (
            (release_zip(extra={"../dehors.txt": "x"}), "inattendu"),
            (release_zip(folder="autre"), "inattendu"),
            (release_zip("8.0.0", folder="ai-hub-9.0.0"), "annoncée"),
            (b"pas un zip", "inattendu"),
        )
        for data, said in cases:
            with self.subTest(said=said), self.assertRaisesRegex(updater.UpdateError, said):
                updater.download({"version": "9.0.0", "url": URL}, tmp, get=lambda url, timeout: Answer(data))
        self.assertEqual(list(tmp.iterdir()), [])  # a refused file is not kept
        with self.assertRaisesRegex(updater.UpdateError, "adresse"):
            updater.download({"version": "9.0.0", "url": "https://exemple.test/ai-hub-9.0.0.zip"}, tmp)
        good = updater.download({"version": "9.0.0", "url": URL}, tmp, get=lambda url, timeout: Answer(release_zip()))
        self.assertEqual(good.name, "ai-hub-9.0.0.zip")

    def test_a_development_copy_is_left_to_git(self):
        tmp = self.make_tempdir()
        (tmp / "requirements.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(updater.why_not_here(tmp))
        (tmp / ".git").mkdir()
        self.assertIn("git", updater.why_not_here(tmp))
        here = Path(updater.__file__).resolve().parents[1]
        if (here / ".git").exists():  # this very copy (the mutation bench's has no .git)
            self.assertIn("git", updater.why_not_here(here))


class ApplyTest(TempDirMixin, unittest.TestCase):
    def test_unhealthy_restart_restores_migrated_database_and_code(self):
        import sqlite3
        root, zip_path = self.install()
        database = root / 'data/aihub.db'
        database.unlink()
        conn = sqlite3.connect(database)
        conn.execute('CREATE TABLE original (value TEXT)')
        conn.execute("INSERT INTO original VALUES ('preserved')")
        conn.commit(); conn.close()
        starts = []
        process = mock.Mock()
        process.wait.return_value = 0
        def launch(command, **options):
            starts.append(options)
            if len(starts) == 1:
                conn = sqlite3.connect(database)
                conn.execute('DROP TABLE original')
                conn.execute('PRAGMA user_version = 999')
                conn.commit(); conn.close()
            return process
        restart = ['python', '-m', 'aihub', '--db', str(database), 'serve', '--port', '12345']
        with mock.patch('aihub.providers.claude_code._kill_tree') as kill, self.assertRaisesRegex(updater.UpdateError, 'rétablie'):
            updater.apply(zip_path, '9.0.0', root, pip=False, restart=restart, popen=launch,
                          health=lambda process, url, version, probe: False)
        kill.assert_called_once_with(process)
        self.assertEqual(len(starts), 2)
        self.assertIn('AIHUB_UPDATE_PROBE', starts[0]['env'])
        self.assertNotIn('env', starts[1])
        conn = sqlite3.connect(database)
        try:
            self.assertEqual(conn.execute('SELECT value FROM original').fetchone()[0], 'preserved')
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
        finally:
            conn.close()
        self.assertTrue((root / 'aihub/ancien.py').is_file())
        self.assertFalse((root / 'aihub/nouveau.py').exists())

    def test_live_server_prevents_replacement(self):
        root, zip_path = self.install()
        with mock.patch.object(updater, '_alive', return_value=True), self.assertRaisesRegex(updater.UpdateError, 'arrêté'):
            updater.apply(zip_path, '9.0.0', root, pid=12345, wait_s=0, pip=False)
        self.assertTrue((root / 'aihub/ancien.py').exists())
    def install(self):
        root = self.make_tempdir()
        (root / "aihub").mkdir()
        (root / "aihub" / "__init__.py").write_text('__version__ = "0.3.0"\n', encoding="utf-8")
        (root / "aihub" / "ancien.py").write_text("# l'ancienne\n", encoding="utf-8")
        (root / "requirements.txt").write_text("vieux\n", encoding="utf-8")
        (root / "data").mkdir()
        (root / "data" / "aihub.db").write_bytes(b"les conversations")
        (root / ".venv").mkdir()
        zip_path = root / "data" / "ai-hub-9.0.0.zip"
        zip_path.write_bytes(release_zip())
        return root, zip_path

    def test_the_new_files_in_place_the_old_ones_kept_the_person_s_untouched(self):
        root, zip_path = self.install()
        started = []
        backup = updater.apply(zip_path, "9.0.0", root, pip=False, restart=["relance"],
                               popen=lambda cmd, **kw: started.append(cmd))
        self.assertTrue((root / "aihub" / "nouveau.py").exists())
        self.assertFalse((root / "aihub" / "ancien.py").exists())
        self.assertTrue((backup / "aihub" / "ancien.py").exists())
        self.assertEqual((root / "data" / "aihub.db").read_bytes(), b"les conversations")
        self.assertTrue((root / ".venv").is_dir())
        self.assertEqual(started, [["relance"]])

    def test_a_failure_puts_the_old_version_back_and_restarts_it(self):
        root, zip_path = self.install()
        started = []

        def failing_pip(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "pip")
        with self.assertRaises(subprocess.CalledProcessError):
            updater.apply(zip_path, "9.0.0", root, pip=True, run=failing_pip, restart=["relance"],
                          popen=lambda cmd, **kw: started.append(cmd))
        self.assertTrue((root / "aihub" / "ancien.py").exists())
        self.assertFalse((root / "aihub" / "nouveau.py").exists())
        self.assertEqual((root / "requirements.txt").read_text(encoding="utf-8"), "vieux\n")
        self.assertEqual(started, [["relance"]])  # the app comes back, as it was
        zip_path.write_bytes(b"abime")
        with self.assertRaises(updater.UpdateError):
            updater.apply(zip_path, "9.0.0", root, pip=False, restart=["relance"], popen=lambda cmd, **kw: started.append(cmd))
        self.assertEqual(len(started), 2)  # a bad file: the old server is started again too
        self.assertTrue((root / "aihub" / "ancien.py").exists())


class WebUpdateTest(SharingTestCase):
    def hooks(self, root, **more):
        self.spawned, self.stopped = [], []
        return {"get": lambda url, timeout=15: Answer(releases("9.0.0") if "api.github" in url else release_zip()),
                "spawn": self.spawned.append, "stop": lambda: self.stopped.append(True), "restart": ["relance"],
                "root": root, "why_not": lambda: None, **more}

    def test_the_owner_updates_in_a_click(self):
        root = self.make_tempdir()
        (root / "requirements.txt").write_text("x", encoding="utf-8")
        with mock.patch.dict(updater._cache, clear=True):
            self.app_hooks(self.hooks(root))
            state = self.owner.get("/api/update", headers=H).json()
            self.assertEqual((state["latest"]["version"], state["can_apply"]), ("9.0.0", True))
            guest, _ = self.guest("Lou")
            self.assertEqual(guest.get("/api/update", headers=H).status_code, 403)
            self.assertEqual(guest.post("/api/update/apply", headers=H).status_code, 403)
            r = self.owner.post("/api/update/apply", headers=H)
            self.assertEqual(r.status_code, 202, r.text)
            self.assertEqual(self.stopped, [True])
            command = self.spawned[0]
            self.assertEqual(command[1:4], ["-m", "aihub.updater", "apply"])
            self.assertEqual(json.loads(command[command.index("--restart") + 1]), ["relance"])
            self.assertTrue((root / "data" / "mise-a-jour" / "ai-hub-9.0.0.zip").is_file())

    def test_never_while_an_ai_works_nor_on_a_development_copy(self):
        root = self.make_tempdir()
        with mock.patch.dict(updater._cache, clear=True):
            self.app_hooks(self.hooks(root))
            with mock.patch.object(self.app.state.jobs, "busy", return_value=True):
                self.assertEqual(self.owner.post("/api/update/apply", headers=H).status_code, 409)
            (root / ".git").mkdir()
            self.app_hooks(self.hooks(root, why_not=lambda: updater.why_not_here(root)))
            self.assertFalse(self.owner.get("/api/update", headers=H).json()["can_apply"])
            r = self.owner.post("/api/update/apply", headers=H)
            self.assertIn("git", r.json()["detail"])
            self.assertEqual(self.spawned, [])

    def app_hooks(self, hooks):
        self.app.state.update_hooks = hooks  # GitHub, the helper and the stop, replaced


if __name__ == "__main__":
    unittest.main()
