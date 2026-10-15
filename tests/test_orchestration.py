import json
import os
import threading
import unittest
from pathlib import Path

from aihub import db, orchestrator, store
from aihub.orchestrator import PlanExecution
from aihub.providers.claude_code import Decision, PermissionRequest, ProviderError
from aihub.store import StoreError
from tests.helpers import (FAKE_COMMAND, FAKE_COMMANDS, TempDirMixin, fake_env, read_log, wait_dead,
                           wait_for_log_key)


def subtask(title, description=None, depends_on=(), files=("x.txt",), kind="code"):
    return {"title": title, "description": description or title, "kind": kind,
            "depends_on": list(depends_on), "files": list(files)}


class OrchestrationTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.workdir = self.make_tempdir()
        self.logs = self.make_tempdir()
        self.log = tmp / "log.json"
        self.env = fake_env("auto", self.log)
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.logs)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.chef = store.create_agent(self.conn, "Chef", "claude_code", "opus")
        self.a = store.create_agent(self.conn, "Agent A", "claude_code", "sonnet")
        self.b = store.create_agent(self.conn, "Agent B", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "orchestre", mode="orchestration",
                                             agent_id=self.chef, conductor_agent_id=self.chef,
                                             workdir=str(self.workdir))

    def use_plan(self, summary="plan d'essai", max_parallel=2, subtasks=()):
        self.env["FAKE_PLAN"] = json.dumps({"summary": summary, "max_parallel": max_parallel,
                                            "subtasks": list(subtasks)})

    def propose(self, prompt="fais le projet"):
        return orchestrator.propose_plan(self.conn, self.cid, prompt, command=FAKE_COMMAND, env=self.env)

    def ready_plan(self, agent=None):
        plan_id, result = self.propose()
        self.assertIsNotNone(plan_id, result.error)
        for st in store.get_plan(self.conn, plan_id)["subtasks"]:
            store.assign_subtask(self.conn, st["id"], agent or self.a)
        store.validate_plan(self.conn, plan_id)
        return plan_id

    def execute(self, plan_id, ask_permission=None):
        events = []
        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMAND, env=self.env,
                                  ask_permission=ask_permission, on_event=events.append)
        status = execution.run()
        return status, events, execution

    def runs(self):
        """The fake CLI's per-process logs, subtasks only, by title."""
        found = {}
        for path in self.logs.glob("run-*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("scenario") not in (None, "conductor", "synthesis"):
                title = data["subtask"].splitlines()[0].split(":", 1)[-1].strip()
                found[title] = data
        return found

    @staticmethod
    def overlap(one, two):
        return one["started"] < two["ended"] and two["started"] < one["ended"]


class PlanningTests(OrchestrationTestCase):
    def test_conductor_proposes_a_structured_plan(self):
        plan_id, result = self.propose("construis le site")
        self.assertEqual(result.status, "ok")
        plan = store.get_plan(self.conn, plan_id)
        self.assertEqual((plan["status"], plan["max_parallel"], plan["summary"]),
                         ("proposed", 2, "Deux parties en parallèle, puis l'assemblage."))
        a, b, c = plan["subtasks"]
        self.assertEqual([s["title"] for s in plan["subtasks"]], ["Partie A", "Partie B", "Assemblage"])
        self.assertEqual((a["files"], b["files"], c["files"]), (["a.txt"], ["b.txt"], ["c.txt"]))
        self.assertEqual(c["depends_on"], [a["id"], b["id"]])
        self.assertEqual([s["agent_id"] for s in plan["subtasks"]], [None, None, None])
        user, reply = store.list_messages(self.conn, self.cid)
        self.assertEqual(user["content"], "construis le site")
        self.assertEqual(reply["content"], "Plan proposé : Deux parties en parallèle, puis l'assemblage.")

    def test_conductor_only_reads(self):
        self.propose()
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--json-schema", args)
        self.assertNotIn("--resume", args)
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertIn("Ne modifie rien", prompt)
        self.assertIn("<demande>\nfais le projet\n</demande>", prompt)

    def test_malformed_plan_creates_nothing(self):
        self.env["FAKE_PLAN"] = json.dumps({"summary": "vide", "max_parallel": 1, "subtasks": []})
        plan_id, result = self.propose()
        self.assertIsNone(plan_id)
        self.assertEqual(result.status, "error")
        reply = store.list_messages(self.conn, self.cid)[-1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("pas rendu de plan", reply["error"])
        self.assertIsNone(store.open_plan(self.conn, self.cid))

    def test_open_plan_blocks_a_second_plan(self):
        plan_id, _ = self.propose()
        before = len(store.list_messages(self.conn, self.cid))
        self.log.unlink()
        with self.assertRaisesRegex(store.Busy, "un plan est déjà ouvert"):
            self.propose("autre chose")
        # Refused before anything: no message recorded, the conductor never ran.
        self.assertEqual(len(store.list_messages(self.conn, self.cid)), before)
        self.assertFalse(self.log.exists())
        store.stop_plan(self.conn, plan_id)
        self.assertIsNotNone(self.propose("autre chose")[0])

    def test_single_conversation_is_refused(self):
        single = store.create_conversation(self.conn, "s", agent_id=self.a, workdir=str(self.workdir))
        with self.assertRaisesRegex(StoreError, "pas en mode orchestration"):
            orchestrator.propose_plan(self.conn, single, "x", command=FAKE_COMMAND, env=self.env)


class DiscussionTests(OrchestrationTestCase):
    def discuss(self, text):
        return orchestrator.discuss(self.conn, self.cid, text, command=FAKE_COMMAND, env=self.env)

    def test_talk_with_the_conductor(self):
        result = self.discuss("Combien de fichiers as-tu vus ?")
        self.assertEqual(result.status, "ok")
        user, reply = store.list_messages(self.conn, self.cid)
        self.assertEqual(user["content"], "Combien de fichiers as-tu vus ?")
        self.assertEqual((reply["content"], reply["agent_id"]),
                         ("Le chef répond à : Combien de fichiers as-tu vus ?", self.chef))
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("--json-schema", args)
        self.assertIn("ne modifie rien", read_log(self.log)["user_message"]["message"]["content"])
        self.assertIsNone(store.open_plan(self.conn, self.cid))  # talking makes no plan

    def test_conductor_remembers_its_session(self):
        self.propose()
        self.discuss("pourquoi deux agents ?")
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")

    def test_open_plan_is_shown_as_the_user_left_it(self):
        plan_id, _ = self.propose()
        first = store.get_plan(self.conn, plan_id)["subtasks"][0]
        store.assign_subtask(self.conn, first["id"], self.b)
        self.discuss("et la partie A ?")
        self.assertEqual(store.list_messages(self.conn, self.cid)[-1]["content"],
                         "Le chef répond à : et la partie A ? (plan vu)")
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertIn("Partie A — agent : Agent B (haiku)", prompt)
        self.assertIn("« Adapter le plan »", prompt)
        # Discussing does not touch the plan.
        self.assertEqual(store.get_plan(self.conn, plan_id)["status"], "proposed")

    def test_adapting_applies_the_discussion(self):
        plan_id, _ = self.propose()
        self.discuss("fais-le sans la partie B")
        orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertIn("Discussion avec l'utilisateur depuis le plan", prompt)
        self.assertIn("Utilisateur : fais-le sans la partie B", prompt)
        self.assertIn("Toi (chef) : Le chef répond à : fais-le sans la partie B", prompt)
        self.assertNotIn("Toi (chef) : Plan proposé", prompt)

    def test_no_discussion_no_section(self):
        plan_id, _ = self.propose()
        orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        self.assertNotIn("Discussion avec l'utilisateur depuis le plan",
                         read_log(self.log)["user_message"]["message"]["content"])

    def test_only_in_orchestration(self):
        single = store.create_conversation(self.conn, "s", agent_id=self.a, workdir=str(self.workdir))
        with self.assertRaisesRegex(StoreError, "pas en mode orchestration"):
            orchestrator.discuss(self.conn, single, "x", command=FAKE_COMMAND, env=self.env)


class AnswerParsingTests(unittest.TestCase):
    def test_specs_from_answer(self):
        summary, parallel, specs = orchestrator.specs_from_answer({
            "summary": " s ", "max_parallel": 50,
            "subtasks": [{"title": "t", "description": "d", "depends_on": [2, 9, True, 0], "files": []},
                         {"description": "e", "files": ["", "f.py"]}]})
        self.assertEqual((summary, parallel), ("s", 2))  # capped by the number of subtasks
        self.assertEqual(specs[0], {"title": "t", "description": "d", "files": ["*"], "depends_on": [2],
                                    "kind": "autre"})
        self.assertEqual(specs[1]["files"], ["f.py"])

    def test_unknown_kind_becomes_other(self):
        _, _, specs = orchestrator.specs_from_answer(
            {"max_parallel": 1, "subtasks": [{"description": "d", "kind": "terminal"},
                                             {"description": "e", "kind": "poésie"}]})
        self.assertEqual([s["kind"] for s in specs], ["terminal", "autre"])

    def test_agent_names_become_ids(self):
        agents = {"A": {"id": 7}}
        _, _, specs = orchestrator.specs_from_answer(
            {"max_parallel": 1, "subtasks": [{"description": "d", "agent": "A"},
                                             {"description": "e", "agent": "inconnu"}]}, agents)
        self.assertEqual(specs[0]["agent_id"], 7)
        self.assertNotIn("agent_id", specs[1])

    def test_refusals(self):
        for answer in (None, {}, {"subtasks": []}, {"subtasks": [{"description": " "}]}):
            with self.subTest(answer=answer), self.assertRaises(ProviderError):
                orchestrator.specs_from_answer(answer)


class ExecutionTests(OrchestrationTestCase):
    def test_full_plan_with_synthesis(self):
        plan_id = self.ready_plan()
        status, events, execution = self.execute(plan_id)
        self.assertEqual(status, "done")
        plan = store.get_plan(self.conn, plan_id)
        self.assertEqual([(s["status"], s["result"]) for s in plan["subtasks"]],
                         [("ok", "fait : Partie A"), ("ok", "fait : Partie B"), ("ok", "fait : Assemblage")])
        synthesis = store.get_message(self.conn, plan["synthesis_message_id"])
        self.assertEqual((synthesis["status"], synthesis["content"]),
                         ("ok", "Synthèse : 3 réussie(s), 0 en échec."))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "plan_started")
        self.assertEqual(kinds.count("subtask_started"), 3)
        self.assertEqual(kinds[-1], "plan_done")
        self.assertIn("synthesis_started", kinds)
        # The subtask's own model is used.
        assemblage = self.runs()["Assemblage"]
        self.assertEqual(assemblage["args"][assemblage["args"].index("--model") + 1], "sonnet")
        self.assertEqual(os.path.normcase(assemblage["cwd"]), os.path.normcase(str(self.workdir)))

    def test_a_validated_plan_asked_without_prompt_is_not_taken_for_a_mere_plan(self):
        # The automatic request says "Prépare un plan" : once the user validated
        # the plan, neither the agents nor the synthesis may read it as "plan only".
        plan_id, result = self.propose("")
        self.assertIsNotNone(plan_id, result.error)
        request = store.get_message(self.conn, store.get_plan(self.conn, plan_id)["request_message_id"])
        self.assertEqual(request["content"], orchestrator.AUTO_PLAN_REQUEST)
        for st in store.get_plan(self.conn, plan_id)["subtasks"]:
            store.assign_subtask(self.conn, st["id"], self.a)
        store.validate_plan(self.conn, plan_id)
        self.assertEqual(self.execute(plan_id)[0], "done")
        logs = [json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json")]
        prompts = {d.get("scenario"): d["user_message"]["message"]["content"] for d in logs
                   if d.get("scenario") in ("synthesis", "subtask")}
        self.assertEqual(set(prompts), {"synthesis", "subtask"})
        for kind, prompt in prompts.items():
            self.assertNotIn(orchestrator.AUTO_PLAN_REQUEST, prompt, kind)
            self.assertIn("puis il l'a validé", prompt, kind)
            self.assertIn(orchestrator.VALIDATED, prompt, kind)
        self.assertIn("Ne présente pas son exécution comme un écart", prompts["synthesis"])
        summary = store.get_plan(self.conn, plan_id)["summary"]
        self.assertTrue(summary)
        self.assertIn(f"Plan d'ensemble : {summary}", prompts["synthesis"])

    def test_a_written_request_reaches_the_agents_as_written(self):
        self.execute(self.ready_plan())
        logs = [json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json")]
        synthesis = next(d for d in logs if d.get("scenario") == "synthesis")["user_message"]["message"]["content"]
        self.assertIn("<demande>\nfais le projet\n</demande>", synthesis)
        self.assertIn(orchestrator.VALIDATED, synthesis)

    def test_dependencies_get_earlier_results(self):
        self.execute(self.ready_plan())
        prompt = self.runs()["Assemblage"]["user_message"]["message"]["content"]
        self.assertIn("Résultat de la sous-tâche 1 (Partie A), dont tu dépends :\nfait : Partie A", prompt)
        self.assertIn("Résultat de la sous-tâche 2 (Partie B)", prompt)
        runs = self.runs()
        self.assertGreaterEqual(runs["Assemblage"]["started"], runs["Partie A"]["ended"])
        self.assertGreaterEqual(runs["Assemblage"]["started"], runs["Partie B"]["ended"])

    def test_independent_subtasks_run_at_the_same_time(self):
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "lent un", files=["1.txt"]),
                                                subtask("Deux", "lent deux", files=["2.txt"])])
        self.execute(self.ready_plan())
        runs = self.runs()
        self.assertTrue(self.overlap(runs["Un"], runs["Deux"]))

    def test_conductor_limit_is_respected(self):
        self.use_plan(max_parallel=1, subtasks=[subtask("Un", "lent un", files=["1.txt"]),
                                                subtask("Deux", "lent deux", files=["2.txt"])])
        self.execute(self.ready_plan())
        runs = self.runs()
        self.assertFalse(self.overlap(runs["Un"], runs["Deux"]))

    def test_shared_declared_file_means_one_after_the_other(self):
        for files in (["commun.txt"], ["*"]):
            with self.subTest(files=files):
                for path in self.logs.glob("*.json"):
                    path.unlink()
                self.use_plan(max_parallel=2, subtasks=[subtask("Un", "lent un", files=files),
                                                        subtask("Deux", "lent deux", files=["2.txt"])
                                                        if files == ["*"] else
                                                        subtask("Deux", "lent deux", files=files)])
                cid = store.create_conversation(self.conn, "o", mode="orchestration", agent_id=self.chef,
                                                conductor_agent_id=self.chef, workdir=str(self.workdir))
                self.cid = cid
                self.execute(self.ready_plan())
                runs = self.runs()
                self.assertFalse(self.overlap(runs["Un"], runs["Deux"]))

    def test_failure_is_reported_not_hidden(self):
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "plante", files=["1.txt"]),
                                                subtask("Deux", "deux", files=["2.txt"]),
                                                subtask("Trois", "trois", depends_on=[1], files=["3.txt"])])
        plan_id = self.ready_plan()
        status, events, _ = self.execute(plan_id)
        self.assertEqual(status, "error")
        plan = store.get_plan(self.conn, plan_id)
        self.assertEqual([s["status"] for s in plan["subtasks"]], ["error", "ok", "stopped"])
        self.assertIn("code 3", plan["subtasks"][0]["error"])
        synthesis = store.get_message(self.conn, plan["synthesis_message_id"])
        self.assertEqual(synthesis["content"], "Synthèse : 1 réussie(s), 1 en échec.")
        prompt = [d for d in (json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json"))
                  if d.get("scenario") == "synthesis"][0]["user_message"]["message"]["content"]
        self.assertIn("ne masque aucune erreur", prompt)
        self.assertIn("Sous-tâche 3 — Trois — non lancée (bloquée)", prompt)

    def test_stop_kills_every_agent(self):
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "attends un", files=["1.txt"]),
                                                subtask("Deux", "attends deux", files=["2.txt"]),
                                                subtask("Trois", "trois", depends_on=[1, 2], files=["3.txt"])])
        plan_id = self.ready_plan()
        events = []
        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMAND, env=self.env,
                                  on_event=events.append)
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("status", execution.run()))
        self.addCleanup(thread.join, 20)
        self.addCleanup(execution.stop)
        thread.start()

        def two_children():
            pids = []
            for path in self.logs.glob("run-*.json"):
                try:
                    pids.append(json.loads(path.read_text(encoding="utf-8")).get("child_pid"))
                except (OSError, ValueError):
                    pass  # The fake CLI may still be writing; retry until the deadline.
            return [p for p in pids if p]

        import time
        deadline = time.monotonic() + 20
        while len(two_children()) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        children = two_children()
        self.assertEqual(len(children), 2)
        execution.stop()
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["status"], "stopped")
        plan = store.get_plan(self.conn, plan_id)
        self.assertEqual((plan["status"], [s["status"] for s in plan["subtasks"]]),
                         ("stopped", ["stopped", "stopped", "stopped"]))
        self.assertIsNone(plan["synthesis_message_id"])
        for pid in children:
            self.assertTrue(wait_dead(pid))

    def test_writes_to_the_same_file_never_overlap(self):
        # Declared files differ, so both start together; both then ask to
        # write note.txt: the second one waits for the first to finish
        # before anyone is asked.
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "écris un", files=["1.txt"]),
                                                subtask("Deux", "écris deux", files=["2.txt"])])
        plan_id = self.ready_plan()
        order = []
        lock = threading.Lock()
        events = []

        def on_event(event):
            if event["type"] == "subtask_done":
                with lock:
                    order.append(("done", event["subtask_id"]))
            events.append(event)

        def ask(sub, request):
            with lock:
                order.append(("ask", sub["id"]))
            return Decision(True)

        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMAND, env=self.env,
                                  ask_permission=ask, on_event=on_event)
        self.assertEqual(execution.run(), "done")
        first = order[0][1]
        second = next(sid for kind, sid in order if kind == "ask" and sid != first)
        self.assertLess(order.index(("done", first)), order.index(("ask", second)))
        journal = store.list_permissions(self.conn, subtask_id=first)
        self.assertEqual([(j["tool_name"], j["decision"]) for j in journal], [("Write", "allow")])

    def test_refused_write_releases_the_file_at_once(self):
        # The refused agent keeps running for a second (fake CLI); the other
        # one must not wait for its end to be asked.
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "écris un", files=["1.txt"]),
                                                subtask("Deux", "écris deux", files=["2.txt"])])
        plan_id = self.ready_plan()
        order = []
        lock = threading.Lock()

        def on_event(event):
            if event["type"] == "subtask_done":
                with lock:
                    order.append(("done", event["subtask_id"]))

        def ask(sub, request):
            with lock:
                order.append(("ask", sub["id"]))
                first = len([o for o in order if o[0] == "ask"]) == 1
            return Decision(not first, "non")

        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMAND, env=self.env,
                                  ask_permission=ask, on_event=on_event)
        self.assertEqual(execution.run(), "done")
        refused = order[0][1]
        other = next(sid for kind, sid in order if kind == "ask" and sid != refused)
        self.assertLess(order.index(("ask", other)), order.index(("done", refused)))

    def test_a_subtask_given_to_codex_runs_on_codex(self):
        gpt = store.ensure_agent(self.conn, "codex", "gpt-5.6-luna")
        self.use_plan(max_parallel=1, subtasks=[subtask("Un", "un", files=["1.txt"])])
        plan_id = self.ready_plan(agent=gpt)
        codex_log = self.make_tempdir() / "codex.json"
        self.env["FAKE_CODEX_LOG"] = str(codex_log)
        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMANDS, env=self.env)
        self.assertEqual(execution.run(), "done")
        sub = store.get_plan(self.conn, plan_id)["subtasks"][0]
        self.assertEqual((sub["status"], sub["result"], sub["provider"]), ("ok", "Bonjour", "codex"))
        sent = read_log(codex_log)
        self.assertEqual(sent["thread_params"]["model"], "gpt-5.6-luna")
        self.assertIn("Un", sent["prompt"])

    def test_a_change_of_several_files_claims_them_all(self):
        self.use_plan(max_parallel=2, subtasks=[subtask("Un", "un", files=["1.txt"]),
                                                subtask("Deux", "deux", files=["2.txt"])])
        plan_id = self.ready_plan()
        one, two = store.get_plan(self.conn, plan_id)["subtasks"]
        conversation = store.get_conversation(self.conn, self.cid)
        answers = iter([Decision(True), Decision(False, "non")])
        execution = PlanExecution(self.db_path, plan_id, command=FAKE_COMMAND, env=self.env,
                                  ask_permission=lambda sub, request: next(answers))
        both = PermissionRequest("Edit", {"file_path": "a.txt", "file_paths": ["a.txt", "b.txt"]}, "1")
        self.assertTrue(execution._ask(self.conn, conversation, one, both).allow)
        keys = {orchestrator._norm(str(self.workdir), p) for p in ("a.txt", "b.txt")}
        self.assertEqual({k for k, v in execution._claimed.items() if v == one["id"]}, keys)
        # The other agent asking for b.txt alone waits until the first is done.
        box = {}

        def ask_later():
            journal = db.connect(self.db_path)  # a worker's own connection, as in PlanExecution
            try:
                box["d"] = execution._ask(journal, conversation, two,
                                          PermissionRequest("Write", {"file_path": "b.txt"}, "2"))
            finally:
                journal.close()

        later = threading.Thread(target=ask_later)
        later.start()
        later.join(1.5)
        self.assertTrue(later.is_alive())
        execution._release(one["id"])
        later.join(10)
        self.assertFalse(box["d"].allow)  # refused: what it had taken is given back at once
        self.assertEqual(execution._claimed, {})

    def test_subtask_without_agent_cannot_start(self):
        plan_id, _ = self.propose()
        with self.assertRaisesRegex(StoreError, "sans agent"):
            store.validate_plan(self.conn, plan_id)


