import os
import sqlite3
import tempfile
import unittest

from aihub import db, store


class MigrationTests(unittest.TestCase):
    def test_new_database_gets_current_schema(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual(
            {"agents", "conversations", "messages", "plans", "subtasks", "subtask_deps"}, tables
        )

    def test_reopening_keeps_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub.db")
            conn = db.connect(path)
            store.create_agent(conn, "Claude", "claude_code", "opus-5.5")
            conn.close()
            conn = db.connect(path)
            self.assertEqual([r["name"] for r in store.list_agents(conn)], ["Claude"])
            conn.close()

    def test_version_1_database_is_upgraded_without_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub.db")
            raw = sqlite3.connect(path, isolation_level=None)
            for statement in db._SCHEMA_V1:
                raw.execute(statement)
            raw.execute("PRAGMA user_version = 1")
            now = db.utc_now()
            raw.execute("INSERT INTO conversations (title, created_at, updated_at) "
                        "VALUES ('ancienne', ?, ?)", (now, now))
            raw.execute("INSERT INTO messages (conversation_id, role, content, status, created_at) "
                        "VALUES (1, 'user', 'gardé', 'ok', ?)", (now,))
            raw.close()

            conn = db.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                (message,) = store.list_messages(conn, 1)
                self.assertEqual((message["content"], message["session_id"]), ("gardé", None))
                store.record_quota(conn, "claude_code", "allowed")
                self.assertEqual(store.latest_quota(conn, "claude_code")["status"], "allowed")
            finally:
                conn.close()

    def test_database_from_newer_version_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub.db")
            db.connect(path).close()
            raw = sqlite3.connect(path)
            raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
            raw.close()
            with self.assertRaises(db.SchemaError):
                db.connect(path)
            # The refusal released the file: it can be opened and removed.
            os.remove(path)

    def test_pragmas(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_error_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                store.create_agent(self.conn, "A", "claude_code", "m")
                raise RuntimeError
        self.assertEqual(store.list_agents(self.conn), [])

    def test_success_commits(self):
        with db.transaction(self.conn):
            store.create_agent(self.conn, "A", "claude_code", "m")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(len(store.list_agents(self.conn)), 1)

    def test_nested_block_joins_outer(self):
        with self.assertRaises(RuntimeError):
            with db.transaction(self.conn):
                with db.transaction(self.conn):
                    store.create_agent(self.conn, "A", "claude_code", "m")
                raise RuntimeError
        self.assertEqual(store.list_agents(self.conn), [])


class DatabaseConstraintTests(unittest.TestCase):
    """The schema refuses invalid states even when the store is bypassed."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = db.utc_now()
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "opus-5.5")

    def _conversation(self, mode="orchestration"):
        return self.conn.execute(
            "INSERT INTO conversations (title, mode, created_at, updated_at) VALUES ('t', ?, ?, ?)",
            (mode, self.now, self.now),
        ).lastrowid

    def test_bypass_needs_confirmation(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO conversations (title, permission_mode, created_at, updated_at) "
                "VALUES ('t', 'bypass', ?, ?)",
                (self.now, self.now),
            )
        self.conn.execute(
            "INSERT INTO conversations (title, permission_mode, bypass_confirmed_at, created_at, "
            "updated_at) VALUES ('t', 'bypass', ?, ?, ?)",
            (self.now, self.now, self.now),
        )

    def test_one_active_request_per_conversation(self):
        cid = self._conversation()
        insert = (
            "INSERT INTO messages (conversation_id, role, status, created_at) "
            "VALUES (?, 'assistant', ?, ?)"
        )
        self.conn.execute(insert, (cid, "running", self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (cid, "pending", self.now))
        # Finished replies do not count, and other conversations are free.
        self.conn.execute(insert, (cid, "ok", self.now))
        self.conn.execute(insert, (self._conversation(), "pending", self.now))

    def test_one_open_plan_per_conversation(self):
        cid = self._conversation()
        insert = "INSERT INTO plans (conversation_id, status, created_at) VALUES (?, ?, ?)"
        self.conn.execute(insert, (cid, "running", self.now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert, (cid, "proposed", self.now))
        self.conn.execute(insert, (cid, "done", self.now))

    def _plan_with_subtask(self, status):
        cid = self._conversation()
        pid = self.conn.execute(
            "INSERT INTO plans (conversation_id, created_at) VALUES (?, ?)", (cid, self.now)
        ).lastrowid
        sid = self.conn.execute(
            "INSERT INTO subtasks (plan_id, position, description, agent_id, provider, model) "
            "VALUES (?, 1, 'a', ?, 'claude_code', 'opus-5.5')",
            (pid, self.agent),
        ).lastrowid
        self.conn.execute("UPDATE plans SET status = ? WHERE id = ?", (status, pid))
        return cid, pid, sid

    def test_frozen_plan_content_cannot_change(self):
        for status in ("validated", "running", "done", "stopped", "error"):
            with self.subTest(status=status):
                cid, pid, sid = self._plan_with_subtask(status)
                for sql, params in (
                    ("UPDATE subtasks SET description = 'b' WHERE id = ?", (sid,)),
                    ("UPDATE subtasks SET model = 'other' WHERE id = ?", (sid,)),
                    ("DELETE FROM subtasks WHERE id = ?", (sid,)),
                    ("INSERT INTO subtasks (plan_id, position, description) VALUES (?, 2, 'c')",
                     (pid,)),
                ):
                    with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                        self.conn.execute(sql, params)
                # Execution fields stay writable.
                self.conn.execute("UPDATE subtasks SET status = 'running' WHERE id = ?", (sid,))
                self.conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))

    def test_proposed_plan_content_can_change(self):
        _, pid, sid = self._plan_with_subtask("proposed")
        self.conn.execute("UPDATE subtasks SET description = 'b' WHERE id = ?", (sid,))
        self.conn.execute(
            "INSERT INTO subtasks (plan_id, position, description) VALUES (?, 2, 'c')", (pid,)
        )
        self.conn.execute("DELETE FROM subtasks WHERE id = ?", (sid,))

    def test_dependency_must_stay_in_plan(self):
        _, _, s1 = self._plan_with_subtask("proposed")
        _, _, s2 = self._plan_with_subtask("proposed")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO subtask_deps VALUES (?, ?)", (s1, s2))

    def test_frozen_plan_dependencies_cannot_change(self):
        cid = self._conversation()
        pid = self.conn.execute(
            "INSERT INTO plans (conversation_id, created_at) VALUES (?, ?)", (cid, self.now)
        ).lastrowid
        s1, s2 = (
            self.conn.execute(
                "INSERT INTO subtasks (plan_id, position, description) VALUES (?, ?, 'x')",
                (pid, pos),
            ).lastrowid
            for pos in (1, 2)
        )
        self.conn.execute("INSERT INTO subtask_deps VALUES (?, ?)", (s2, s1))
        self.conn.execute("UPDATE plans SET status = 'validated' WHERE id = ?", (pid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM subtask_deps WHERE subtask_id = ?", (s2,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO subtask_deps VALUES (?, ?)", (s1, s2))


class DefinitiveDeletionTests(unittest.TestCase):
    MARKER = "marqueur-de-suppression-7f3a91"

    def _files_contain_marker(self, path):
        found = False
        for suffix in ("", "-wal"):
            name = path + suffix
            if os.path.exists(name):
                with open(name, "rb") as fh:
                    found = found or self.MARKER.encode() in fh.read()
        return found

    def test_deleted_conversation_cannot_be_read_back_from_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub.db")
            conn = db.connect(path)
            keep = store.create_conversation(conn, "garder")
            store.add_user_message(conn, keep, "autre contenu")
            gone = store.create_conversation(conn, "supprimer")
            store.add_user_message(conn, gone, self.MARKER)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # The probe must see the marker before deletion, or its "absent"
            # verdict afterwards would prove nothing.
            self.assertTrue(self._files_contain_marker(path))

            store.delete_conversation(conn, gone)

            self.assertFalse(self._files_contain_marker(path))
            self.assertEqual([c["title"] for c in store.list_conversations(conn)], ["garder"])
            conn.close()

    def test_deleted_project_cannot_be_read_back_from_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hub.db")
            conn = db.connect(path)
            store.create_conversation(conn, "garder")
            pid = store.create_project(conn, "Projet")
            gone = store.create_conversation(conn, "dans le projet", project_id=pid)
            store.add_user_message(conn, gone, self.MARKER)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertTrue(self._files_contain_marker(path))  # the probe sees it before

            store.delete_project(conn, pid, with_conversations=True)

            self.assertFalse(self._files_contain_marker(path))  # neither in the base nor in its log
            self.assertEqual([c["title"] for c in store.list_conversations(conn)], ["garder"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
