"""The one-click update: which release, the file checked, put in place, undone when it fails."""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from aihub import updater
from aihub.web.app import create_app

from .helpers import FAKE_COMMAND, TempDirMixin
from .test_sharing import H, SharingTestCase

URL = f"https://github.com/{updater.REPO}/releases/download/v/ai-hub-9.0.0.zip"
SUMS_URL = f"https://github.com/{updater.REPO}/releases/download/v/SHA256SUMS"


def release_zip(version="9.0.0", folder=None, extra=None, requirements="fastapi\n"):
    folder = folder or f"ai-hub-{version}"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(f"{folder}/aihub/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr(f"{folder}/aihub/nouveau.py", "# la nouvelle version\n")
        archive.writestr(f"{folder}/requirements.txt", requirements)
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return data.getvalue()


def releases(*versions, draft=False):
    return [{"name": f"v{v}", "body": "nouveautés", "draft": draft, "html_url": "https://github.com/x",
             "assets": [{"name": f"ai-hub-{v}.zip", "browser_download_url": URL.replace("9.0.0", v), "size": 100},
                        {"name": "SHA256SUMS", "browser_download_url": SUMS_URL.replace("/v/", f"/v{v}/")}]}
            for v in versions]


def sums(data, name="ai-hub-9.0.0.zip"):
    """A release's SHA256SUMS, as release.yml writes it (the Linux file listed too)."""
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n{'0' * 64}  ai-hub-9.0.0-linux.tar.gz\n".encode()


def github(data, listed=None):
    """GitHub's downloads: the file, and its release's SHA256SUMS (the true one, unless `listed`)."""
    return lambda url, timeout=15: Answer((sums(data) if listed is None else listed)
                                          if url.endswith("/SHA256SUMS") else data)


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
        self.assertTrue(found["sums"].endswith("/v0.4.0b1/SHA256SUMS"))  # the list of that very release
        unlisted = releases("0.5.0")
        unlisted[0]["assets"].pop()
        self.assertIsNone(updater.latest(unlisted, "0.3.0b1")["sums"])
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
                updater.download({"version": "9.0.0", "url": URL, "sums": SUMS_URL}, tmp, get=github(data))
        self.assertEqual(list(tmp.iterdir()), [])  # a refused file is not kept
        with self.assertRaisesRegex(updater.UpdateError, "adresse"):
            updater.download({"version": "9.0.0", "url": "https://exemple.test/ai-hub-9.0.0.zip", "sums": SUMS_URL}, tmp)
        good = updater.download({"version": "9.0.0", "url": URL, "sums": SUMS_URL}, tmp, get=github(release_zip()))
        self.assertEqual(good.name, "ai-hub-9.0.0.zip")

    def test_the_file_is_the_one_its_release_lists_in_sha256sums(self):
        tmp = self.make_tempdir()
        data = release_zip()
        release = {"version": "9.0.0", "url": URL, "sums": SUMS_URL}
        other = "0" * 64
        cases = (
            ({"sums": None}, sums(data), "pas de liste d'empreintes"),
            ({"sums": "https://exemple.test/SHA256SUMS"}, sums(data), "adresse de la liste d'empreintes"),
            ({}, f"{other}  ai-hub-9.0.0-linux.tar.gz\n".encode(), "pas une ligne"),
            ({}, sums(data) + sums(data), "pas une ligne"),
            ({}, sums(data).replace(b"  ", b" ", 1), "illisible"),
            ({}, sums(data) + b"pas une empreinte\n", "illisible"),
            ({}, b"", "illisible"),
            ({}, b"\xff\xfe", "illisible"),
            ({}, sums(data) + b"#" * updater.MAX_SUMS, "trop grosse"),
            ({}, f"{other}  ai-hub-9.0.0.zip\n".encode(), "pas l'empreinte publiée"),
            ({}, sums(release_zip(extra={"ai-hub-9.0.0/aihub/ajout.py": "x"})), "pas l'empreinte publiée"),
        )
        for change, listed, said in cases:
            with self.subTest(said=said, listed=listed[:80]):
                with self.assertRaisesRegex(updater.UpdateError, said):
                    updater.download({**release, **change}, tmp, get=github(data, listed))
                self.assertEqual(list(tmp.iterdir()), [])  # refused: the downloaded file is not kept
        unreachable = lambda url, timeout=15: (_ for _ in ()).throw(OSError("introuvable")) if url == SUMS_URL else Answer(data)
        with self.assertRaisesRegex(updater.UpdateError, "SHA256SUMS impossible à lire : introuvable"):
            updater.download(release, tmp, get=unreachable)
        self.assertEqual(list(tmp.iterdir()), [])
        good = updater.download(release, tmp, get=github(data))
        self.assertEqual(good.read_bytes(), data)

    def test_the_journal_is_told_why_the_new_server_is_not_taken(self):
        ended = mock.Mock(**{"poll.return_value": 3})
        why = []
        self.assertFalse(updater.wait_health(ended, "http://127.0.0.1:9/api/health", "9.0.0", "sonde", why=why))
        self.assertEqual(why, ["le nouveau serveur s'est arrêté (code de sortie 3)"])
        running = mock.Mock(**{"poll.return_value": None})
        answers = (({"ready": True, "version": "8.0.0", "probe": "sonde"}, "il répond en version 8.0.0, pas en 9.0.0"),
                   ({"ready": True, "version": "9.0.0", "probe": "autre"}, "sans la sonde de ce lancement"),
                   ({"ready": False, "version": "9.0.0", "probe": "sonde"}, "sans être prêt"),
                   (OSError("connexion refusée"), "pas de réponse (connexion refusée)"))
        for answer, said in answers:
            def urlopen(url, timeout):
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(json.dumps(answer).encode())
            why = []
            with self.subTest(said=said), mock.patch("urllib.request.urlopen", urlopen):
                self.assertFalse(updater.wait_health(running, "http://x/api/health", "9.0.0", "sonde", timeout=0.3, why=why))
                self.assertEqual(len(why), 1)
                self.assertIn("délai de 0.3 s dépassé", why[0])
                self.assertIn(said, why[0])
        with mock.patch("urllib.request.urlopen", lambda url, timeout: io.BytesIO(
                json.dumps({"ready": True, "version": "9.0.0", "probe": "sonde"}).encode())):
            self.assertTrue(updater.wait_health(running, "http://x/api/health", "9.0.0", "sonde", why=why))

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
                          health=lambda process, url, version, probe, why: False)
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

    def refused(self, root, zip_path, **options):
        """apply() with a new server that does not answer (undone); the journal written meanwhile."""
        options = {"pip": False, "popen": lambda command, **kw: mock.Mock(**{"wait.return_value": 0}),
                   "health": lambda process, url, version, probe, why: False,
                   "restart": ['python', '-m', 'aihub', '--db', str(root / 'data/absente.db'), 'serve'], **options}
        with mock.patch('aihub.providers.claude_code._kill_tree'), self.assertRaisesRegex(updater.UpdateError, 'rétablie'):
            updater.apply(zip_path, '9.0.0', root, **options)
        log = root / 'data/mise-a-jour/journal.txt'
        return log.read_text(encoding='utf-8') if log.exists() else ''

    def test_undoing_never_runs_the_code_it_undoes(self):
        root, zip_path = self.install()
        name = 'aihub.providers.claude_code'
        self.addCleanup(sys.modules.__setitem__, name, sys.modules[name])
        new_code = types.ModuleType(name)
        new_code._kill_tree = mock.Mock()
        process = mock.Mock(**{"wait.return_value": 0})

        def health(process, url, version, probe, why):
            sys.modules[name] = new_code  # the files are swapped: an import now reads the new version's code
            return False
        with mock.patch(f'{name}._kill_tree') as kill, self.assertRaises(updater.UpdateError):
            updater.apply(zip_path, '9.0.0', root, pip=False, popen=lambda command, **kw: process, health=health,
                          restart=['python', '-m', 'aihub', '--db', str(root / 'data/absente.db'), 'serve'])
        kill.assert_called_once_with(process)  # the old version's own
        new_code._kill_tree.assert_not_called()

    def test_undoing_puts_the_old_dependencies_back_when_they_differ(self):
        for new, expected, said in (("fastapi\n", ["fastapi\n", "vieux\n"], "dépendances de l'ancienne version réinstallées"),
                                    ("vieux\n", ["vieux\n"], "requirements.txt identique")):
            with self.subTest(new=new):
                root, zip_path = self.install()
                (root / "requirements.txt").write_bytes(b"vieux\r\n")  # the same lines, whatever their ends
                zip_path.write_bytes(release_zip(requirements=new))
                installed = []

                def pip(command, **options):
                    self.assertEqual(command[-2:], ["-r", str(root.resolve() / "requirements.txt")])
                    installed.append((root / "requirements.txt").read_text(encoding="utf-8"))
                log = self.refused(root, zip_path, pip=True, run=pip)
                self.assertEqual(installed, expected)  # the new ones, then the old ones again when they differ
                self.assertIn(said, log)
        root, zip_path = self.install()
        installed = []

        def broken_pip(command, **options):
            installed.append((root / "requirements.txt").read_text(encoding="utf-8"))
            if len(installed) == 1:
                raise subprocess.CalledProcessError(1, "pip")  # half done: the venv may have changed
        with self.assertRaises(subprocess.CalledProcessError):
            updater.apply(zip_path, '9.0.0', root, run=broken_pip, restart=["relance"], popen=lambda command, **kw: None)
        self.assertEqual(installed, ["fastapi\n", "vieux\n"])
        root, zip_path = self.install()

        def pip(command, **options):
            if len(installed) == 1:
                raise subprocess.CalledProcessError(1, "pip")
            installed.append(command)
        installed = []
        log = self.refused(root, zip_path, pip=True, run=pip)
        self.assertIn("dépendances de l'ancienne version non réinstallées", log)
        self.assertTrue((root / 'aihub/ancien.py').is_file())  # the old version back all the same

    def test_the_journal_says_why_and_the_end_of_the_server_log(self):
        root, zip_path = self.install()
        log = root / 'data' / 'serveur.log'
        log.write_text('--- ancien démarrage\nune erreur d\'avant\n', encoding='utf-8')
        started = []

        def launch(command, **options):
            started.append(command)
            if len(started) == 1:  # the new version writes, then stops
                with log.open('a', encoding='utf-8') as fh:
                    fh.write('--- démarrage\n\nTraceback (most recent call last):\nImportError: nouveau\n')
            return mock.Mock(**{"poll.return_value": 3, "wait.return_value": 3})
        restart = ['python', '-m', 'aihub', '--db', str(root / 'data/absente.db'), 'serve', '--journal', 'data/serveur.log']
        with mock.patch('aihub.providers.claude_code._kill_tree'), \
                self.assertRaisesRegex(updater.UpdateError, r"\(le nouveau serveur s'est arrêté \(code de sortie 3\)\)"):
            updater.apply(zip_path, '9.0.0', root, pip=False, restart=restart, popen=launch)
        text = (root / 'data/mise-a-jour/journal.txt').read_text(encoding='utf-8')
        shown = [line for line in text.splitlines() if line.startswith('    | ')]
        self.assertEqual(shown, ['    | --- démarrage', '    | Traceback (most recent call last):',
                                 '    | ImportError: nouveau'])  # since the restart only, blank lines left out
        self.assertIn("serveur.log depuis la relance :", text)
        self.assertEqual(len(started), 2)  # the old version started again

    def test_the_end_of_the_server_log_is_short(self):
        log = self.make_tempdir() / 'serveur.log'
        log.write_bytes(b'avant\n' + b''.join(f'ligne {n}\n'.encode() for n in range(1, 16)))
        self.assertEqual(updater.log_end(log, 6), [f'ligne {n}' for n in range(6, 16)])  # the last ten
        self.assertEqual(updater.log_end(log, 10_000), [f'ligne {n}' for n in range(6, 16)])  # begun again since
        self.assertEqual(updater.log_end(log.with_name('absent.log'), 0), [])

    def test_no_write_ahead_log_left_beside_the_saved_base(self):
        import sqlite3
        root, zip_path = self.install()
        database = root / 'data/aihub.db'
        database.unlink()
        conn = sqlite3.connect(database)
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('CREATE TABLE original (value TEXT)')
        conn.execute("INSERT INTO original VALUES ('gardée')")
        conn.commit(); conn.close()
        self.refused(root, zip_path, restart=['python', '-m', 'aihub', '--db', str(database), 'serve'])
        saved = next((root / 'data/mise-a-jour').glob('avant-9.0.0-*/base-avant.db'))
        self.assertEqual(sorted(p.name for p in saved.parent.glob('base-avant*')), ['base-avant.db'])
        conn = sqlite3.connect(saved)
        try:
            self.assertEqual(conn.execute('SELECT value FROM original').fetchone()[0], 'gardée')  # kept whole
        finally:
            conn.close()

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
        files = github(release_zip())
        return {"get": lambda url, timeout=15: Answer(releases("9.0.0")) if "api.github" in url else files(url, timeout),
                "spawn": self.spawned.append, "stop": lambda: self.stopped.append(True), "restart": ["relance"],
                "root": root, "why_not": lambda: None, **more}

    def test_a_stopped_server_is_not_held_by_its_last_resort(self):
        root = self.make_tempdir()
        (root / "requirements.txt").write_text("x", encoding="utf-8")
        hooks = self.hooks(root)
        del hooks["stop"]  # the real stop, its timers held back below
        made = []

        class Held(threading.Timer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # Made as by the real server, whose threads are no daemons (the test client's are).
                self.daemon = False

            def start(self):
                made.append(self)  # never started: this test's server goes on
        with mock.patch.dict(updater._cache, clear=True), mock.patch("threading.Timer", Held):
            self.app_hooks(hooks)
            r = self.owner.post("/api/update/apply", headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        last = [timer for timer in made if timer.function is os._exit]
        self.assertEqual([(timer.interval, timer.daemon) for timer in last], [(20.0, True)])

    def test_the_probe_answers_but_is_not_inherited(self):
        with mock.patch.dict(os.environ, {"AIHUB_UPDATE_PROBE": "sonde-1"}):
            app = create_app(str(self.make_tempdir() / "autre.db"), command=FAKE_COMMAND, env=self.env, run_scheduler=False)
            self.assertNotIn("AIHUB_UPDATE_PROBE", os.environ)  # the processes started from here do not get it
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            self.assertEqual(client.get("/api/health").json()["probe"], "sonde-1")

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
