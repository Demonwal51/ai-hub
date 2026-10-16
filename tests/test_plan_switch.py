"""1.1.1: « Passer la suite sur … » — the rest of a validated, running or interrupted plan on one AI."""

import json
import sqlite3
import unittest

from aihub import db, store
from tests.helpers import FAKE_CODEX

try:
    from tests.test_web import WebTestCase, wait_until
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = None
    H = {}


if WebTestCase is not None:
    class PlanSwitchTests(WebTestCase):
        scenario = "auto"
        app_kwargs = {"codex_command": FAKE_CODEX}

        def setUp(self):
            super().setUp()
            self.env["FAKE_CODEX_MODELS"] = "gpt-5.6-sol,gpt-5.6-luna"
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())
            from aihub import routing
            self.addCleanup(routing.set_available, {"claude_code": None})

        def orchestration(self):
            return self.new_conversation(title="Site", mode="orchestration", conductor_model="opus", assignment_mode="auto")

        def plan(self, cid):
            return self.conversation(cid)["plans"][-1]

        def run_plan(self, cid):
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.wait_idle(cid)
            return plan["id"]

        def interrupt(self, pid):
            conn = db.connect(self.db_path)
            try:
                with conn:
                    conn.execute("UPDATE subtasks SET status = 'error', error = 'plus de quota', result = NULL "
                                 "WHERE plan_id = ? AND position = 2", (pid,))
                    conn.execute("UPDATE subtasks SET status = 'stopped', result = NULL WHERE plan_id = ? AND position = 3", (pid,))
                    conn.execute("UPDATE plans SET status = 'error' WHERE id = ?", (pid,))
            finally:
                conn.close()

        def test_an_interrupted_plan_goes_on_another_ai_conductor_included(self):
            cid = self.orchestration()
            pid = self.run_plan(cid)
            self.interrupt(pid)
            r = self.client.post(f"/api/plans/{pid}/switch", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["moved"], r.json()["resumed"]), ([2, 3], True))
            data = self.wait_idle(cid)
            plan = data["plans"][-1]
            self.assertEqual(plan["status"], "done")
            self.assertEqual([s["provider"] for s in plan["subtasks"]], ["claude_code", "codex", "codex"])
            synthesis = next(m for m in data["messages"] if m["id"] == plan["synthesis_message_id"])
            self.assertEqual(synthesis["provider"], "codex")  # the conductor too: it wrote the synthesis
            self.assertEqual(data["conversation"]["conductor_agent_id"], plan["subtasks"][1]["agent_id"])
            self.assertEqual(synthesis["model"], "gpt-5.6-luna")

        def test_the_conductor_kept_when_asked(self):
            cid = self.orchestration()
            pid = self.run_plan(cid)
            self.interrupt(pid)
            before = self.conversation(cid)["conversation"]["conductor_agent_id"]
            self.client.post(f"/api/plans/{pid}/switch", json={"model": "gpt-5.6-luna", "conductor": False})
            data = self.wait_idle(cid)
            self.assertEqual(data["conversation"]["conductor_agent_id"], before)
            synthesis = next(m for m in data["messages"] if m["id"] == data["plans"][-1]["synthesis_message_id"])
            self.assertEqual(synthesis["provider"], "claude_code")

        def test_a_running_plan_its_tasks_not_started_move(self):
            self.env["FAKE_PLAN"] = json.dumps({"summary": "essai", "max_parallel": 1, "subtasks": [
                {"title": "Long", "description": "attends longtemps", "kind": "code", "depends_on": [], "files": ["l.txt"]},
                {"title": "Suite", "description": "la suite", "kind": "code", "depends_on": [1], "files": ["s.txt"]}]})
            cid = self.orchestration()
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            wait_until(lambda: self.plan(cid)["subtasks"][0]["status"] == "running")
            r = self.client.post(f"/api/plans/{plan['id']}/switch", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["moved"], r.json()["resumed"]), ([2], False))
            now = self.plan(cid)
            self.assertEqual([(s["status"], s["provider"]) for s in now["subtasks"]],
                             [("running", "claude_code"), ("pending", "codex")])  # the running one keeps its AI
            self.client.post(f"/api/conversations/{cid}/stop")
            self.wait_idle(cid)

        def test_switched_while_running_the_new_conductor_writes_the_synthesis(self):
            self.env["FAKE_PLAN"] = json.dumps({"summary": "essai", "max_parallel": 1, "subtasks": [
                {"title": "Un", "description": "lent un", "kind": "code", "depends_on": [], "files": ["1.txt"]},
                {"title": "Deux", "description": "deux", "kind": "code", "depends_on": [1], "files": ["2.txt"]}]})
            cid = self.orchestration()
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            wait_until(lambda: self.plan(cid)["subtasks"][0]["status"] == "running")
            r = self.client.post(f"/api/plans/{plan['id']}/switch", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.json()["moved"], [2])
            data = self.wait_idle(cid)
            done = data["plans"][-1]
            self.assertEqual(done["status"], "done")
            self.assertEqual([s["provider"] for s in done["subtasks"]], ["claude_code", "codex"])
            synthesis = next(m for m in data["messages"] if m["id"] == done["synthesis_message_id"])
            # The conductor changed while the tasks ran: the synthesis is the new one's.
            self.assertEqual(synthesis["provider"], "codex")

        def test_refusals(self):
            cid = self.orchestration()
            self.send(cid, "fais le site")
            proposed = self.wait_idle(cid)["plans"][-1]
            r = self.client.post(f"/api/plans/{proposed['id']}/switch", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 400)  # a proposed plan is edited as usual
            self.client.post(f"/api/plans/{proposed['id']}/launch")
            self.wait_idle(cid)
            r = self.client.post(f"/api/plans/{proposed['id']}/switch", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 400)  # done: nothing left to do
            self.interrupt(proposed["id"])
            r = self.client.post(f"/api/plans/{proposed['id']}/switch", json={"model": "gpt-6-astra"})
            self.assertEqual(r.status_code, 400)  # not offered here
            self.assertEqual(self.client.post("/api/plans/999/switch", json={"model": "sonnet"}).status_code, 404)

        def test_the_frozen_plan_rule_still_holds_for_what_runs_or_is_done(self):
            cid = self.orchestration()
            pid = self.run_plan(cid)
            conn = db.connect(self.db_path)
            try:
                other = store.ensure_agent(conn, "codex", "gpt-5.6-luna")
                with conn:
                    conn.execute("UPDATE plans SET status = 'running' WHERE id = ?", (pid,))
                    conn.execute("UPDATE subtasks SET status = 'running' WHERE plan_id = ? AND position = 2", (pid,))
                    conn.execute("UPDATE subtasks SET status = 'pending' WHERE plan_id = ? AND position = 3", (pid,))
                for position in (1, 2):  # done, running
                    with self.subTest(position=position), self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):
                        with conn:
                            conn.execute("UPDATE subtasks SET agent_id = ? WHERE plan_id = ? AND position = ?", (other, pid, position))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):  # a pending task's content
                    with conn:
                        conn.execute("UPDATE subtasks SET description = 'autre' WHERE plan_id = ? AND position = 3", (pid,))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):  # its content along with a new AI
                    with conn:
                        conn.execute("UPDATE subtasks SET agent_id = ?, description = 'autre' WHERE plan_id = ? AND position = 3",
                                     (other, pid))
                with conn:  # a pending task's AI: the opening
                    conn.execute("UPDATE subtasks SET agent_id = ?, provider = 'codex', model = 'gpt-5.6-luna' "
                                 "WHERE plan_id = ? AND position = 3", (other, pid))
            finally:
                conn.close()


