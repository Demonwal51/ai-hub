import json
import unittest

from aihub import db, orchestrator, routing, store
from tests.helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log


class TableTests(unittest.TestCase):
    def test_every_kind_has_rules_ending_with_claude(self):
        self.assertEqual(set(routing.RULES), set(routing.KINDS))
        for kind, rules in routing.RULES.items():
            with self.subTest(kind=kind):
                self.assertTrue(rules)
                self.assertTrue(any(r.provider == "claude_code" for r in rules),
                                "with Claude Code alone, every kind must get an agent")
                for rule in rules:
                    self.assertTrue(rule.evidence.strip())

    def test_claude_alone(self):
        expected = {
            "code": "opus", "terminal": "opus", "migration": "sonnet", "redaction": "opus",
            "analyse": "opus", "recherche": "fable", "maths": "fable", "maths_avancees": "opus",
            "algorithmique": "opus", "ops": "opus", "securite": "haiku", "autre": "sonnet",
        }
        for kind, model in expected.items():
            with self.subTest(kind=kind):
                choice = routing.route(kind)
                self.assertEqual((choice.rule.provider, choice.rule.model), ("claude_code", model))

    def test_reason_shows_the_figure_and_the_better_unconnected_choice(self):
        choice = routing.route("ops")
        self.assertTrue(choice.reason.startswith("Claude Opus : 33,6 % sur SRE Bench"))
        self.assertIn("GPT-6 Astra serait meilleur (n°1 SRE Bench (56,9 %)", choice.reason)
        self.assertIn("Codex n'est pas branché", choice.reason)
        # Where Claude is first, nothing better is mentioned.
        self.assertEqual(routing.route("terminal").reason,
                         "Claude Opus : n°1 Terminal-Bench avec Claude Code (64,8 %), tbench.ai")

    def test_with_codex_connected(self):
        both = frozenset({"claude_code", "codex"})
        self.assertEqual(routing.route("ops", both).rule.model, "gpt-6-astra")
        self.assertEqual(routing.route("maths_avancees", both).rule.model, "gpt-6.1-sol")
        self.assertEqual(routing.route("code", both).rule.model, "opus")  # Claude stays first

    def test_excluded_agents_are_passed_over(self):
        choice = routing.route("terminal", excluded=frozenset({("claude_code", "opus")}))
        self.assertEqual(choice.rule.model, "sonnet")
        self.assertIn("Claude Opus a été retiré du plan", choice.reason)
        everything = frozenset((r.provider, r.model) for rules in routing.RULES.values() for r in rules)
        self.assertIsNone(routing.route("terminal", excluded=everything))

    def test_unknown_kind_is_treated_as_other(self):
        self.assertEqual(routing.route("poésie").rule.model, "sonnet")
        self.assertEqual(routing.route(None).rule.model, "sonnet")


class AutoAssignTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.workdir = self.make_tempdir()
        self.log = tmp / "log.json"
        self.env = fake_env("auto", self.log)
        self.conn = db.connect(str(tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        self.chef = store.ensure_agent(self.conn, "claude_code", "opus")

    def conversation(self, assignment):
        return store.create_conversation(self.conn, "o", mode="orchestration", agent_id=self.chef,
                                         conductor_agent_id=self.chef, assignment_mode=assignment,
                                         workdir=str(self.workdir))

    def use_plan(self, *kinds):
        self.env["FAKE_PLAN"] = json.dumps({"summary": "s", "max_parallel": 2, "subtasks": [
            {"title": f"T{i}", "description": f"d{i}", "kind": k, "depends_on": [], "files": [f"{i}.txt"]}
            for i, k in enumerate(kinds, start=1)]})

    def propose(self, cid):
        plan_id, result = orchestrator.propose_plan(self.conn, cid, "fais", command=FAKE_COMMAND, env=self.env)
        self.assertIsNotNone(plan_id, result.error)
        return plan_id

    def subtasks(self, plan_id):
        return store.get_plan(self.conn, plan_id)["subtasks"]


class AutoAssignTests(AutoAssignTestCase):
    def test_auto_mode_assigns_from_the_table(self):
        self.use_plan("terminal", "recherche", "securite")
        plan_id = self.propose(self.conversation("auto"))
        subs = self.subtasks(plan_id)
        self.assertEqual([(s["kind"], s["provider"], s["model"]) for s in subs],
                         [("terminal", "claude_code", "opus"), ("recherche", "claude_code", "fable"),
                          ("securite", "claude_code", "haiku")])
        self.assertTrue(subs[0]["assignment_reason"].startswith("Claude Opus : n°1 Terminal-Bench"))
        self.assertIn("GPT-6 Sol serait meilleur", subs[2]["assignment_reason"])
        # Missing agents were created, named after their model.
        names = {a["name"] for a in store.list_agents(self.conn)}
        self.assertLessEqual({"Claude Code (fable)", "Claude Code (haiku)"}, names)
        store.validate_plan(self.conn, plan_id)  # nothing left without an agent

    def test_manual_mode_leaves_the_choice_to_the_user(self):
        self.use_plan("terminal")
        plan_id = self.propose(self.conversation("manual"))
        sub = self.subtasks(plan_id)[0]
        self.assertEqual((sub["kind"], sub["agent_id"], sub["assignment_reason"]), ("terminal", None, None))

    def test_conductor_is_told_the_kinds(self):
        self.use_plan("code")
        self.propose(self.conversation("auto"))
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertIn("  - ops : déploiement, serveurs, fiabilité", prompt)
        self.assertIn("maths_avancees", read_log(self.log)["args"][read_log(self.log)["args"].index("--json-schema") + 1])

    def test_a_manual_choice_clears_the_reason(self):
        self.use_plan("terminal")
        plan_id = self.propose(self.conversation("auto"))
        sub = self.subtasks(plan_id)[0]
        sonnet = store.ensure_agent(self.conn, "claude_code", "sonnet")
        store.assign_subtask(self.conn, sub["id"], sonnet)
        self.assertIsNone(self.subtasks(plan_id)[0]["assignment_reason"])

    def test_only_unassigned(self):
        self.use_plan("terminal", "terminal")
        plan_id = self.propose(self.conversation("manual"))
        first, second = self.subtasks(plan_id)
        haiku = store.ensure_agent(self.conn, "claude_code", "haiku")
        store.assign_subtask(self.conn, first["id"], haiku)
        self.assertEqual(routing.auto_assign(self.conn, plan_id, only_unassigned=True), 1)
        first, second = self.subtasks(plan_id)
        self.assertEqual((first["model"], second["model"]), ("haiku", "opus"))

    def test_removed_agent_is_not_brought_back(self):
        self.use_plan("terminal", "code")
        plan_id = self.propose(self.conversation("auto"))
        opus = self.subtasks(plan_id)[0]["agent_id"]
        store.remove_agent_from_plan(self.conn, plan_id, opus)
        self.assertEqual(store.get_plan(self.conn, plan_id)["removed_agents"], [opus])
        routing.auto_assign(self.conn, plan_id)
        subs = self.subtasks(plan_id)
        self.assertEqual([s["model"] for s in subs], ["sonnet", "sonnet"])
        self.assertIn("Claude Opus a été retiré du plan", subs[0]["assignment_reason"])
        # The user gives it back by hand: it is no longer taken out.
        store.assign_subtask(self.conn, subs[0]["id"], opus)
        self.assertEqual(store.get_plan(self.conn, plan_id)["removed_agents"], [])

    def test_adapt_does_not_offer_removed_agents(self):
        self.use_plan("terminal")
        plan_id = self.propose(self.conversation("auto"))
        opus = self.subtasks(plan_id)[0]["agent_id"]
        store.ensure_agent(self.conn, "claude_code", "sonnet")
        store.remove_agent_from_plan(self.conn, plan_id, opus)
        orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertNotIn("Claude Code (opus) : modèle opus", prompt)
        self.assertIn("Claude Code (sonnet) : modèle sonnet", prompt)
        self.assertNotEqual(self.subtasks(plan_id)[0]["agent_id"], opus)

    def test_adapt_in_auto_mode_fills_the_gaps_from_the_table(self):
        self.use_plan("terminal", "recherche")
        plan_id = self.propose(self.conversation("auto"))
        self.env["FAKE_ADAPT_AGENT"] = "agent inconnu"  # the conductor names no usable agent
        orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        subs = self.subtasks(plan_id)
        self.assertEqual([s["model"] for s in subs], ["opus", "fable"])
        self.assertTrue(all(s["assignment_reason"] for s in subs))

    def test_adapt_in_manual_mode_leaves_the_gaps(self):
        self.use_plan("terminal")
        plan_id = self.propose(self.conversation("manual"))
        store.ensure_agent(self.conn, "claude_code", "sonnet")
        self.env["FAKE_ADAPT_AGENT"] = "agent inconnu"
        orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        self.assertIsNone(self.subtasks(plan_id)[0]["agent_id"])

    def test_frozen_after_validation(self):
        self.use_plan("terminal")
        plan_id = self.propose(self.conversation("auto"))
        store.validate_plan(self.conn, plan_id)
        sid = self.subtasks(plan_id)[0]["id"]
        import sqlite3
        for sql in ("UPDATE subtasks SET kind = 'code' WHERE id = ?",
                    "UPDATE subtasks SET assignment_reason = 'x' WHERE id = ?"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, (sid,))
        with self.assertRaisesRegex(store.StoreError, "ne peut plus être modifié"):
            routing.auto_assign(self.conn, plan_id)


class EnsureAgentTests(unittest.TestCase):
    def test_get_or_create(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        a = store.ensure_agent(conn, "claude_code", "opus")
        self.assertEqual(store.ensure_agent(conn, "claude_code", "opus"), a)
        self.assertEqual(store.get_agent(conn, a)["name"], "Claude Code (opus)")
        self.assertEqual(store.get_agent(conn, store.ensure_agent(conn, "claude_code", "default"))["name"],
                         "Claude Code")
        self.assertEqual(store.get_agent(conn, store.ensure_agent(conn, "codex", "gpt-6-astra"))["name"],
                         "Codex (gpt-6-astra)")
        with self.assertRaises(store.StoreError):
            store.ensure_agent(conn, "gemini", "x")


if __name__ == "__main__":
    unittest.main()
