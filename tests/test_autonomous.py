"""Autonomous work: rounds, the check, the limits, the stop."""

import json
import sys
import threading
import unittest
from unittest import mock

from aihub import autonomous, db, store
from aihub.autonomous import AutonomousRun, CheckConfirmationRequired
from aihub.store import StoreError

from .helpers import FAKE_COMMAND, TempDirMixin, fake_env, wait_dead


class StateTest(unittest.TestCase):
    def test_the_last_state_line_counts(self):
        self.assertEqual(autonomous.agent_state("fait.\nÉTAT : FINI"), "fini")
        self.assertEqual(autonomous.agent_state("**État : à suivre**"), "suivre")
        self.assertEqual(autonomous.agent_state("ETAT: A SUIVRE ... puis ÉTAT : FINI"), "fini")
        self.assertEqual(autonomous.agent_state("ÉTAT : FINI\nnon, ÉTAT : À SUIVRE"), "suivre")
        self.assertEqual(autonomous.agent_state("rien"), "inconnu")
        self.assertEqual(autonomous.agent_state(None), "inconnu")

    def test_limits(self):
        for rounds, minutes in ((0, 5), (51, 5), (5, 0), (5, 1441), ("5", 5)):
            with self.subTest(rounds=rounds, minutes=minutes), self.assertRaises(StoreError):
                autonomous.check_limits(rounds, minutes, None, False)
        with self.assertRaises(CheckConfirmationRequired):
            autonomous.check_limits(5, 5, "pytest", False)
        with self.assertRaisesRegex(StoreError, "une seule ligne"):
            autonomous.check_limits(5, 5, "a\nb", True)
        self.assertIsNone(autonomous.check_limits(5, 5, "  ", False))
        self.assertEqual(autonomous.check_limits(5, 5, " pytest ", True), "pytest")


class RunTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.workdir = self.make_tempdir()
        self.logs = self.make_tempdir()
        self.conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        self.env = fake_env("auto", self.tmp / "log.json")
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.logs)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Chantier", agent_id=self.agent, workdir=str(self.workdir),
                                             permission_mode="bypass", confirm_bypass=True)

    def work(self, goal="écris les fichiers des tours", **kwargs):
        events = []
        run = AutonomousRun(self.conn, self.cid, self.agent, goal, command=FAKE_COMMAND, env=self.env,
                            on_event=events.append, **kwargs)
        return run, run.run(), events

    def rounds_sent(self):
        """The prompt of each round, in order."""
        runs = [json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json")]
        runs = [r for r in runs if r.get("scenario") == "autonome"]
        runs.sort(key=lambda r: r["round"])
        return [r["user_message"]["message"]["content"] for r in runs]

    def check_script(self, needs):
        """A check that passes when the file `needs` exists in the working folder."""
        script = self.tmp / "verif.py"
        script.write_text(f"import os, sys\nprint('cherche {needs}')\n"
                          f"sys.exit(0 if os.path.exists({needs!r}) else 3)\n", encoding="utf-8")
        return f'"{sys.executable}" "{script}"'


class RunTest(RunTestCase):
    def test_until_the_agent_says_it_is_done(self):
        run, row, events = self.work()
        self.assertEqual((row["status"], row["rounds"]), ("done", 2))
        messages = store.list_messages(self.conn, self.cid)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "assistant"])
        self.assertEqual(messages[0]["content"], "écris les fichiers des tours")  # the goal, as the user's message
        first, second = self.rounds_sent()
        self.assertTrue(first.startswith("écris les fichiers des tours\n\n<mode_autonome>"))
        self.assertIn("vérifie toi-même", first)
        self.assertIn("Tour 2 sur 5", second)
        self.assertEqual([e["type"] for e in events], ["round_started", "round_done"] * 2)
        rounds = autonomous.list_runs(self.conn, self.cid)[0]["round_list"]
        self.assertEqual([(r["round"], r["agent_state"], r["check_code"]) for r in rounds],
                         [(1, "suivre", None), (2, "fini", None)])

    def test_the_check_decides_too(self):
        # The agent says « fini » at round 2, but the check wants round 3's file.
        run, row, _ = self.work(check_command=self.check_script("tour-3.txt"), confirm_check=True)
        self.assertEqual((row["status"], row["rounds"]), ("done", 3))
        third = self.rounds_sent()[2]
        self.assertIn("Vérification en échec (code 3)", third)
        self.assertIn("cherche tour-3.txt", third)
        self.assertIn("la vérification échoue : corrige", third)
        rounds = autonomous.list_runs(self.conn, self.cid)[0]["round_list"]
        self.assertEqual([r["check_code"] for r in rounds], [3, 3, 0])

    def test_rounds_limit(self):
        self.env["FAKE_AUTONOMOUS_DONE_AT"] = "99"
        run, row, _ = self.work(max_rounds=3)
        self.assertEqual((row["status"], row["rounds"]), ("limit", 3))

    def test_time_limit(self):
        now = [0.0]

        def clock():
            now[0] += 40  # each look at the clock: 40 s later
            return now[0]
        self.env["FAKE_AUTONOMOUS_DONE_AT"] = "99"
        run, row, _ = self.work(max_minutes=1, clock=clock)
        self.assertEqual(row["status"], "limit")
        self.assertLess(row["rounds"], 5)

    def test_a_failed_round_ends_the_run(self):
        self.env["FAKE_AUTONOMOUS_FAIL_AT"] = "2"
        self.env["FAKE_AUTONOMOUS_DONE_AT"] = "99"
        run, row, _ = self.work()
        self.assertEqual((row["status"], row["rounds"]), ("error", 2))
        self.assertIn("Quota", row["error"])

    def test_a_reply_without_its_state(self):
        self.env["FAKE_AUTONOMOUS_SILENT"] = "1"
        run, row, _ = self.work(max_rounds=2)
        self.assertEqual(row["status"], "limit")
        self.assertIn("ne finissait pas par", self.rounds_sent()[1])

    def test_stop_in_the_middle(self):
        self.env["FAKE_AUTONOMOUS_SLOW"] = "30"
        started = threading.Event()
        box = {}
        # The connection comes in the worker thread (sqlite3 connections belong to one thread).
        run = AutonomousRun(None, self.cid, self.agent, "long", command=FAKE_COMMAND, env=self.env,
                            on_event=lambda e: started.set())
        thread = threading.Thread(target=lambda: box.setdefault("row", self._run_in_thread(run)))
        thread.start()
        self.assertTrue(started.wait(20))
        run.stop()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["row"]["status"], "stopped")

    def test_stop_during_the_check(self):
        script = self.tmp / "lent.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        checking = threading.Event()
        run = AutonomousRun(None, self.cid, self.agent, "long", command=FAKE_COMMAND, env=self.env,
                            check_command=f'"{sys.executable}" "{script}"', confirm_check=True,
                            on_event=lambda e: e["type"] == "check_started" and checking.set())
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("row", self._run_in_thread(run)))
        thread.start()
        self.assertTrue(checking.wait(20))
        run.stop()
        thread.join(30)
        self.assertEqual((box["row"]["status"], box["row"]["rounds"]), ("stopped", 1))
        # No second round started for nothing (it would leave an empty stopped reply).
        self.assertEqual([m["role"] for m in store.list_messages(self.conn, self.cid)], ["user", "assistant"])

    def _run_in_thread(self, run):
        run.conn = db.connect(str(self.tmp / "hub.db"))
        try:
            return run.run()
        finally:
            run.conn.close()

    def test_a_check_that_hangs_is_cut(self):
        script = self.tmp / "long.py"
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        with mock.patch.object(autonomous, "CHECK_TIMEOUT_S", 2):
            run, row, _ = self.work(max_rounds=1, check_command=f'"{sys.executable}" "{script}"',
                                    confirm_check=True)
        rounds = autonomous.list_runs(self.conn, self.cid)[0]["round_list"]
        self.assertEqual(rounds[0]["check_code"], 124)
        self.assertIn("arrêtée après", rounds[0]["check_output"])

    def test_refusals_before_anything(self):
        with self.assertRaises(CheckConfirmationRequired):
            self.work(check_command="pytest")
        with self.assertRaisesRegex(StoreError, "objectif vide"):
            self.work(goal="  ")
        self.assertEqual(store.list_messages(self.conn, self.cid), [])
        self.assertEqual(autonomous.list_runs(self.conn, self.cid), [])

    def test_recover_after_a_stopped_server(self):
        from aihub.web.jobs import JobManager
        self.conn.execute("INSERT INTO autonomous_runs (conversation_id, max_rounds, max_minutes, status, started_at) "
                          "VALUES (?, 3, 10, 'running', 'x')", (self.cid,))
        JobManager(str(self.tmp / "hub.db"))  # the server starts again
        self.assertEqual(autonomous.list_runs(self.conn, self.cid)[0]["status"], "stopped")
        self.assertEqual(autonomous.recover(self.conn), 0)

    def test_deleting_the_conversation_deletes_its_runs(self):
        self.work(max_rounds=1)
        store.delete_conversation(self.conn, self.cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM autonomous_runs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM autonomous_rounds").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
