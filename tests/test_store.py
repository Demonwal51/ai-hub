import sqlite3
import unittest

from aihub import db, store
from aihub.store import StoreError


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.claude = store.create_agent(self.conn, "Claude", "claude_code", "opus-5.5")
        self.codex = store.create_agent(self.conn, "Codex", "codex", "gpt-6-astra")

    def count(self, table, where="1", params=()):
        return self.conn.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]


class AgentTests(StoreTestCase):
    def test_create_and_list(self):
        names = [a["name"] for a in store.list_agents(self.conn)]
        self.assertEqual(names, ["Claude", "Codex"])
        agent = store.get_agent(self.conn, self.claude)
        self.assertEqual((agent["provider"], agent["model"], agent["enabled"]),
                         ("claude_code", "opus-5.5", 1))

    def test_duplicate_name_refused(self):
        with self.assertRaisesRegex(StoreError, "existe déjà"):
            store.create_agent(self.conn, "Claude", "claude_code", "sonnet-5.5")
        store.create_agent(self.conn, "Claude Sonnet", "claude_code", "sonnet-5.5")

    def test_invalid_values_refused(self):
        with self.assertRaisesRegex(StoreError, "fournisseur inconnu"):
            store.create_agent(self.conn, "X", "gemini", "m")
        with self.assertRaisesRegex(StoreError, "modèle vide"):
            store.create_agent(self.conn, "X", "codex", "  ")
        with self.assertRaisesRegex(StoreError, "nom de l'agent vide"):
            store.create_agent(self.conn, "", "codex", "m")

    def test_update_model_and_enabled(self):
        store.update_agent(self.conn, self.claude, model="sonnet-5.5", enabled=False)
        agent = store.get_agent(self.conn, self.claude)
        self.assertEqual((agent["model"], agent["enabled"]), ("sonnet-5.5", 0))
        self.assertEqual([a["name"] for a in store.list_agents(self.conn, enabled_only=True)],
                         ["Codex"])

    def test_unknown_agent(self):
        with self.assertRaisesRegex(StoreError, "agent introuvable"):
            store.get_agent(self.conn, 999)

    def test_deleted_agent_leaves_history_intact(self):
        cid = store.create_conversation(self.conn, "c")
        mid = store.start_request(self.conn, cid, self.claude)
        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, "ok", content="réponse")
        store.delete_agent(self.conn, self.claude)
        message = store.get_message(self.conn, mid)
        self.assertIsNone(message["agent_id"])
        self.assertEqual((message["provider"], message["model"]), ("claude_code", "opus-5.5"))

    def test_agent_busy_cannot_be_deleted(self):
        cid = store.create_conversation(self.conn, "c")
        mid = store.start_request(self.conn, cid, self.claude)
        with self.assertRaisesRegex(StoreError, "utilisé par une tâche"):
            store.delete_agent(self.conn, self.claude)
        store.stop_request(self.conn, cid)
        store.delete_agent(self.conn, self.claude)
        self.assertEqual(store.get_message(self.conn, mid)["status"], "stopped")