try:
    from tests.test_roles import RolesTestCase
except unittest.SkipTest:
    RolesTestCase = None

if RolesTestCase is not None:
    class PlanSwitchRightsTests(RolesTestCase):
        def test_reading_is_not_changing_the_ai(self):
            cid = self.conversation("Site", mode="orchestration", conductor_model="opus")
            conn = db.connect(self.db_path)
            try:
                agent = store.ensure_agent(conn, "claude_code", "opus")
                mid = store.add_user_message(conn, cid, "fais le site", None)
                pid = store.create_plan(conn, cid, mid, [{"description": "la page", "agent_id": agent}])
                store.validate_plan(conn, pid)
                with conn:
                    conn.execute("UPDATE subtasks SET status = 'error', error = 'plus de quota' WHERE plan_id = ?", (pid,))
                    conn.execute("UPDATE plans SET status = 'error' WHERE id = ?", (pid,))
            finally:
                conn.close()
            sam, uid = self.person("Sam", grants=[{"conversation_id": cid, "role": "read"}])
            r = sam.post(f"/api/plans/{pid}/switch", json={"model": "sonnet"}, headers=H)
            self.assertEqual(r.status_code, 403)
            conn = db.connect(self.db_path)
            model = conn.execute("SELECT model FROM subtasks WHERE plan_id = ?", (pid,)).fetchone()[0]
            conn.close()
            self.assertEqual(model, "opus")


if __name__ == "__main__":
    unittest.main()
