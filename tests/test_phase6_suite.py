"""End of phase 6: connected devices, keeping the PC awake, start with Windows, restore."""

import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from aihub import auth, backup, cli, db, eveil, store
from tests.helpers import FAKE_COMMAND, TempDirMixin

try:
    from fastapi.testclient import TestClient
    from aihub.web.app import create_app
except ImportError:  # web dependencies missing
    TestClient = None


@unittest.skipIf(TestClient is None, "web dependencies missing")
class DeviceTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.db_path = str(self.make_tempdir() / "hub.db")
        self.password = secrets.token_urlsafe(18)  # a test value made for this run
        conn = db.connect(self.db_path)
        auth.set_password(conn, self.password)
        conn.close()
        self.app = create_app(self.db_path, command=FAKE_COMMAND, auth_policy=auth.Policy())

    def device(self, agent):
        client = TestClient(self.app, client=("127.0.0.1", 1), headers={"User-Agent": agent, "X-AIHub": "1"})
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"password": self.password}).status_code, 200)
        return client

    def test_list_close_one_and_close_the_others(self):
        pc = self.device("Mozilla/5.0 (Windows NT 10.0) Chrome/130")
        phone = self.device("Mozilla/5.0 (Linux; Android 15) Chrome/130 Mobile")
        tablet = self.device("Mozilla/5.0 (iPad) Safari/605")
        listed = pc.get("/api/sessions").json()["sessions"]
        self.assertEqual(len(listed), 3)
        self.assertEqual([s["current"] for s in listed].count(True), 1)
        mine = next(s for s in listed if s["current"])
        self.assertIn("Windows", mine["user_agent"])
        self.assertNotIn("token_hash", mine)  # nothing that would open a session
        phone_id = next(s["id"] for s in listed if "Android" in s["user_agent"])
        self.assertEqual(pc.delete(f"/api/sessions/{phone_id}").status_code, 200)
        self.assertEqual(phone.get("/api/conversations").status_code, 401)
        self.assertEqual(pc.delete(f"/api/sessions/{phone_id}").status_code, 404)
        self.assertEqual(pc.post("/api/sessions/close-others").json(), {"closed": 1})
        self.assertEqual(tablet.get("/api/conversations").status_code, 401)
        self.assertEqual(pc.get("/api/conversations").status_code, 200)  # the caller stays in

    def test_needs_a_session(self):
        anonymous = TestClient(self.app, client=("127.0.0.1", 1))
        with anonymous:
            self.assertEqual(anonymous.get("/api/sessions").status_code, 401)
            self.assertEqual(anonymous.post("/api/sessions/close-others", headers={"X-AIHub": "1"}).status_code, 401)

    def test_without_a_password(self):
        app = create_app(str(self.make_tempdir() / "hub.db"), command=FAKE_COMMAND)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/sessions").json(), {"sessions": [], "required": False})


