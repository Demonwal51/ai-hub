"""1.1: a plan a quota stopped is resumed by itself when the quota comes back, or given to another AI."""

import shutil
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from aihub import db, store
from tests.helpers import FAKE_CODEX

try:
    from tests.test_web import WebTestCase
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = None

QUOTA = "Quota de Claude Code (opus) atteint : retour prévu le 16/10 à 16:20."


def later(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="seconds")


if WebTestCase is not None:
    class QuotaPlanTests(WebTestCase):
        scenario = "auto"
        app_kwargs = {"codex_command": FAKE_CODEX}

        def setUp(self):
            super().setUp()
            self.env["FAKE_CODEX_MODELS"] = "gpt-5.6-sol,gpt-5.6-luna"
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())
            # The fake Codex makes its models « available » for the whole process: put back after.
            from aihub import routing
            self.addCleanup(routing.set_available, {"claude_code": None})

        def stopped_by_a_quota(self, resets_at=None):
            """A plan run, then its 2nd task said stopped by Claude's quota, the 3rd never started."""
            cid = self.new_conversation(title="Site", mode="orchestration", conductor_model="opus", assignment_mode="auto")
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.wait_idle(cid)
            conn = db.connect(self.db_path)
            try:
                with conn:
                    conn.execute("UPDATE subtasks SET status = 'error', error = ?, result = NULL WHERE plan_id = ? AND position = 2",
                                 (QUOTA, plan["id"]))
                    conn.execute("UPDATE subtasks SET status = 'stopped', result = NULL WHERE plan_id = ? AND position = 3", (plan["id"],))
                    conn.execute("UPDATE plans SET status = 'error' WHERE id = ?", (plan["id"],))
                    if resets_at:
                        conn.execute("INSERT INTO quota_observations (provider, status, resets_at, observed_at) "
                                     "VALUES ('claude_code', 'rejected', ?, ?)", (resets_at, later()))
            finally:
                conn.close()
            return cid, plan["id"]

        def plan(self, cid):
            return self.conversation(cid)["plans"][-1]

        def test_what_the_quota_stopped_and_when_it_comes_back(self):
            back = later(hours=2)
            cid, pid = self.stopped_by_a_quota(back)
            quota = self.plan(cid)["quota"]
            self.assertEqual(quota["subtasks"], [2])
            self.assertEqual(quota["waiting"], [2, 3])  # the task never started was on the same AI
            self.assertEqual(quota["providers"], ["claude_code"])
            self.assertEqual(quota["resets_at"], back)

        def test_resumed_by_itself_when_the_quota_comes_back(self):
            back = later(hours=2)
            cid, pid = self.stopped_by_a_quota(back)
            r = self.client.post(f"/api/plans/{pid}/auto-resume", json={})
            self.assertEqual(r.status_code, 200, r.text)
            planned = datetime.fromisoformat(r.json()["plan"]["auto_resume_at"])
            self.assertEqual(planned - datetime.fromisoformat(back), store.RESUME_MARGIN)
            self.assertEqual(self.app.state.scheduler.tick(), [])  # not yet
            conn = db.connect(self.db_path)
            with conn:
                conn.execute("UPDATE plans SET auto_resume_at = ? WHERE id = ?", (later(minutes=-1), pid))
            conn.close()
            self.assertEqual(self.app.state.scheduler.tick(), [f"plan-{pid}"])
            plan = self.wait_idle(cid)["plans"][-1]
            self.assertEqual(plan["status"], "done")
            self.assertEqual([s["status"] for s in plan["subtasks"]], ["ok", "ok", "ok"])
            self.assertIsNone(plan["auto_resume_at"])
            self.assertEqual(self.app.state.scheduler.tick(), [])  # once only

        def test_an_unknown_return_needs_a_time_and_a_resume_can_be_cancelled(self):
            cid, pid = self.stopped_by_a_quota()
            self.assertIsNone(self.plan(cid)["quota"]["resets_at"])
            r = self.client.post(f"/api/plans/{pid}/auto-resume", json={})
            self.assertEqual(r.status_code, 400)
            self.assertIn("pas connue", r.json()["detail"])
            soon = (datetime.now(timezone(timedelta(hours=2))) + timedelta(hours=3)).isoformat(timespec="seconds")
            kept = self.client.post(f"/api/plans/{pid}/auto-resume", json={"at": soon}).json()["plan"]["auto_resume_at"]
            self.assertTrue(kept.endswith("+00:00"))  # kept in UTC: due_resumes compares them as text
            self.assertEqual(datetime.fromisoformat(kept), datetime.fromisoformat(soon))
            for bad in ("hier", later(days=9)):
                with self.subTest(at=bad):
                    self.assertEqual(self.client.post(f"/api/plans/{pid}/auto-resume", json={"at": bad}).status_code, 400)
            self.client.post(f"/api/plans/{pid}/auto-resume", json={"cancel": True})
            self.assertIsNone(self.plan(cid)["auto_resume_at"])

        def test_a_resume_too_soon_waits_a_minute(self):
            cid, pid = self.stopped_by_a_quota()
            kept = self.client.post(f"/api/plans/{pid}/auto-resume", json={"at": later(minutes=-30)}).json()["plan"]["auto_resume_at"]
            self.assertGreater(datetime.fromisoformat(kept), datetime.now(timezone.utc) + timedelta(seconds=50))

        def test_a_refused_resume_is_said_on_the_plan(self):
            cid, pid = self.stopped_by_a_quota()
            self.client.post(f"/api/plans/{pid}/auto-resume", json={"at": later(hours=1)})
            shutil.rmtree(self.workdir)  # the working folder is gone meanwhile
            conn = db.connect(self.db_path)
            with conn:
                conn.execute("UPDATE plans SET auto_resume_at = ? WHERE id = ?", (later(minutes=-1), pid))
            conn.close()
            self.assertEqual(self.app.state.scheduler.tick(), [])
            plan = self.plan(cid)
            self.assertIn("n'existe pas", plan["auto_resume_error"])
            self.assertIsNone(plan["auto_resume_at"])  # not tried again and again
            self.assertEqual(plan["status"], "error")

        def test_given_to_another_ai_and_resumed_at_once(self):
            cid, pid = self.stopped_by_a_quota(later(hours=5))
            r = self.client.post(f"/api/plans/{pid}/give", json={"model": "sonnet"})
            self.assertEqual(r.status_code, 400)  # the same AI, the same quota
            r = self.client.post(f"/api/plans/{pid}/give", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 202, r.text)
            self.assertEqual(r.json()["moved"], [2, 3])
            plan = self.wait_idle(cid)["plans"][-1]
            self.assertEqual(plan["status"], "done")
            by_position = {s["position"]: s for s in plan["subtasks"]}
            self.assertEqual([by_position[n]["provider"] for n in (1, 2, 3)], ["claude_code", "codex", "codex"])
            self.assertEqual(by_position[2]["assignment_reason"], "confiée après un quota atteint")

        def test_the_frozen_plan_rule_keeps_only_that_opening(self):
            import sqlite3
            cid, pid = self.stopped_by_a_quota()
            conn = db.connect(self.db_path)
            try:
                other = store.ensure_agent(conn, "codex", "gpt-5.6-luna")  # another agent: only the rule refuses
                for sql in ("UPDATE subtasks SET agent_id = ? WHERE plan_id = ? AND position = 1",       # a task already done
                            "UPDATE subtasks SET description = 'autre' || ? WHERE plan_id = ? AND position = 2",  # its content
                            "UPDATE subtasks SET model = 'autre' || ? WHERE plan_id = ? AND position = 2",  # a model alone
                            "UPDATE subtasks SET kind = 'autre' || ? WHERE plan_id = ? AND position = 2"):  # its kind
                    with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):
                        with conn:
                            conn.execute(sql, (other, pid))
                with conn:
                    conn.execute("UPDATE plans SET status = 'running' WHERE id = ?", (pid,))
                    conn.execute("UPDATE subtasks SET status = 'running' WHERE plan_id = ? AND position = 2", (pid,))
                # A running plan may move its tasks not started (1.1.1), never the one that runs.
                with self.assertRaisesRegex(sqlite3.IntegrityError, "frozen"):
                    with conn:
                        conn.execute("UPDATE subtasks SET agent_id = ? WHERE plan_id = ? AND position = 2", (other, pid))
            finally:
                conn.close()

        def test_only_a_quota_and_only_an_interrupted_plan(self):
            cid, pid = self.stopped_by_a_quota(later(hours=1))
            conn = db.connect(self.db_path)
            try:
                with conn:
                    conn.execute("UPDATE subtasks SET error = 'Claude Code a renvoyé une erreur : panne' WHERE plan_id = ?", (pid,))
                self.assertIsNone(self.plan(cid)["quota"])  # another error is not a quota
                # Interrupted, resumable, but by no quota: nothing to give away.
                r = self.client.post(f"/api/plans/{pid}/give", json={"model": "gpt-5.6-luna"})
                self.assertEqual(r.status_code, 400, r.text)
                with conn:
                    conn.execute("UPDATE subtasks SET error = ? WHERE plan_id = ? AND position = 2", (QUOTA, pid))
                    conn.execute("UPDATE plans SET status = 'running' WHERE id = ?", (pid,))
                self.assertIsNone(self.plan(cid)["quota"])  # still running: not stopped by it (yet)
                with conn:
                    conn.execute("UPDATE plans SET status = 'error' WHERE id = ?", (pid,))
            finally:
                conn.close()
            self.assertEqual(self.plan(cid)["quota"]["subtasks"], [2])

        def test_a_past_return_is_not_known(self):
            cid, pid = self.stopped_by_a_quota(later(hours=-1))
            self.assertIsNone(self.plan(cid)["quota"]["resets_at"])

        def test_a_finished_plan_is_not_resumed(self):
            cid = self.new_conversation(title="Fini", mode="orchestration", conductor_model="opus", assignment_mode="auto")
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.wait_idle(cid)
            r = self.client.post(f"/api/plans/{plan['id']}/auto-resume", json={"at": later(hours=1)})
            self.assertEqual(r.status_code, 400)
            self.assertIsNone(self.plan(cid)["auto_resume_at"])

        def test_nothing_to_give_without_a_quota(self):
            cid = self.new_conversation(title="Site", mode="orchestration", conductor_model="opus", assignment_mode="auto")
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.wait_idle(cid)
            self.assertIsNone(self.plan(cid)["quota"])
            r = self.client.post(f"/api/plans/{plan['id']}/give", json={"model": "gpt-5.6-luna"})
            self.assertEqual(r.status_code, 400)

        def test_a_manual_resume_drops_the_planned_one(self):
            cid, pid = self.stopped_by_a_quota()
            self.client.post(f"/api/plans/{pid}/auto-resume", json={"at": later(hours=1)})
            self.assertEqual(self.client.post(f"/api/plans/{pid}/resume").status_code, 202)
            plan = self.wait_idle(cid)["plans"][-1]
            self.assertEqual(plan["status"], "done")
            self.assertIsNone(plan["auto_resume_at"])


