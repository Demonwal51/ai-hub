"""1.2: the code on disk changed since the start (a development copy): seen, tried, restarted on it.
And a new conversation without a folder gets one of its own."""

import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from aihub import db, relaunch, store
from tests.helpers import TempDirMixin

try:
    from tests.test_sharing import H, SharingTestCase
except unittest.SkipTest:  # web dependencies missing
    SharingTestCase = None

A, B = "a" * 40, "b" * 40


def copy_at(root, version="1.2.0", head=None, packed=None):
    """A copy of the code: aihub/__init__.py, and a .git when a head is given."""
    (root / "aihub").mkdir(parents=True, exist_ok=True)
    (root / "aihub" / "__init__.py").write_text(f'"""x"""\n\n__version__ = "{version}"\n', encoding="utf-8")
    if head is not None:
        git = root / ".git"
        (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text(head, encoding="utf-8")
        if packed is not None:
            (git / "packed-refs").write_text(packed, encoding="utf-8")
    return root


class FakeGit:
    def __init__(self, log="", status=""):
        self.log, self.status, self.calls = log, status, []

    def __call__(self, root, *args, timeout=10):
        self.calls.append(args)
        return self.log if args[0] == "log" else self.status if args[0] == "status" else None


class CodeStateTests(TempDirMixin, unittest.TestCase):
    def test_the_commit_read_from_git_files(self):
        root = copy_at(self.make_tempdir(), head="ref: refs/heads/main\n")
        (root / ".git" / "refs" / "heads" / "main").write_text(A + "\n", encoding="utf-8")
        self.assertEqual(relaunch.git_head(root), A)
        (root / ".git" / "refs" / "heads" / "main").unlink()
        (root / ".git" / "packed-refs").write_text(f"# pack-refs\n{B} refs/heads/main\n", encoding="utf-8")
        self.assertEqual(relaunch.git_head(root), B)
        (root / ".git" / "HEAD").write_text(A, encoding="utf-8")  # detached
        self.assertEqual(relaunch.git_head(root), A)
        for head in ("ref: refs/../../dehors", "ref: HEAD", "pas un commit"):
            (root / ".git" / "HEAD").write_text(head, encoding="utf-8")
            self.assertIsNone(relaunch.git_head(root), head)
        self.assertIsNone(relaunch.git_head(self.make_tempdir()))

    def test_the_version_on_disk(self):
        root = copy_at(self.make_tempdir(), "3.4.5")
        self.assertEqual(relaunch.disk_version(root), "3.4.5")
        (root / "aihub" / "__init__.py").write_text("rien", encoding="utf-8")
        self.assertIsNone(relaunch.disk_version(root))
        self.assertIsNone(relaunch.disk_version(self.make_tempdir()))

    def test_an_installed_copy_compares_its_version(self):
        root = copy_at(self.make_tempdir(), "1.2.0")
        git = FakeGit()
        state = relaunch.code_state({"version": "1.2.0", "commit": None}, root, git=git)
        self.assertEqual((state["restart_needed"], state["git"], state["dirty"]), (False, False, None))
        self.assertEqual(git.calls, [])  # no git asked outside a development copy
        copy_at(root, "1.2.1")
        state = relaunch.code_state({"version": "1.2.0", "commit": None}, root, git=git)
        self.assertEqual((state["restart_needed"], state["running"], state["disk"]), (True, "1.2.0", "1.2.1"))

    def test_a_development_copy_compares_its_commit(self):
        root = copy_at(self.make_tempdir(), "1.2.0", head=A)
        git = FakeGit(log="Nouveau bouton\nCorrection\n", status="")
        state = relaunch.code_state({"version": "1.2.0", "commit": A}, root, git=git)
        self.assertEqual((state["restart_needed"], state["changes"], state["dirty"]), (False, [], []))
        self.assertNotIn("log", [c[0] for c in git.calls])
        (root / ".git" / "HEAD").write_text(B, encoding="utf-8")  # a commit came, the version did not change
        state = relaunch.code_state({"version": "1.2.0", "commit": A}, root, git=git)
        self.assertTrue(state["restart_needed"])
        self.assertEqual(state["changes"], ["Nouveau bouton", "Correction"])
        self.assertIn(f"{A}..{B}", git.calls[-2])
        self.assertEqual(git.calls[-1][-2:], relaunch.CODE_PATHS)  # only the code's files count
        git.status = " M aihub/web/app.py\n M aihub/store.py\n"
        state = relaunch.code_state({"version": "1.2.0", "commit": A}, root, git=git)
        self.assertEqual(state["dirty"], ["aihub/store.py", "aihub/web/app.py"])
        state = relaunch.code_state({"version": "1.2.0", "commit": A}, root, git=lambda *a, **k: None)
        self.assertEqual((state["restart_needed"], state["changes"], state["dirty"]), (True, [], None))


class HelperTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        (self.root / "data").mkdir()
        self.db_path = self.root / "data" / "aihub.db"
        conn = db.connect(str(self.db_path))
        store.create_conversation(conn, "Avant")
        conn.close()
        self.started = []

        class Process:
            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        def popen(command, **options):
            self.started.append((command, options.get("env", {}).get("AIHUB_UPDATE_PROBE")))
            return Process()
        self.popen = popen
        self.restart = ["python", "-m", "aihub", "--db", str(self.db_path), "serve"]

    def titles(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
        finally:
            conn.close()

    def test_restarted_and_checked(self):
        asked = []
        report = relaunch.run(self.db_path, "9.9.9", root=self.root, restart=self.restart, popen=self.popen,
                              health=lambda process, url, version, probe: asked.append((url, version, probe)) or True)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(asked, [("http://127.0.0.1:8770/api/health", "9.9.9", self.started[0][1])])
        self.assertIsNotNone(self.started[0][1])  # probed: the answer is from the new server
        self.assertEqual(relaunch.last_report(self.db_path)["status"], "ok")

    def test_a_server_that_does_not_stop_is_left_alone(self):
        report = relaunch.run(self.db_path, "9.9.9", root=self.root, pid=4242, restart=self.restart, popen=self.popen,
                              wait_s=0.2, alive=lambda pid: True)
        self.assertEqual(report["status"], "echec")
        self.assertIn("ne s'est pas arrêté", report["error"])
        self.assertEqual(self.started, [])

    def test_a_new_code_that_does_not_answer_gets_the_base_back_and_one_more_start(self):
        keep = relaunch.keep_copy(self.db_path, "9.9.9")
        self.assertTrue(keep.name.startswith("aihub-avant-9.9.9-"))
        conn = db.connect(str(self.db_path))
        store.create_conversation(conn, "Migrée")  # what the new code did to the base
        conn.close()
        with mock.patch("aihub.providers.claude_code._kill_tree") as kill:
            report = relaunch.run(self.db_path, "9.9.9", root=self.root, restart=self.restart, popen=self.popen,
                                  keep=keep, health=lambda process, url, version, probe: False)
        self.assertEqual(report["status"], "echec")
        kill.assert_called_once()
        self.assertEqual(len(self.started), 2)  # the try, then once more
        self.assertIsNone(self.started[1][1])
        self.assertEqual(self.titles(), ["Avant"])  # the base from before
        self.assertIn("base d'avant", report["error"])
        # Without a copy (no schema change), the base stays as it is.
        self.started.clear()
        with mock.patch("aihub.providers.claude_code._kill_tree"):
            report = relaunch.run(self.db_path, "9.9.9", root=self.root, restart=self.restart, popen=self.popen,
                                  health=lambda process, url, version, probe: False)
        self.assertNotIn("base d'avant", report["error"])
        self.assertEqual(len(self.started), 2)

    def test_the_trial_migrates_a_copy_never_the_base(self):
        out = self.root / "resultat.json"
        self.assertEqual(relaunch.check(self.db_path, out), 0)
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((result["ok"], result["schema"]), (True, db.SCHEMA_VERSION))
        old = self.root / "data" / "ancienne.db"
        conn = sqlite3.connect(old)
        conn.execute("PRAGMA user_version = 999")  # newer than this code: refused
        conn.close()
        self.assertEqual(relaunch.check(old, out), 1)
        result = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn("SchemaError", result["error"])
        conn = sqlite3.connect(old)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 999)  # never touched
        conn.close()
        # A base the new code migrates: only its copy is, the base stays as it was.
        bare = self.root / "data" / "nue.db"
        conn = sqlite3.connect(bare)
        conn.execute("CREATE TABLE autre (x)")
        conn.close()
        self.assertEqual(relaunch.check(bare, out), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["schema"], db.SCHEMA_VERSION)
        conn = sqlite3.connect(bare)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual([r[0] for r in conn.execute("SELECT name FROM sqlite_master")], ["autre"])
        finally:
            conn.close()

    def test_the_trial_said_even_when_the_new_code_dies(self):
        def silent(command, **options):
            return None  # the process died before writing its answer
        result = relaunch.preflight(self.db_path, self.root, run=silent)
        self.assertEqual(result["ok"], False)
        self.assertIn("avant de répondre", result["error"])

        def slow(command, **options):
            raise subprocess.TimeoutExpired(command, options["timeout"])
        self.assertIn("pas fini", relaunch.preflight(self.db_path, self.root, run=slow)["error"])

        def answers(command, **options):
            self.assertEqual(command[1:4], ["-m", "aihub.relaunch", "check"])
            self.assertEqual(options["cwd"], str(self.root))  # the code on disk, from its own folder
            Path(command[command.index("--out") + 1]).write_text('{"ok": true, "version": "9.9.9", "schema": 28}',
                                                                 encoding="utf-8")
        self.assertEqual(relaunch.preflight(self.db_path, self.root, run=answers)["version"], "9.9.9")

    def test_the_real_trial_in_a_process_of_its_own(self):
        root = Path(relaunch.__file__).resolve().parents[1]
        result = relaunch.preflight(self.db_path, root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.titles(), ["Avant"])


if SharingTestCase is not None:
    class RestartWebTests(SharingTestCase):
        def setUp(self):
            super().setUp()
            self.root = copy_at(self.make_tempdir(), "9.9.9", head=B)
            self.spawned, self.stopped, self.tried = [], [], []
            self.git = FakeGit(log="Plus simple\n")
            self.hooks(preflight=self.trial(True))

        def trial(self, ok, schema=None, error="ImportError: x"):
            def run(db_path, root):
                self.tried.append(root)
                conn = sqlite3.connect(db_path)
                try:
                    current = conn.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
                return {"ok": True, "version": "9.9.9", "schema": schema or current} if ok else {"ok": False, "error": error}
            return run

        def hooks(self, **more):
            self.app.state.update_hooks = {
                "get": lambda url, timeout=15: (_ for _ in ()).throw(OSError("pas de réseau")),
                "spawn": self.spawned.append, "stop": lambda: self.stopped.append(True), "root": self.root,
                "started": {"version": "1.2.0", "commit": A}, "git": self.git, "why_not": lambda: "git",
                "restart": ["python", "-m", "aihub", "--db", self.db_path, "serve"], **more}

        def test_the_owner_sees_what_changed(self):
            state = self.owner.get("/api/update", headers=H).json()
            code = state["code"]
            self.assertEqual((code["restart_needed"], code["running"], code["disk"]), (True, "1.2.0", "9.9.9"))
            self.assertEqual(code["changes"], ["Plus simple"])
            self.assertIsNone(state["latest"])
            self.assertIn("pas de réseau", state["error"])  # GitHub out of reach: said, never fatal
            guest, _ = self.guest("Lou")
            self.assertEqual(guest.get("/api/update", headers=H).status_code, 403)
            self.assertEqual(guest.post("/api/update/restart", headers=H).status_code, 403)
            self.assertEqual(self.spawned, [])

        def test_restarted_on_the_new_code(self):
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertEqual(r.status_code, 202, r.text)
            self.assertEqual(r.json(), {"restarting": "9.9.9", "backup": None})  # same schema: no copy
            self.assertEqual(self.tried, [self.root])
            self.assertEqual(self.stopped, [True])
            command = self.spawned[0]
            self.assertEqual(command[1:3], ["-m", "aihub.relaunch"])
            self.assertEqual(command[command.index("--version") + 1], "9.9.9")
            self.assertEqual(command[command.index("--root") + 1], str(self.root))
            self.assertEqual(json.loads(command[command.index("--restart") + 1])[-1], "serve")
            self.assertNotIn("--keep", command)

        def test_a_schema_change_keeps_the_base_from_before(self):
            self.hooks(preflight=self.trial(True, schema=db.SCHEMA_VERSION + 1))
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertEqual(r.status_code, 202, r.text)
            keep = Path(self.db_path).parent / r.json()["backup"]
            self.assertTrue(keep.is_file())
            self.assertEqual(self.spawned[0][self.spawned[0].index("--keep") + 1], str(keep))

        def test_refused_when_nothing_changed_an_ai_works_or_work_goes_on(self):
            self.hooks(started={"version": "9.9.9", "commit": B})
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertIn("rien à redémarrer", r.json()["detail"])
            self.hooks()
            with mock.patch.object(self.app.state.jobs, "busy", return_value=True):
                self.assertEqual(self.owner.post("/api/update/restart", headers=H).status_code, 409)
            self.git.status = " M aihub/store.py\n"
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertIn("pas encore enregistrés", r.json()["detail"])
            self.assertEqual((self.spawned, self.stopped, self.tried), ([], [], []))

        def test_a_new_code_that_does_not_start_leaves_the_server_on(self):
            self.hooks(preflight=self.trial(False, error="SyntaxError: invalid syntax (store.py, line 3)"))
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("SyntaxError", r.json()["detail"])
            self.assertIn("continue sur l'ancien", r.json()["detail"])
            self.assertEqual((self.spawned, self.stopped), ([], []))
            self.hooks(restart=["relance"])  # not started by « python -m aihub serve »
            r = self.owner.post("/api/update/restart", headers=H)
            self.assertIn("ne sait pas se relancer", r.json()["detail"])
            self.assertEqual(self.tried, [self.root])  # refused before the trial

    class AutomaticFolderTests(SharingTestCase):
        def test_a_conversation_without_a_folder_gets_one_of_its_own(self):
            spaces = Path(self.db_path).resolve().parent / "hub-espaces"  # beside the base « hub.db »
            r = self.owner.post("/api/conversations", json={"title": "Une idée"}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            first = Path(r.json()["conversation"]["workdir"])
            self.assertTrue(first.is_dir())
            self.assertTrue(first.is_relative_to(spaces))
            self.assertEqual(first.name, "Une idée")
            # The same title again: another folder, even while the first one is still empty.
            r = self.owner.post("/api/conversations", json={"title": "Une idée"}, headers=H)
            second = Path(r.json()["conversation"]["workdir"])
            self.assertEqual(second.name, "Une idée (2)")
            # A folder given is still checked.
            r = self.owner.post("/api/conversations", json={"title": "x", "workdir": "relatif"}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("chemin complet", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