class KeepAwakeTests(unittest.TestCase):
    def test_holds_sleep_back_only_while_busy(self):
        calls, busy = [], threading.Event()
        awake = eveil.KeepAwake(busy.is_set, period_s=0.02, set_state=calls.append)
        awake.start()
        try:
            time.sleep(0.1)
            self.assertEqual(calls, [])  # idle: nothing asked of Windows
            busy.set()
            deadline = time.monotonic() + 5
            while not awake.holding and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(calls, [eveil.ES_CONTINUOUS | eveil.ES_SYSTEM_REQUIRED])  # once, not every check
            busy.clear()
            while awake.holding and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(calls[-1], eveil.ES_CONTINUOUS)
        finally:
            awake.stop()

    def test_released_when_stopped(self):
        calls = []
        awake = eveil.KeepAwake(lambda: True, period_s=0.02, set_state=calls.append)
        awake.start()
        deadline = time.monotonic() + 5
        while not awake.holding and time.monotonic() < deadline:
            time.sleep(0.01)
        awake.stop()
        self.assertEqual(calls[-1], eveil.ES_CONTINUOUS)
        self.assertFalse(awake.holding)

    @unittest.skipIf(os.name != "nt", "Windows only")
    def test_the_real_windows_call(self):
        setter = eveil._windows_setter()
        self.assertNotEqual(setter(eveil.ES_CONTINUOUS), 0)  # 0 would mean the call failed

    def test_a_broken_check_does_not_hold(self):
        calls = []
        awake = eveil.KeepAwake(lambda: 1 / 0, period_s=0.02, set_state=calls.append)
        awake.start()
        time.sleep(0.1)
        awake.stop()
        self.assertEqual(calls, [])

    @unittest.skipIf(TestClient is None, "web dependencies missing")
    def test_wired_to_the_jobs(self):
        calls = []
        app = create_app(str(Path(self.tmp.name) / "hub.db"), command=FAKE_COMMAND, keep_awake=True,
                         awake_setter=calls.append)
        self.assertEqual(app.state.awake.is_busy, app.state.jobs.busy)
        with TestClient(app):
            self.assertTrue(app.state.awake._thread.is_alive())
        self.assertFalse(app.state.awake._thread.is_alive())  # stopped with the server
        self.assertIsNone(create_app(str(Path(self.tmp.name) / "b.db"), command=FAKE_COMMAND).state.awake)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class CliTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.output = []

    def sleeper(self):
        """A live process that is not this one: a server in the eyes of the pid file."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

        def end():
            proc.kill()
            proc.wait()
        self.addCleanup(end)
        return proc

    def out(self, text="", end="\n"):
        self.output.append(text + end)

    def text(self):
        return "".join(self.output)

    def run_cli(self, *argv, answers=()):
        answers = list(answers)
        return cli.main(["--db", self.db_path, *argv], out=self.out, command=FAKE_COMMAND,
                        ask=lambda prompt: answers.pop(0))


class RunningServerTests(CliTestCase):
    def test_pid_file(self):
        self.assertIsNone(cli.running_server(self.db_path))
        sleeper = self.sleeper()
        cli.pid_file(self.db_path).write_text(json.dumps({"pid": sleeper.pid, "port": 8770}), encoding="utf-8")
        self.assertEqual(cli.running_server(self.db_path), (sleeper.pid, 8770))
        sleeper.kill()
        sleeper.wait()  # the cleanup's own kill and wait then do nothing
        self.assertIsNone(cli.running_server(self.db_path))  # a stale file is ignored
        cli.pid_file(self.db_path).write_text("pas du json", encoding="utf-8")
        self.assertIsNone(cli.running_server(self.db_path))

    def test_serve_refuses_a_second_server_and_cleans_its_file(self):
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            self.skipTest("web dependencies missing")
        seen = {}
        with mock.patch("uvicorn.run", side_effect=lambda *a, **k: seen.update(
                marker=json.loads(cli.pid_file(self.db_path).read_text(encoding="utf-8")))):
            self.assertEqual(self.run_cli("serve", "--sans-mot-de-passe", "--sans-sauvegarde"), 0)
        self.assertEqual(seen["marker"], {"pid": os.getpid(), "port": 8770})
        self.assertFalse(cli.pid_file(self.db_path).exists())
        sleeper = self.sleeper()
        cli.pid_file(self.db_path).write_text(json.dumps({"pid": sleeper.pid, "port": 8770}), encoding="utf-8")
        with mock.patch("uvicorn.run") as run:
            self.assertEqual(self.run_cli("serve", "--sans-mot-de-passe"), 2)
        run.assert_not_called()
        self.assertIn("tourne déjà", self.text())

    def test_journal(self):
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            self.skipTest("web dependencies missing")
        journal = self.tmp / "serveur.log"
        before = sys.stdout
        with mock.patch("uvicorn.run") as run:
            cli.main(["--db", self.db_path, "serve", "--sans-mot-de-passe", "--journal", str(journal)],
                     command=FAKE_COMMAND)
        self.assertIs(sys.stdout, before)  # given back
        self.assertIsNotNone(run.call_args.args[0].state.awake)  # sleep held back by default
        with mock.patch("uvicorn.run") as run:
            self.run_cli("serve", "--sans-mot-de-passe", "--laisser-dormir")
        self.assertIsNone(run.call_args.args[0].state.awake)
        text = journal.read_text(encoding="utf-8")
        self.assertIn(": démarrage", text)
        self.assertIn("Interface : http://127.0.0.1:8770/", text)
        self.assertIn("Mise en veille retenue", text)


@unittest.skipIf(os.name != "nt", "Windows only")
class StartupTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.folder = self.make_tempdir()
        patcher = mock.patch.dict(os.environ, {"AIHUB_DEMARRAGE_DOSSIER": str(self.folder)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_activate_state_deactivate(self):
        self.assertEqual(self.run_cli("demarrage-auto", "etat"), 0)
        self.assertIn("Démarrage automatique : désactivé", self.text())
        self.assertEqual(self.run_cli("demarrage-auto", "activer", "--", "--obsidian", "D:\\coffre avec espace"), 0)
        shortcut = self.folder / cli.SHORTCUT_NAME
        self.assertTrue(shortcut.exists())
        self.output.clear()
        self.run_cli("demarrage-auto", "etat")
        text = self.text()
        self.assertIn("Démarrage automatique : activé", text)
        self.assertIn("pythonw.exe", text.lower())
        self.assertIn('serve --journal', text)
        self.assertIn('--obsidian "D:\\coffre avec espace"', text)  # quoted as one argument
        self.assertIn("Serveur : arrêté", text)
        self.assertEqual(self.run_cli("demarrage-auto", "desactiver"), 0)
        self.assertFalse(shortcut.exists())
        self.output.clear()
        self.run_cli("demarrage-auto", "desactiver")
        self.assertIn("n'était pas activé", self.text())


class RestoreTests(CliTestCase):
    def setUp(self):
        super().setUp()
        conn = db.connect(self.db_path)
        store.create_conversation(conn, "ancienne", workdir=str(self.tmp))
        conn.close()
        self.folder = self.tmp / "sauvegardes"
        backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 1))
        conn = db.connect(self.db_path)
        store.create_conversation(conn, "nouvelle", workdir=str(self.tmp))
        conn.close()

    def titles(self):
        conn = db.connect(self.db_path)
        try:
            return sorted(c["title"] for c in store.list_conversations(conn))
        finally:
            conn.close()

    def test_list(self):
        self.assertEqual(self.run_cli("restaurer"), 0)
        self.assertIn("aihub-2026-10-01.db", self.text())

    def test_restore_after_typed_confirmation(self):
        self.assertEqual(self.run_cli("restaurer", "aihub-2026-10-01.db", answers=["non"]), 2)
        self.assertEqual(self.titles(), ["ancienne", "nouvelle"])
        self.assertEqual(self.run_cli("restaurer", "aihub-2026-10-01.db", answers=["oui"]), 0)
        self.assertEqual(self.titles(), ["ancienne"])
        kept = list(self.tmp.glob("avant-restauration-*.db"))
        self.assertEqual(len(kept), 1)
        old = sqlite3.connect(kept[0])
        try:
            self.assertEqual(sorted(r[0] for r in old.execute("SELECT title FROM conversations")),
                             ["ancienne", "nouvelle"])  # nothing lost for good
        finally:
            old.close()

    def test_refusals(self):
        self.assertEqual(self.run_cli("restaurer", "absente.db"), 1)
        self.assertIn("introuvable", self.text())
        broken = self.folder / "aihub-2026-10-02.db"
        broken.write_bytes(b"pas une base" * 200)
        self.assertEqual(self.run_cli("restaurer", str(broken), answers=["oui"]), 1)
        self.assertIn("illisible", self.text())
        sleeper = self.sleeper()
        cli.pid_file(self.db_path).write_text(json.dumps({"pid": sleeper.pid, "port": 8770}), encoding="utf-8")
        self.assertEqual(self.run_cli("restaurer", "aihub-2026-10-01.db", answers=["oui"]), 2)
        self.assertIn("arrêtez-le", self.text())
        self.assertEqual(self.titles(), ["ancienne", "nouvelle"])


if __name__ == "__main__":
    unittest.main()
