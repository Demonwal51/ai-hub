"""1.1: restoring a backup from the app. The helper waits for the server, restores, restarts, checks."""

import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from aihub import backup, db, restore, store
from tests.helpers import TempDirMixin

try:
    from tests.test_sharing import H, SharingTestCase
except unittest.SkipTest:  # web dependencies missing
    SharingTestCase = None


def titles(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM conversations ORDER BY id")]
    finally:
        conn.close()


class HelperTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        root = self.make_tempdir()
        (root / "data").mkdir()
        self.db_path = root / "data" / "aihub.db"
        conn = db.connect(str(self.db_path))
        store.create_conversation(conn, "Avant")
        conn.close()
        self.copy = backup.backup_now(str(self.db_path), root / "data" / "sauvegardes", today=date(2026, 10, 1))
        conn = db.connect(str(self.db_path))
        store.create_conversation(conn, "Après")
        conn.close()

    def test_the_backup_back_the_former_base_kept_the_report_written(self):
        report = restore.run(self.copy, self.db_path)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(titles(self.db_path), ["Avant"])
        self.assertEqual(titles(report["keep"]), ["Avant", "Après"])
        self.assertEqual(restore.last_report(self.db_path)["status"], "ok")
        self.assertEqual(restore.last_report(self.db_path)["backup"], self.copy.name)

    def test_an_unreadable_backup_changes_nothing(self):
        bad = self.copy.with_name("aihub-2026-10-02.db")
        bad.write_bytes(b"ceci n'est pas une base SQLite" * 10)
        report = restore.run(bad, self.db_path)
        self.assertEqual(report["status"], "echec")
        self.assertEqual(titles(self.db_path), ["Avant", "Après"])
        self.assertEqual(restore.last_report(self.db_path)["status"], "echec")

    def test_a_server_that_does_not_stop_means_no_restore(self):
        report = restore.run(self.copy, self.db_path, pid=12345, wait_s=0.2, alive=lambda pid: True)
        self.assertEqual(report["status"], "echec")
        self.assertIn("ne s'est pas arrêté", report["error"])
        self.assertEqual(titles(self.db_path), ["Avant", "Après"])

    def test_restarted_and_checked_or_the_former_base_back(self):
        started = []

        class Process:
            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        def popen(command, **options):
            started.append((command, options.get("env", {}).get("AIHUB_UPDATE_PROBE")))
            return Process()

        restart = ["python", "-m", "aihub", "--db", str(self.db_path), "serve"]
        with mock.patch("aihub.providers.claude_code._kill_tree"):
            report = restore.run(self.copy, self.db_path, root=self.db_path.parent.parent, restart=restart,
                                 popen=popen, health=lambda process, url, version, probe: False)
        self.assertEqual(report["status"], "retabli")
        self.assertEqual(titles(self.db_path), ["Avant", "Après"])  # the base from before
        self.assertEqual(len(started), 2)  # the try, then the server on the former base
        self.assertIsNotNone(started[0][1])  # the first start is probed
        started.clear()
        report = restore.run(self.copy, self.db_path, root=self.db_path.parent.parent, restart=restart,
                             popen=popen, health=lambda process, url, version, probe: True)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(titles(self.db_path), ["Avant"])
        self.assertEqual(len(started), 1)


if SharingTestCase is not None:
    class RestoreWebTests(SharingTestCase):
        def setUp(self):
            super().setUp()
            self.started, self.stopped = [], []
            self.app.state.restore_hooks = {"spawn": self.started.append, "stop": lambda: self.stopped.append(True),
                                            "restart": ["python", "-m", "aihub", "serve"]}
            self.conversation("Gardée")
            self.folder = Path(self.db_path).resolve().parent / "sauvegardes"
            self.copy = backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 3))

        def test_the_owner_sees_the_backups_and_restores_one(self):
            listed = self.owner.get("/api/server/backups", headers=H).json()
            self.assertEqual([b["name"] for b in listed["backups"]], ["aihub-2026-10-03.db"])
            r = self.owner.post("/api/server/restore", json={"name": "aihub-2026-10-03.db", "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 202, r.text)
            self.assertEqual(r.json()["conversations"], 1)
            command = self.started[0]
            self.assertEqual(command[1:3], ["-m", "aihub.restore"])
            self.assertEqual(Path(command[command.index("--backup") + 1]), self.copy)
            self.assertEqual(json.loads(command[command.index("--restart") + 1]), ["python", "-m", "aihub", "serve"])
            self.assertEqual(self.stopped, [True])

        def test_refusals(self):
            r = self.owner.post("/api/server/restore", json={"name": "aihub-2026-10-03.db", "password": "faux"}, headers=H)
            self.assertEqual(r.status_code, 401)
            for name in ("../aihub.db", str(self.copy), "absente.db"):
                with self.subTest(name=name):
                    r = self.owner.post("/api/server/restore", json={"name": name, "password": self.password}, headers=H)
                    self.assertEqual(r.status_code, 404)
            guest, uid = self.guest("Lou")
            self.owner.patch(f"/api/users/{uid}", json={"role": "admin"}, headers=H)
            self.assertEqual(guest.get("/api/server/backups", headers=H).status_code, 403)
            r = guest.post("/api/server/restore", json={"name": "aihub-2026-10-03.db", "password": "un bon mot de passe"}, headers=H)
            self.assertEqual(r.status_code, 403)
            # Refused before any password is tried: an admin cannot test passwords here.
            r = guest.post("/api/server/restore", json={"name": "aihub-2026-10-03.db", "password": "pas le bon"}, headers=H)
            self.assertEqual(r.status_code, 403)
            self.assertEqual((self.started, self.stopped), ([], []))

        def test_an_unreadable_or_newer_backup_is_refused_before_anything(self):
            self.copy.write_bytes(b"pas une base" * 50)
            r = self.owner.post("/api/server/restore", json={"name": self.copy.name, "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("illisible", r.json()["detail"])
            newer = backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 4))
            conn = sqlite3.connect(newer)
            conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
            conn.close()
            r = self.owner.post("/api/server/restore", json={"name": newer.name, "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("plus récente", r.json()["detail"])
            self.assertEqual((self.started, self.stopped), ([], []))

        def test_a_backup_sqlite_says_damaged_is_refused(self):
            # A finer damage than an unreadable file: SQLite opens it, and quick_check says what is wrong.
            real = sqlite3.connect

            class Reported:
                def __init__(self, conn):
                    self.conn = conn

                def execute(self, sql, *args):
                    if sql.startswith("PRAGMA quick_check"):
                        return real(":memory:").execute("SELECT '*** in database main *** Page 3: btreeInitPage() returns error code 11'")
                    return self.conn.execute(sql, *args)

                def close(self):
                    self.conn.close()

            def connect(path, *args, **kwargs):
                conn = real(path, *args, **kwargs)
                return Reported(conn) if "mode=ro" in str(path) else conn  # only the backup's check

            with mock.patch.object(sqlite3, "connect", connect):
                r = self.owner.post("/api/server/restore", json={"name": self.copy.name, "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("intégrité", r.json()["detail"])
            self.assertEqual((self.started, self.stopped), ([], []))

        def test_a_server_that_cannot_restart_itself_restores_nothing(self):
            self.app.state.restore_hooks["restart"] = ["python", "tools/serveur_essai.py", "--port", "8766"]
            r = self.owner.post("/api/server/restore", json={"name": self.copy.name, "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("aihub restaurer", r.json()["detail"])
            self.assertEqual((self.started, self.stopped), ([], []))

        def test_not_while_an_ai_works(self):
            with mock.patch.object(self.app.state.jobs, "busy", return_value=True):
                r = self.owner.post("/api/server/restore", json={"name": self.copy.name, "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 409)
            self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