class ConversationTests(StoreTestCase):
    def test_defaults(self):
        conv = store.get_conversation(self.conn, store.create_conversation(self.conn, "Projet"))
        self.assertEqual(
            (conv["mode"], conv["permission_mode"], conv["assignment_mode"],
             conv["bypass_confirmed_at"]),
            ("single", "manual", "manual", None),
        )

    def test_bypass_requires_confirmation_with_warning(self):
        with self.assertRaises(store.BypassConfirmationRequired) as ctx:
            store.create_conversation(self.conn, "c", permission_mode="bypass")
        self.assertIn("sans rien demander", str(ctx.exception))
        self.assertEqual(self.count("conversations"), 0)

        cid = store.create_conversation(self.conn, "c", permission_mode="bypass",
                                        confirm_bypass=True)
        self.assertIsNotNone(store.get_conversation(self.conn, cid)["bypass_confirmed_at"])

    def test_switching_to_bypass_and_back(self):
        cid = store.create_conversation(self.conn, "c")
        with self.assertRaises(store.BypassConfirmationRequired):
            store.set_permission_mode(self.conn, cid, "bypass")
        self.assertEqual(store.get_conversation(self.conn, cid)["permission_mode"], "manual")

        store.set_permission_mode(self.conn, cid, "bypass", confirm_bypass=True)
        conv = store.get_conversation(self.conn, cid)
        self.assertEqual(conv["permission_mode"], "bypass")
        self.assertIsNotNone(conv["bypass_confirmed_at"])

        # Leaving bypass forgets the confirmation: coming back warns again.
        store.set_permission_mode(self.conn, cid, "plan")
        self.assertIsNone(store.get_conversation(self.conn, cid)["bypass_confirmed_at"])
        with self.assertRaises(store.BypassConfirmationRequired):
            store.set_permission_mode(self.conn, cid, "bypass")

    def test_invalid_settings_refused(self):
        for kwargs, pattern in (
            ({"mode": "duo"}, "mode inconnu"),
            ({"permission_mode": "auto"}, "mode de permission inconnu"),
            ({"assignment_mode": "random"}, "mode d'assignation inconnu"),
            ({"agent_id": 999}, "agent introuvable"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(StoreError, pattern):
                store.create_conversation(self.conn, "c", **kwargs)
        with self.assertRaisesRegex(StoreError, "titre vide"):
            store.create_conversation(self.conn, "   ")

    def test_rename(self):
        cid = store.create_conversation(self.conn, "avant")
        store.rename_conversation(self.conn, cid, "  après  ")
        self.assertEqual(store.get_conversation(self.conn, cid)["title"], "après")
        with self.assertRaisesRegex(StoreError, "titre vide"):
            store.rename_conversation(self.conn, cid, "")

    def test_list_most_recent_first(self):
        old = store.create_conversation(self.conn, "ancienne")
        new = store.create_conversation(self.conn, "récente")
        self.conn.execute("UPDATE conversations SET updated_at = '2026-01-01' WHERE id = ?", (old,))
        self.conn.execute("UPDATE conversations SET updated_at = '2026-02-01' WHERE id = ?", (new,))
        self.assertEqual([c["id"] for c in store.list_conversations(self.conn)], [new, old])
        store.add_user_message(self.conn, old, "relance")
        self.assertEqual([c["id"] for c in store.list_conversations(self.conn)], [old, new])

    def test_update_settings(self):
        cid = store.create_conversation(self.conn, "c")
        store.update_conversation(self.conn, cid, mode="orchestration",
                                  conductor_agent_id=self.claude, assignment_mode="auto",
                                  workdir=r"D:\projets\essai")
        conv = store.get_conversation(self.conn, cid)
        self.assertEqual(
            (conv["mode"], conv["conductor_agent_id"], conv["assignment_mode"], conv["workdir"]),
            ("orchestration", self.claude, "auto", r"D:\projets\essai"),
        )

    def test_settings_locked_during_request(self):
        cid = store.create_conversation(self.conn, "c")
        store.start_request(self.conn, cid, self.claude)
        with self.assertRaisesRegex(StoreError, "requête est en cours"):
            store.update_conversation(self.conn, cid, workdir="ailleurs")
        store.stop_request(self.conn, cid)
        store.update_conversation(self.conn, cid, workdir="ailleurs")

    def test_delete_removes_everything(self):
        cid = store.create_conversation(self.conn, "c", mode="orchestration")
        uid = store.add_user_message(self.conn, cid, "demande")
        pid = store.create_plan(self.conn, cid, uid, [
            {"description": "a", "agent_id": self.claude},
            {"description": "b", "agent_id": self.codex, "depends_on": [1]},
        ])
        other = store.create_conversation(self.conn, "autre")
        store.add_user_message(self.conn, other, "reste")

        store.delete_conversation(self.conn, cid)

        with self.assertRaisesRegex(StoreError, "conversation introuvable"):
            store.get_conversation(self.conn, cid)
        self.assertEqual(self.count("messages", "conversation_id = ?", (cid,)), 0)
        self.assertEqual(self.count("plans", "id = ?", (pid,)), 0)
        self.assertEqual(self.count("subtasks", "plan_id = ?", (pid,)), 0)
        self.assertEqual(self.count("subtask_deps"), 0)
        self.assertEqual(self.count("messages", "conversation_id = ?", (other,)), 1)

    def test_delete_refused_while_request_runs(self):
        cid = store.create_conversation(self.conn, "c")
        store.start_request(self.conn, cid, self.claude)
        with self.assertRaisesRegex(StoreError, "arrêtez la requête"):
            store.delete_conversation(self.conn, cid)
        store.stop_request(self.conn, cid)
        store.delete_conversation(self.conn, cid)
        self.assertEqual(self.count("conversations"), 0)


class MessageTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.cid = store.create_conversation(self.conn, "c")

    def test_full_request(self):
        uid = store.add_user_message(self.conn, self.cid, "bonjour")
        mid = store.start_request(self.conn, self.cid, self.claude)
        self.assertEqual(store.get_message(self.conn, mid)["status"], "pending")
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "Bon")
        store.append_message_content(self.conn, mid, "jour !")
        store.finish_message(self.conn, mid, "ok", duration_ms=1200, tokens_in=10, tokens_out=4)

        user, reply = store.list_messages(self.conn, self.cid)
        self.assertEqual((user["id"], user["role"], user["status"]), (uid, "user", "ok"))
        self.assertEqual(
            (reply["content"], reply["status"], reply["duration_ms"], reply["tokens_in"],
             reply["tokens_out"], reply["provider"], reply["model"]),
            ("Bonjour !", "ok", 1200, 10, 4, "claude_code", "opus-5.5"),
        )
        self.assertIsNotNone(reply["finished_at"])

    def test_one_request_at_a_time(self):
        mid = store.start_request(self.conn, self.cid, self.claude)
        with self.assertRaisesRegex(StoreError, "déjà en cours"):
            store.start_request(self.conn, self.cid, self.codex)
        with self.assertRaisesRegex(StoreError, "déjà en cours"):
            store.add_user_message(self.conn, self.cid, "encore")
        # Other conversations are not blocked.
        other = store.create_conversation(self.conn, "autre")
        store.start_request(self.conn, other, self.codex)

        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, "error", error="quota atteint")
        store.add_user_message(self.conn, self.cid, "encore")
        store.start_request(self.conn, self.cid, self.codex)

    def test_final_content_replaces_streamed_text(self):
        mid = store.start_request(self.conn, self.cid, self.claude)
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "brouillon")
        store.finish_message(self.conn, mid, "ok", content="texte final")
        self.assertEqual(store.get_message(self.conn, mid)["content"], "texte final")

    def test_status_transitions(self):
        mid = store.start_request(self.conn, self.cid, self.claude)
        with self.assertRaisesRegex(StoreError, "« pending » à « ok »"):
            store.finish_message(self.conn, mid, "ok")
        with self.assertRaisesRegex(StoreError, "n'est pas en cours"):
            store.append_message_content(self.conn, mid, "x")
        store.mark_message_running(self.conn, mid)
        with self.assertRaisesRegex(StoreError, "« running » à « running »"):
            store.mark_message_running(self.conn, mid)
        with self.assertRaisesRegex(StoreError, "statut final inconnu"):
            store.finish_message(self.conn, mid, "done")
        store.finish_message(self.conn, mid, "ok")
        with self.assertRaisesRegex(StoreError, "« ok » à « error »"):
            store.finish_message(self.conn, mid, "error")

    def test_stopped_request_ignores_late_result(self):
        mid = store.start_request(self.conn, self.cid, self.claude)
        store.mark_message_running(self.conn, mid)
        self.assertEqual(store.stop_request(self.conn, self.cid), mid)
        with self.assertRaisesRegex(StoreError, "« stopped » à « ok »"):
            store.finish_message(self.conn, mid, "ok", content="trop tard")
        with self.assertRaises(StoreError):
            store.append_message_content(self.conn, mid, "trop tard")
        message = store.get_message(self.conn, mid)
        self.assertEqual((message["status"], message["content"]), ("stopped", ""))
        self.assertIsNone(store.stop_request(self.conn, self.cid))

    def test_bad_measurements_refused(self):
        mid = store.start_request(self.conn, self.cid, self.claude)
        store.mark_message_running(self.conn, mid)
        for kwargs in ({"duration_ms": -1}, {"tokens_in": True}, {"tokens_out": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(StoreError, "invalide"):
                store.finish_message(self.conn, mid, "ok", **kwargs)
        store.finish_message(self.conn, mid, "ok", duration_ms=0, tokens_in=0, tokens_out=0)

    def test_disabled_agent_cannot_answer(self):
        store.update_agent(self.conn, self.claude, enabled=False)
        with self.assertRaisesRegex(StoreError, "désactivé"):
            store.start_request(self.conn, self.cid, self.claude)
        store.start_request(self.conn, self.cid, self.codex)

    def test_empty_user_message_refused(self):
        with self.assertRaisesRegex(StoreError, "message vide"):
            store.add_user_message(self.conn, self.cid, " \n ")


class QuotaAndPermissionTests(StoreTestCase):
    def test_latest_quota_wins(self):
        self.assertIsNone(store.latest_quota(self.conn, "claude_code"))
        store.record_quota(self.conn, "claude_code", "allowed", utilization=0.1)
        store.record_quota(self.conn, "codex", "rejected")
        store.record_quota(self.conn, "claude_code", "allowed_warning", limit_type="five_hour",
                           utilization=0.9, resets_at="2026-10-09T20:00:00+00:00")
        quota = store.latest_quota(self.conn, "claude_code")
        self.assertEqual((quota["status"], quota["limit_type"], quota["utilization"]),
                         ("allowed_warning", "five_hour", 0.9))
        self.assertEqual(store.latest_quota(self.conn, "codex")["status"], "rejected")

    def test_invalid_quota_refused(self):
        with self.assertRaisesRegex(StoreError, "état de quota inconnu"):
            store.record_quota(self.conn, "claude_code", "ok")
        with self.assertRaisesRegex(StoreError, "fournisseur inconnu"):
            store.record_quota(self.conn, "gemini", "allowed")
        for value in (-0.1, True, "50%"):
            with self.subTest(value=value), self.assertRaisesRegex(StoreError, "taux"):
                store.record_quota(self.conn, "claude_code", "allowed", utilization=value)
        store.record_quota(self.conn, "claude_code", "allowed", utilization=0)

    def test_permission_journal(self):
        cid = store.create_conversation(self.conn, "c")
        mid = store.start_request(self.conn, cid, self.claude)
        store.record_permission(self.conn, message_id=mid, tool_name="Bash",
                                input_json='{"command": "ls"}', decision="allow")
        store.record_permission(self.conn, message_id=mid, tool_name="Write",
                                input_json="{}", decision="deny", reason="non")
        rows = store.list_permissions(self.conn, message_id=mid)
        self.assertEqual([(r["tool_name"], r["decision"], r["reason"]) for r in rows],
                         [("Bash", "allow", None), ("Write", "deny", "non")])

    def test_permission_must_belong_to_one_thing(self):
        cid = store.create_conversation(self.conn, "c")
        mid = store.start_request(self.conn, cid, self.claude)
        with self.assertRaisesRegex(StoreError, "message ou à une sous-tâche"):
            store.record_permission(self.conn, tool_name="Bash", input_json="{}", decision="allow")
        with self.assertRaisesRegex(StoreError, "type de décision inconnu"):
            store.record_permission(self.conn, message_id=mid, tool_name="Bash", input_json="{}",
                                    decision="maybe")

    def test_journal_goes_with_the_conversation(self):
        cid = store.create_conversation(self.conn, "c")
        mid = store.start_request(self.conn, cid, self.claude)
        store.record_permission(self.conn, message_id=mid, tool_name="Bash", input_json="{}",
                                decision="allow")
        store.stop_request(self.conn, cid)
        store.delete_conversation(self.conn, cid)
        self.assertEqual(self.count("permission_requests"), 0)

    def test_session_to_resume(self):
        cid = store.create_conversation(self.conn, "c")
        self.assertIsNone(store.last_session(self.conn, cid))
        for session in ("s1", None, "s2"):
            mid = store.start_request(self.conn, cid, self.claude)
            store.mark_message_running(self.conn, mid)
            store.finish_message(self.conn, mid, "ok", session_id=session)
        self.assertEqual(store.last_session(self.conn, cid), ("claude_code", "s2"))
        gpt = store.ensure_agent(self.conn, "codex", "codex")
        mid = store.start_request(self.conn, cid, gpt)
        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, "ok", session_id="t1")
        self.assertEqual(store.last_session(self.conn, cid), ("codex", "t1"))  # the latest, whatever the AI


class PlanTestCase(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.cid = store.create_conversation(self.conn, "orchestre", mode="orchestration",
                                             conductor_agent_id=self.claude)
        self.request = store.add_user_message(self.conn, self.cid, "fais le projet")

    def plan(self, subtasks):
        return store.create_plan(self.conn, self.cid, self.request, subtasks)

    def ids(self, plan_id):
        return [s["id"] for s in store.get_plan(self.conn, plan_id)["subtasks"]]


class PlanEditingTests(PlanTestCase):
    def test_create_plan_with_dependencies(self):
        pid = self.plan([
            {"description": "analyser", "agent_id": self.claude},
            {"description": "coder", "agent_id": self.codex, "model": "gpt-6.1-sol",
             "depends_on": [1]},
            {"description": "relire", "agent_id": self.claude, "depends_on": [1, 2]},
        ])
        plan = store.get_plan(self.conn, pid)
        self.assertEqual(plan["status"], "proposed")
        a, b, c = plan["subtasks"]
        self.assertEqual([s["position"] for s in plan["subtasks"]], [1, 2, 3])
        self.assertEqual((b["provider"], b["model"]), ("codex", "gpt-6.1-sol"))
        self.assertEqual((a["provider"], a["model"]), ("claude_code", "opus-5.5"))
        self.assertEqual((a["depends_on"], b["depends_on"], c["depends_on"]),
                         ([], [a["id"]], [a["id"], b["id"]]))

    def test_plan_needs_orchestration_mode(self):
        single = store.create_conversation(self.conn, "simple")
        uid = store.add_user_message(self.conn, single, "x")
        with self.assertRaisesRegex(StoreError, "pas en mode orchestration"):
            store.create_plan(self.conn, single, uid, [])

    def test_plan_request_must_be_a_user_message_of_the_conversation(self):
        other = store.create_conversation(self.conn, "autre", mode="orchestration")
        foreign = store.add_user_message(self.conn, other, "ailleurs")
        with self.assertRaisesRegex(StoreError, "ne correspond pas"):
            store.create_plan(self.conn, self.cid, foreign, [])

    def test_one_open_plan_per_conversation(self):
        pid = self.plan([{"description": "a", "agent_id": self.claude}])
        with self.assertRaisesRegex(StoreError, "déjà ouvert"):
            self.plan([])
        store.stop_plan(self.conn, pid)
        self.plan([])

    def test_bad_plan_is_not_half_written(self):
        for subtasks, pattern in (
            ([{"description": "a"}, {"description": "b", "depends_on": [5]}], "inexistante"),
            ([{"description": "a", "depends_on": [1]}], "elle-même"),
            ([{"description": "a", "depends_on": [2]}, {"description": "b", "depends_on": [1]}],
             "boucle"),
            ([{"description": "a"}, {"agent_id": self.claude}], "sans description"),
            ([{"description": "a", "agent_id": 999}], "agent introuvable"),
            ([{"description": "a", "model": "m"}], "sans agent"),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(StoreError, pattern):
                    self.plan(subtasks)
                self.assertEqual(self.count("plans"), 0)
                self.assertEqual(self.count("subtasks"), 0)

    def test_reassign_and_change_model(self):
        pid = self.plan([{"description": "a", "agent_id": self.claude}])
        (sid,) = self.ids(pid)
        store.assign_subtask(self.conn, sid, self.codex)
        sub = store.get_plan(self.conn, pid)["subtasks"][0]
        self.assertEqual((sub["agent_id"], sub["provider"], sub["model"]),
                         (self.codex, "codex", "gpt-6-astra"))
        store.assign_subtask(self.conn, sid, self.claude, model="sonnet-5.5")
        sub = store.get_plan(self.conn, pid)["subtasks"][0]
        self.assertEqual((sub["provider"], sub["model"]), ("claude_code", "sonnet-5.5"))
        # The agent's default model is not changed by a per-subtask choice.
        self.assertEqual(store.get_agent(self.conn, self.claude)["model"], "opus-5.5")
        store.assign_subtask(self.conn, sid, None)
        sub = store.get_plan(self.conn, pid)["subtasks"][0]
        self.assertEqual((sub["agent_id"], sub["provider"], sub["model"]), (None, None, None))

    def test_add_and_remove_subtasks_keeps_positions_contiguous(self):
        pid = self.plan([{"description": d, "agent_id": self.claude} for d in "abc"])
        a, b, c = self.ids(pid)
        store.add_dependency(self.conn, c, b)
        store.remove_subtask(self.conn, b)
        plan = store.get_plan(self.conn, pid)
        self.assertEqual([(s["description"], s["position"]) for s in plan["subtasks"]],
                         [("a", 1), ("c", 2)])
        self.assertEqual(plan["subtasks"][1]["depends_on"], [])
        new = store.add_subtask(self.conn, pid, "d", agent_id=self.codex)
        self.assertEqual(store.get_plan(self.conn, pid)["subtasks"][-1]["id"], new)
        self.assertEqual([s["position"] for s in store.get_plan(self.conn, pid)["subtasks"]],
                         [1, 2, 3])

    def test_remove_agent_from_plan(self):
        pid = self.plan([
            {"description": "a", "agent_id": self.claude},
            {"description": "b", "agent_id": self.codex},
            {"description": "c", "agent_id": self.claude},
        ])
        a, b, c = self.ids(pid)
        self.assertEqual(store.remove_agent_from_plan(self.conn, pid, self.claude), [a, c])
        subs = store.get_plan(self.conn, pid)["subtasks"]
        self.assertEqual([s["agent_id"] for s in subs], [None, self.codex, None])
        # The agent still exists in the configuration.
        store.get_agent(self.conn, self.claude)

    def test_dependencies(self):
        pid = self.plan([{"description": d, "agent_id": self.claude} for d in "abc"])
        a, b, c = self.ids(pid)
        store.add_dependency(self.conn, b, a)
        store.add_dependency(self.conn, c, b)
        with self.assertRaisesRegex(StoreError, "boucle"):
            store.add_dependency(self.conn, a, c)
        with self.assertRaisesRegex(StoreError, "elle-même"):
            store.add_dependency(self.conn, a, a)
        store.add_dependency(self.conn, c, b)  # repeating is harmless
        store.remove_dependency(self.conn, c, b)
        store.add_dependency(self.conn, a, c)  # no loop any more
        self.assertEqual(store.get_plan(self.conn, pid)["subtasks"][0]["depends_on"], [c])

    def test_dependency_across_plans_refused(self):
        pid = self.plan([{"description": "a", "agent_id": self.claude}])
        other = store.create_conversation(self.conn, "o", mode="orchestration")
        uid = store.add_user_message(self.conn, other, "x")
        opid = store.create_plan(self.conn, other, uid, [{"description": "b"}])
        with self.assertRaisesRegex(StoreError, "même plan"):
            store.add_dependency(self.conn, self.ids(pid)[0], self.ids(opid)[0])

    def test_validation_requirements(self):
        pid = self.plan([])
        with self.assertRaisesRegex(StoreError, "aucune sous-tâche"):
            store.validate_plan(self.conn, pid)
        a = store.add_subtask(self.conn, pid, "a", agent_id=self.claude)
        b = store.add_subtask(self.conn, pid, "b")
        with self.assertRaisesRegex(StoreError, "sans agent : 2"):
            store.validate_plan(self.conn, pid)
        store.assign_subtask(self.conn, b, self.codex)
        store.update_agent(self.conn, self.codex, enabled=False)
        with self.assertRaisesRegex(StoreError, "désactivés dans le plan : Codex"):
            store.validate_plan(self.conn, pid)
        store.update_agent(self.conn, self.codex, enabled=True)
        store.validate_plan(self.conn, pid)
        plan = store.get_plan(self.conn, pid)
        self.assertEqual(plan["status"], "validated")
        self.assertIsNotNone(plan["validated_at"])
        self.assertEqual(len(plan["subtasks"]), 2)
        self.assertNotEqual(a, b)

    def test_validated_plan_cannot_be_edited(self):
        pid = self.plan([{"description": "a", "agent_id": self.claude},
                         {"description": "b", "agent_id": self.codex}])
        a, b = self.ids(pid)
        store.validate_plan(self.conn, pid)
        for action in (
            lambda: store.add_subtask(self.conn, pid, "c", agent_id=self.claude),
            lambda: store.remove_subtask(self.conn, a),
            lambda: store.assign_subtask(self.conn, a, self.codex),
            lambda: store.remove_agent_from_plan(self.conn, pid, self.claude),
            lambda: store.add_dependency(self.conn, b, a),
            lambda: store.remove_dependency(self.conn, b, a),
            lambda: store.validate_plan(self.conn, pid),
        ):
            with self.assertRaisesRegex(StoreError, "ne peut plus être modifié"):
                action()
        self.assertEqual(
            [(s["description"], s["agent_id"]) for s in store.get_plan(self.conn, pid)["subtasks"]],
            [("a", self.claude), ("b", self.codex)],
        )

    def test_mode_change_refused_while_plan_open(self):
        pid = self.plan([{"description": "a", "agent_id": self.claude}])
        with self.assertRaisesRegex(StoreError, "un plan est ouvert"):
            store.update_conversation(self.conn, self.cid, mode="single")
        store.stop_plan(self.conn, pid)
        store.update_conversation(self.conn, self.cid, mode="single")


class ConductorPlanTests(PlanTestCase):
    def test_plan_content_from_the_conductor(self):
        pid = store.create_plan(self.conn, self.cid, self.request, [
            {"title": "A", "description": "faire A", "files": ["a.py", " b.py "]},
            {"description": "faire B", "depends_on": [1]},
        ], summary="deux étapes", max_parallel=2)
        plan = store.get_plan(self.conn, pid)
        self.assertEqual((plan["summary"], plan["max_parallel"]), ("deux étapes", 2))
        a, b = plan["subtasks"]
        self.assertEqual((a["title"], a["files"], b["title"], b["files"]), ("A", ["a.py", "b.py"], None, []))
        self.assertEqual(store.list_plans(self.conn, self.cid)[0]["id"], pid)

    def test_bad_values_refused(self):
        for kwargs, specs, pattern in (
            ({"max_parallel": 0}, [], "parallèle invalide"),
            ({"max_parallel": True}, [], "parallèle invalide"),
            ({}, [{"description": "d", "files": ["ok", ""]}], "fichiers invalide"),
            ({}, [{"description": "d", "files": "a.py"}], "fichiers invalide"),
            ({}, [{"description": "d", "title": " "}], "titre de la sous-tâche vide"),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(StoreError, pattern):
                store.create_plan(self.conn, self.cid, self.request, specs, **kwargs)
            self.assertEqual(self.count("plans"), 0)

    def test_replace_plan(self):
        pid = self.plan([{"description": "ancien", "agent_id": self.claude}])
        store.replace_plan(self.conn, pid, [{"title": "N1", "description": "nouveau 1", "agent_id": self.codex},
                                            {"title": "N2", "description": "nouveau 2", "depends_on": [1]}],
                           summary="refait", max_parallel=1)
        plan = store.get_plan(self.conn, pid)
        self.assertEqual([(s["title"], s["position"], s["agent_id"]) for s in plan["subtasks"]],
                         [("N1", 1, self.codex), ("N2", 2, None)])
        self.assertEqual(plan["subtasks"][1]["depends_on"], [plan["subtasks"][0]["id"]])
        self.assertEqual((plan["summary"], plan["max_parallel"]), ("refait", 1))

    def test_update_subtask(self):
        pid = self.plan([{"title": "T", "description": "d", "files": ["a"]}])
        (sid,) = self.ids(pid)
        store.update_subtask(self.conn, sid, title="T2")
        store.update_subtask(self.conn, sid, files=["b", "c"])
        sub = store.get_plan(self.conn, pid)["subtasks"][0]
        self.assertEqual((sub["title"], sub["description"], sub["files"]), ("T2", "d", ["b", "c"]))
        with self.assertRaisesRegex(StoreError, "vide"):
            store.update_subtask(self.conn, sid, description="  ")

    def test_frozen_after_validation(self):
        pid = self.plan([{"title": "T", "description": "d", "agent_id": self.claude}])
        (sid,) = self.ids(pid)
        store.validate_plan(self.conn, pid)
        for action in (lambda: store.replace_plan(self.conn, pid, [{"description": "x"}]),
                       lambda: store.update_subtask(self.conn, sid, title="autre")):
            with self.assertRaisesRegex(StoreError, "ne peut plus être modifié"):
                action()
        # The database itself refuses, whatever code tries.
        for sql in ("UPDATE subtasks SET title = 'x' WHERE id = ?",
                    "UPDATE subtasks SET files_json = '[]' WHERE id = ?"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, (sid,))
        for sql in ("UPDATE plans SET summary = 'x' WHERE id = ?",
                    "UPDATE plans SET max_parallel = 5 WHERE id = ?"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(sql, (pid,))
        store.set_plan_synthesis(self.conn, pid, self.request)  # execution fields stay writable

    def test_open_plan_does_not_block_discussion(self):
        # The user may discuss an open plan with the conductor; asking for a
        # second plan is refused by the orchestrator, not here.
        self.plan([{"description": "d"}])
        store.add_user_message(self.conn, self.cid, "une question sur le plan")


class PlanExecutionTests(PlanTestCase):
    def setUp(self):
        super().setUp()
        # a and b are independent; c needs both.
        self.pid = self.plan([
            {"description": "a", "agent_id": self.claude},
            {"description": "b", "agent_id": self.codex},
            {"description": "c", "agent_id": self.claude, "depends_on": [1, 2]},
        ])
        self.a, self.b, self.c = self.ids(self.pid)

    def ready(self):
        return [s["id"] for s in store.ready_subtasks(self.conn, self.pid)]

    def run_ok(self, sid):
        store.start_subtask(self.conn, sid)
        store.finish_subtask(self.conn, sid, "ok", result="fait", duration_ms=5)

    def test_nothing_runs_before_validation_and_start(self):
        self.assertEqual(self.ready(), [])
        with self.assertRaisesRegex(StoreError, "seul un plan validé"):
            store.start_plan(self.conn, self.pid)
        with self.assertRaisesRegex(StoreError, "ne peut pas démarrer"):
            store.start_subtask(self.conn, self.a)
        store.validate_plan(self.conn, self.pid)
        self.assertEqual(self.ready(), [])
        store.start_plan(self.conn, self.pid)
        self.assertEqual(self.ready(), [self.a, self.b])

    def test_independent_subtasks_run_in_parallel_and_dependencies_wait(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        store.start_subtask(self.conn, self.a)
        store.start_subtask(self.conn, self.b)
        running = self.count("subtasks", "plan_id = ? AND status = 'running'", (self.pid,))
        self.assertEqual(running, 2)
        with self.assertRaisesRegex(StoreError, "ne peut pas démarrer"):
            store.start_subtask(self.conn, self.c)
        store.finish_subtask(self.conn, self.a, "ok")
        self.assertEqual(self.ready(), [])
        store.finish_subtask(self.conn, self.b, "ok")
        self.assertEqual(self.ready(), [self.c])
        with self.assertRaisesRegex(StoreError, "peuvent encore être lancées"):
            store.finish_plan(self.conn, self.pid)
        store.start_subtask(self.conn, self.c)
        with self.assertRaisesRegex(StoreError, "encore en cours"):
            store.finish_plan(self.conn, self.pid)
        store.finish_subtask(self.conn, self.c, "ok", result="synthèse", tokens_in=3, tokens_out=2)
        store.finish_plan(self.conn, self.pid)
        plan = store.get_plan(self.conn, self.pid)
        self.assertEqual(plan["status"], "done")
        self.assertIsNotNone(plan["finished_at"])
        self.assertEqual(plan["subtasks"][2]["result"], "synthèse")

    def test_a_running_subtask_cannot_start_twice(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        store.start_subtask(self.conn, self.a)
        with self.assertRaisesRegex(StoreError, "ne peut pas démarrer"):
            store.start_subtask(self.conn, self.a)

    def test_failure_blocks_dependents_and_fails_plan(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        self.run_ok(self.a)
        store.start_subtask(self.conn, self.b)
        store.finish_subtask(self.conn, self.b, "error", error="quota Codex atteint")
        self.assertEqual(self.ready(), [])
        store.finish_plan(self.conn, self.pid)
        plan = store.get_plan(self.conn, self.pid)
        self.assertEqual(plan["status"], "error")
        self.assertEqual([s["status"] for s in plan["subtasks"]], ["ok", "error", "stopped"])
        self.assertEqual(plan["subtasks"][1]["error"], "quota Codex atteint")

    def test_stop_plan(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        self.run_ok(self.a)
        store.start_subtask(self.conn, self.b)
        self.assertEqual(store.stop_plan(self.conn, self.pid), [self.b])
        plan = store.get_plan(self.conn, self.pid)
        self.assertEqual(plan["status"], "stopped")
        self.assertEqual([s["status"] for s in plan["subtasks"]], ["ok", "stopped", "stopped"])
        # A result arriving after the stop is refused; nothing else can start.
        with self.assertRaisesRegex(StoreError, "« stopped » à « ok »"):
            store.finish_subtask(self.conn, self.b, "ok", result="trop tard")
        self.assertEqual(self.ready(), [])
        with self.assertRaisesRegex(StoreError, "déjà terminé"):
            store.stop_plan(self.conn, self.pid)

    def test_proposed_plan_can_be_cancelled(self):
        self.assertEqual(store.stop_plan(self.conn, self.pid), [])
        self.assertEqual(store.get_plan(self.conn, self.pid)["status"], "stopped")

    def test_conversation_with_running_plan_cannot_be_deleted(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        with self.assertRaisesRegex(StoreError, "arrêtez le plan"):
            store.delete_conversation(self.conn, self.cid)
        store.stop_plan(self.conn, self.pid)
        store.delete_conversation(self.conn, self.cid)

    def test_agent_of_validated_plan_cannot_be_deleted(self):
        store.validate_plan(self.conn, self.pid)
        with self.assertRaisesRegex(StoreError, "utilisé par une tâche"):
            store.delete_agent(self.conn, self.codex)

    def test_agent_of_finished_plan_can_be_deleted(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        for sid in (self.a, self.b, self.c):
            self.run_ok(sid)
        store.finish_plan(self.conn, self.pid)
        store.delete_agent(self.conn, self.codex)
        b = store.get_plan(self.conn, self.pid)["subtasks"][1]
        self.assertEqual((b["agent_id"], b["provider"], b["model"]), (None, "codex", "gpt-6-astra"))

    def test_bad_subtask_measurements_refused(self):
        store.validate_plan(self.conn, self.pid)
        store.start_plan(self.conn, self.pid)
        store.start_subtask(self.conn, self.a)
        with self.assertRaisesRegex(StoreError, "invalide"):
            store.finish_subtask(self.conn, self.a, "ok", duration_ms=-5)
        store.finish_subtask(self.conn, self.a, "ok", duration_ms=5)


if __name__ == "__main__":
    unittest.main()