if __name__ == "__main__":
    unittest.main()


try:
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:
    RolesTestCase = None

if RolesTestCase is not None:
    class PlannedResumeAccountsTests(RolesTestCase):
        def plan_stopped(self):
            cid = self.conversation("Site", mode="orchestration", conductor_model="opus")
            conn = db.connect(self.db_path)
            try:
                agent = store.ensure_agent(conn, "claude_code", "opus")
                mid = store.add_user_message(conn, cid, "fais le site", None)
                pid = store.create_plan(conn, cid, mid, [{"description": "la page", "agent_id": agent}])
                store.validate_plan(conn, pid)
                with conn:
                    conn.execute("UPDATE subtasks SET status = 'error', error = ? WHERE plan_id = ?", (QUOTA, pid))
                    conn.execute("UPDATE plans SET status = 'error' WHERE id = ?", (pid,))
            finally:
                conn.close()
            return cid, pid

        def due(self, pid):
            conn = db.connect(self.db_path)
            with conn:
                conn.execute("UPDATE plans SET auto_resume_at = ? WHERE id = ?", (later(minutes=-1), pid))
            conn.close()

        def test_resumed_with_the_accounts_of_who_planned_it(self):
            cid, pid = self.plan_stopped()
            ada, uid = self.person("Ada", role="admin")
            self.assertEqual(ada.post(f"/api/plans/{pid}/auto-resume", json={"at": later(hours=1)}, headers=H).status_code, 200)
            self.due(pid)
            calls = []
            jobs = self.app.state.jobs
            with mock.patch.object(jobs, "resume_execution", lambda c, p, user=None: calls.append((c, p, user))):
                self.app.state.scheduler.tick()
            self.assertEqual([(c, p, u["id"]) for c, p, u in calls], [(cid, pid, uid)])

        def test_a_suspended_account_resumes_nothing(self):
            cid, pid = self.plan_stopped()
            ada, uid = self.person("Ada", role="admin")
            ada.post(f"/api/plans/{pid}/auto-resume", json={"at": later(hours=1)}, headers=H)
            self.owner.patch(f"/api/users/{uid}", json={"disabled": True}, headers=H)
            self.due(pid)
            calls = []
            with mock.patch.object(self.app.state.jobs, "resume_execution", lambda *a, **k: calls.append(a)):
                self.app.state.scheduler.tick()
            self.assertEqual(calls, [])
            conn = db.connect(self.db_path)
            error = conn.execute("SELECT auto_resume_error FROM plans WHERE id = ?", (pid,)).fetchone()[0]
            conn.close()
            self.assertIn("suspendu", error)