class AdaptTests(OrchestrationTestCase):
    def test_conductor_reassigns_after_an_agent_is_removed(self):
        plan_id, _ = self.propose()
        for st in store.get_plan(self.conn, plan_id)["subtasks"]:
            store.assign_subtask(self.conn, st["id"], self.b)
        store.remove_agent_from_plan(self.conn, plan_id, self.b)
        store.update_agent(self.conn, self.b, enabled=False)
        result = orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)
        self.assertEqual(result.status, "ok")
        plan = store.get_plan(self.conn, plan_id)
        # The fake conductor gives everything to the first agent offered:
        # enabled agents only, sorted by name.
        self.assertEqual({s["agent_id"] for s in plan["subtasks"]}, {self.a})
        self.assertEqual(plan["summary"], "Plan adapté aux agents disponibles.")
        prompt = read_log(self.log)["user_message"]["message"]["content"]
        self.assertIn("Agent A : modèle sonnet", prompt)
        self.assertNotIn("Agent B : modèle", prompt)
        self.assertIn("aucun agent", prompt)  # the current plan, as the user left it
        self.assertEqual(store.list_messages(self.conn, self.cid)[-1]["content"],
                         "Plan adapté : Plan adapté aux agents disponibles.")

    def test_validated_plan_cannot_be_adapted(self):
        plan_id = self.ready_plan()
        with self.assertRaisesRegex(StoreError, "ne peut plus être modifié"):
            orchestrator.adapt_plan(self.conn, plan_id, command=FAKE_COMMAND, env=self.env)


if __name__ == "__main__":
    unittest.main()
