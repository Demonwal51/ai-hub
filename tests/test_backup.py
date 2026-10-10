import sqlite3
import threading
import time
import unittest
from datetime import date, timedelta
from unittest import mock

from aihub import backup, db, store
from tests.helpers import TempDirMixin

try:
    from fastapi.testclient import TestClient
    from aihub.web.app import create_app
    from tests.helpers import FAKE_COMMAND
except ImportError:  # web dependencies missing
    TestClient = None

SECRET = "contenu-tres-secret-7f3a"  # a marker searched for in the raw bytes


def wait_for_backup(folder, timeout=20):
    deadline = time.monotonic() + timeout
    while not backup.backups(folder):
        if time.monotonic() > deadline:
            raise AssertionError("pas de sauvegarde au démarrage")
        time.sleep(0.05)


class BackupTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.folder = tmp / "sauvegardes"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Secrète", agent_id=agent, workdir=str(tmp))
        store.add_user_message(self.conn, self.cid, SECRET)
        self.other = store.create_conversation(self.conn, "Autre", agent_id=agent, workdir=str(tmp))

    def test_one_copy_per_day(self):
        day = date(2026, 10, 9)
        made = backup.backup_now(self.db_path, self.folder, today=day)
        self.assertEqual(made.name, "aihub-2026-10-09.db")
        self.assertIsNone(backup.backup_now(self.db_path, self.folder, today=day))
        copy = sqlite3.connect(made)
        try:
            titles = {r[0] for r in copy.execute("SELECT title FROM conversations")}
            self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            copy.close()
        self.assertEqual(titles, {"Secrète", "Autre"})
        self.assertEqual(list(self.folder.glob("*.partiel")), [])

    def test_only_the_last_ones_are_kept(self):
        for offset in range(5):
            backup.backup_now(self.db_path, self.folder, keep=3, today=date(2026, 10, 1) + timedelta(days=offset))
        self.assertEqual([p.name for p in backup.backups(self.folder)],
                         ["aihub-2026-10-03.db", "aihub-2026-10-04.db", "aihub-2026-10-05.db"])
        (self.folder / "a moi.db").write_bytes(b"x")  # not a backup: never deleted
        backup.backup_now(self.db_path, self.folder, keep=3, today=date(2026, 10, 6))
        self.assertTrue((self.folder / "a moi.db").exists())

    def test_purge_removes_the_conversation_from_every_copy(self):
        for offset in range(2):
            backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 1) + timedelta(days=offset))
        self.assertTrue(all(SECRET.encode() in p.read_bytes() for p in backup.backups(self.folder)))
        self.assertEqual(backup.purge_conversation(self.folder, self.cid), {})
        for path in backup.backups(self.folder):
            raw = path.read_bytes()
            self.assertNotIn(SECRET.encode(), raw)  # overwritten, not only unlinked
            copy = sqlite3.connect(path)
            try:
                self.assertEqual([r[0] for r in copy.execute("SELECT title FROM conversations")], ["Autre"])
                self.assertEqual(copy.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            finally:
                copy.close()

    def test_purge_waits_for_a_copy_in_progress(self):
        # A copy started before a deletion and finished after its purge would
        # keep the conversation: the purge must wait for the copy.
        done = threading.Event()
        with backup._LOCK:  # a copy is being written
            worker = threading.Thread(target=lambda: (backup.purge_conversation(self.folder, self.cid),
                                                      done.set()))
            worker.start()
            self.assertFalse(done.wait(0.3))  # a negative property: it can only be waited out
        self.assertTrue(done.wait(10))
        worker.join()

    def test_unreadable_copy_is_reported(self):
        backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 1))
        (self.folder / "aihub-2026-10-02.db").write_bytes(b"pas une base sqlite" * 100)
        errors = backup.purge_conversation(self.folder, self.cid)
        self.assertEqual(list(errors), ["aihub-2026-10-02.db"])
        self.assertNotIn(SECRET.encode(), (self.folder / "aihub-2026-10-01.db").read_bytes())

    def test_failed_copy_leaves_nothing_behind(self):
        # The copy is written, then its final rename fails: the partial file must go.
        with mock.patch("aihub.backup.os.replace", side_effect=OSError("disque plein")):
            with self.assertRaisesRegex(backup.BackupError, "disque plein"):
                backup.backup_now(self.db_path, self.folder, today=date(2026, 10, 1))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_daily_backup_runs_at_start(self):
        daily = backup.DailyBackup(self.db_path, self.folder)
        daily.start()
        self.addCleanup(daily.stop)
        wait_for_backup(self.folder)
        daily.stop()
        self.assertFalse(daily._thread.is_alive())

    def test_daily_backup_reports_errors(self):
        seen = []
        daily = backup.DailyBackup(self.db_path, self.folder, on_error=seen.append)
        with mock.patch.object(backup, "backup_now", side_effect=backup.BackupError("disque plein")):
            self.assertIsNone(daily.run_once())
        self.assertEqual((seen, daily.last_error), (["disque plein"], "disque plein"))


@unittest.skipIf(TestClient is None, "web dependencies missing")
class DeleteFromBackupsTests(TempDirMixin, unittest.TestCase):
    def test_deleting_a_conversation_purges_the_backups(self):
        tmp = self.make_tempdir()
        folder = tmp / "sauvegardes"
        app = create_app(str(tmp / "hub.db"), command=FAKE_COMMAND, backup_dir=str(folder))
        with TestClient(app) as client:
            r = client.post("/api/conversations", json={"title": SECRET, "workdir": str(tmp)})
            cid = r.json()["conversation"]["id"]
            backup.backup_now(str(tmp / "hub.db"), folder, today=date(2020, 1, 1))
            old = folder / "aihub-2020-01-01.db"
            self.assertIn(SECRET.encode(), old.read_bytes())
            r = client.delete(f"/api/conversations/{cid}")
            self.assertEqual(r.json(), {"deleted": cid, "backup_errors": {}})
            self.assertNotIn(SECRET.encode(), old.read_bytes())

    def test_server_backs_up_when_it_starts(self):
        tmp = self.make_tempdir()
        folder = tmp / "sauvegardes"
        app = create_app(str(tmp / "hub.db"), command=FAKE_COMMAND, backup_dir=str(folder))
        with TestClient(app):
            wait_for_backup(folder)
        self.assertFalse(app.state.backup._thread.is_alive())  # stopped with the server


if __name__ == "__main__":
    unittest.main()
