import json
import os
import sqlite3
import unittest
from pathlib import Path

from aihub import config, db, memory, store
from aihub.runner import Request, resummarizer, summarizer
from tests.helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log

try:
    from tests.test_web import WebTestCase
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = None


class MemoryTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Logo", agent_id=self.agent, workdir=str(self.tmp))

    def say(self, text, reply="d'accord", status="ok", cid=None):
        cid = cid or self.cid
        store.add_user_message(self.conn, cid, text)
        mid = store.start_request(self.conn, cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, status, content=reply if status == "ok" else None,
                             error=None if status == "ok" else "boom")
        return mid

    def indexed(self):
        return [r[0] for r in self.conn.execute("SELECT content FROM memory ORDER BY rowid")]


class IndexTests(MemoryTestCase):
    def test_each_finished_message_enters_the_memory(self):
        store.add_user_message(self.conn, self.cid, "on garde le vert sapin")
        mid = store.start_request(self.conn, self.cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "réponse à moitié")
        self.assertEqual(self.indexed(), ["on garde le vert sapin"])  # not while it is streamed
        store.finish_message(self.conn, mid, "ok", content="Noté : vert sapin.")
        self.assertEqual(self.indexed(), ["on garde le vert sapin", "Noté : vert sapin."])
        self.say("autre chose", status="error")
        self.assertNotIn(None, self.indexed())
        self.assertEqual(len(self.indexed()), 3)  # the request, not the failed reply

    def test_subtask_reports_enter_too(self):
        self.cid = store.create_conversation(self.conn, "Plan", mode="orchestration", agent_id=self.agent,
                                             conductor_agent_id=self.agent, workdir=str(self.tmp))
        request = store.add_user_message(self.conn, self.cid, "fais le plan")
        pid = store.create_plan(self.conn, self.cid, request)
        sid = store.add_subtask(self.conn, pid, "écrire le texte")
        self.conn.execute("UPDATE subtasks SET result = 'texte écrit en bleu' WHERE id = ?", (sid,))
        row = self.conn.execute("SELECT conversation_id, role FROM memory WHERE subtask_id = ?", (sid,)).fetchone()
        self.assertEqual(tuple(row), (self.cid, "subtask"))

    def test_deleting_removes_from_the_memory(self):
        self.say("à effacer")
        other = store.create_conversation(self.conn, "Autre", workdir=str(self.tmp))
        self.say("à garder", cid=other)
        store.delete_conversation(self.conn, self.cid)
        self.assertEqual(self.indexed(), ["à garder", "d'accord"])

    def test_an_old_base_is_indexed_when_upgraded(self):
        path = str(self.tmp / "ancienne.db")
        old = sqlite3.connect(path, isolation_level=None)
        old.execute("PRAGMA foreign_keys = ON")
        for part in (db._SCHEMA_V1, db._SCHEMA_V2, db._SCHEMA_V3, db._SCHEMA_V4, db._SCHEMA_V5,
                     db._SCHEMA_V6, db._SCHEMA_V7):
            for statement in part:
                old.execute(statement)
        old.execute("PRAGMA user_version = 7")
        now = db.utc_now()
        old.execute("INSERT INTO conversations (id, title, mode, permission_mode, assignment_mode, created_at, "
                    "updated_at) VALUES (1, 't', 'single', 'manual', 'manual', ?, ?)", (now, now))
        old.execute("INSERT INTO messages (conversation_id, role, content, status, created_at) "
                    "VALUES (1, 'user', 'dit avant la mise à jour', 'ok', ?)", (now,))
        old.close()
        conn = db.connect(path)
        try:
            self.assertEqual([r[0] for r in conn.execute("SELECT content FROM memory")], ["dit avant la mise à jour"])
        finally:
            conn.close()


class SearchTests(MemoryTestCase):
    def test_every_word_accents_and_prefixes(self):
        self.say("Pour le logo, on a décidé du vert sapin")
        other = store.create_conversation(self.conn, "Budget", workdir=str(self.tmp))
        self.say("le budget est décidé", cid=other)
        found = memory.search(self.conn, "decide logo")  # no accent, a prefix of « décidé »
        self.assertEqual([(r["title"], r["role"]) for r in found], [("Logo", "user")])
        self.assertIn("[[logo]]", found[0]["excerpt"])
        self.assertEqual(len(memory.search(self.conn, "décidé")), 2)
        self.assertEqual(memory.search(self.conn, "le la de"), [])  # nothing but common words

    def test_typed_text_is_never_query_syntax(self):
        self.say("un texte quelconque")
        for text in ('"', 'NEAR(texte quelconque)', 'texte* OR -', "texte AND", "col:texte", "NOT texte",
                     "content:texte", "texte NEAR quelconque", "^texte", "{content} : texte", "texte + -quelconque"):
            with self.subTest(text=text):
                memory.search(self.conn, text)  # no sqlite3.OperationalError
        # Capitals do not become operators: « TEXTE NEAR » looks for two words.
        self.assertEqual(len(memory.search(self.conn, "TEXTE")), 1)
        self.assertEqual(memory.search(self.conn, "TEXTE NEAR"), [])

    def test_limit(self):
        for i in range(5):
            self.say(f"logo numéro {i}", reply="ok")
        self.assertEqual(len(memory.search(self.conn, "logo", limit=2)), 2)


class PackageTests(MemoryTestCase):
    def test_last_exchanges(self):
        self.say("un", "réponse un")
        self.say("deux", status="error")
        # A reply stopped midway keeps its partial text: not given as an exchange.
        store.add_user_message(self.conn, self.cid, "deux bis")
        mid = store.start_request(self.conn, self.cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "réponse coupée en plein milieu")
        store.finish_message(self.conn, mid, "stopped")
        self.assertNotIn("coupée", json.dumps(memory.last_exchanges(self.conn, self.cid, 5), ensure_ascii=False))
        self.say("trois", "réponse trois")
        self.assertEqual([e["content"] for e in memory.last_exchanges(self.conn, self.cid, 1)],
                         ["trois", "réponse trois"])
        self.assertEqual(memory.last_exchanges(self.conn, self.cid, 0), [])
        self.assertNotIn("boom", json.dumps(memory.last_exchanges(self.conn, self.cid, 5)))

    def test_passages(self):
        self.say("le logo sera vert sapin", "Noté pour le logo.")
        self.say("et la police Inter", "D'accord pour Inter.")
        found = memory.passages(self.conn, self.cid, "quelle couleur pour le logo ?", 2000)
        self.assertEqual({p["content"] for p in found}, {"le logo sera vert sapin", "Noté pour le logo."})
        first = self.conn.execute("SELECT id FROM messages WHERE content = 'le logo sera vert sapin'").fetchone()[0]
        self.assertNotIn("le logo sera vert sapin",
                         [p["content"] for p in memory.passages(self.conn, self.cid, "logo", 2000, exclude={first})])
        self.assertEqual(memory.passages(self.conn, self.cid, "logo", 5), [])  # within the budget

    def test_summary_is_rewritten_only_when_messages_came(self):
        calls = []

        def summarize(prompt):
            calls.append(prompt)
            return f"résumé {len(calls)}", 10, 5, "haiku"
        self.say("on garde le vert sapin")
        self.assertEqual(memory.refresh_summary(self.conn, self.cid, summarize), "résumé 1")
        self.assertEqual(memory.refresh_summary(self.conn, self.cid, summarize), "résumé 1")
        self.assertEqual(len(calls), 1)  # nothing new: no call, no quota
        self.say("et la police Inter")
        self.assertEqual(memory.refresh_summary(self.conn, self.cid, summarize), "résumé 2")
        self.assertIn("Résumé précédent :\nrésumé 1", calls[1])
        self.assertIn("police Inter", calls[1])
        self.assertNotIn("vert sapin", calls[1])  # only what came since
        saved = memory.get_summary(self.conn, self.cid)
        self.assertEqual((saved["model"], saved["tokens_in"]), ("haiku", 10))
        self.assertEqual(memory.refresh_summary(self.conn, self.cid, None), "résumé 2")

    def test_summary_prompt_keeps_the_newest_within_its_size(self):
        messages = [{"role": "user", "content": f"message {i} " + "x" * 4000} for i in range(30)]
        prompt = memory.summary_prompt("", messages)
        self.assertIn("message 29", prompt)
        self.assertNotIn("message 0 ", prompt)
        self.assertLess(len(prompt), memory.SUMMARY_INPUT + 2000)

    def test_over_budget_and_automatic_compression(self):
        exchanges = [{"role": "user", "content": "question " + "a" * 6000, "message_id": 1},
                     {"role": "assistant", "content": "réponse " + "b" * 6000, "message_id": 2},
                     {"role": "user", "content": "dernière question", "message_id": 3},
                     {"role": "assistant", "content": "dernière réponse " + "c" * 6000, "message_id": 4}]
        package = memory.base_package(self.conn, self.cid, summary="résumé " * 300, exchanges=exchanges, budget=2000)
        self.assertTrue(package["over"])
        shorter = []
        small = memory.shrink(package, 2000, lambda s, words: shorter.append(words) or "résumé court")
        self.assertFalse(small["over"])
        self.assertLessEqual(small["tokens"], 2000)
        self.assertEqual(small["summary"], "résumé court")  # asked to the summary AI first
        self.assertEqual(len(shorter), 1)
        self.assertEqual([e["message_id"] for e in small["exchanges"]], [3, 4])  # the oldest go first
        cut = memory.shrink(package, 2000)  # without the summary AI: cut
        self.assertFalse(cut["over"])

    def test_render_and_storage(self):
        package = {"summary": "le logo est vert sapin", "budget": 8000,
                   "exchanges": [{"role": "user", "content": "et la police ?", "message_id": 1}]}
        text = memory.render(package, [{"role": "assistant", "content": "Inter", "message_id": 2}], "quelle couleur ?")
        self.assertTrue(text.startswith("<memoire_de_la_conversation>"))
        for part in ("## Résumé\nle logo est vert sapin", "[Utilisateur] et la police ?", "- IA : Inter",
                     "search_this_conversation", "\n\nDemande :\nquelle couleur ?"):
            self.assertIn(part, text)
        memory.store_package(self.conn, self.cid, package)
        self.assertEqual(memory.load_package(store.get_conversation(self.conn, self.cid)), package)
        memory.store_package(self.conn, self.cid, None)
        self.assertIsNone(memory.load_package(store.get_conversation(self.conn, self.cid)))


class RunnerTests(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.logs = self.make_tempdir()
        self.env = fake_env("text", self.tmp / "log.json")
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.logs)

    def runs(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.logs.glob("run-*.json"),
                                                                          key=os.path.getmtime)]

    def request(self, prompt="et maintenant ?"):
        return Request(self.conn, self.cid, self.agent, prompt, command=FAKE_COMMAND, env=self.env).run()

    def test_an_asked_fresh_start(self):
        self.request("premier")  # a session to resume
        memory.store_package(self.conn, self.cid, memory.base_package(
            self.conn, self.cid, summary="on parle du logo", exchanges=[], budget=8000))
        result = self.request("quelle couleur ?")
        self.assertEqual((result.status, result.fresh_start), ("ok", "asked"))
        log = read_log(self.tmp / "log.json")
        self.assertNotIn("--resume", log["args"])  # a new session
        self.assertIn("## Résumé\non parle du logo", log["memory"])
        self.assertIn("--mcp-config", log["args"])  # its own past is searchable, even outside a project
        self.assertNotIn("## Passages", log["memory"])  # not the request itself, found in the memory
        self.assertIsNone(memory.load_package(store.get_conversation(self.conn, self.cid)))  # used once
        self.request("ensuite")
        self.assertIn("--resume", read_log(self.tmp / "log.json")["args"])  # resumed again afterwards

    def test_a_lost_session_starts_again_from_the_memory(self):
        self.request("on garde le vert sapin")
        self.env["FAKE_LOST_SESSION"] = "1"
        result = self.request("et le logo ?")
        self.assertEqual((result.status, result.fresh_start, result.text), ("ok", "automatic", "Bonjour"))
        runs = self.runs()
        self.assertIn("lost_session", runs[-3])  # the failed resume
        self.assertTrue(runs[-2]["summary_prompt"].startswith("Résume cette conversation"))  # the summary AI
        self.assertEqual(runs[-2]["summary_tools"], "")  # no tool at all for it
        self.assertIn("Résumé : on garde le vert sapin", runs[-1]["memory"])
        # The request, recorded before the resume failed, is neither summarized nor an exchange.
        self.assertNotIn("et le logo ?", runs[-2]["summary_prompt"])
        self.assertNotIn("et le logo ?", runs[-1]["memory"])
        self.assertNotIn("--resume", runs[-1]["args"])
        reply = store.list_messages(self.conn, self.cid)[-1]
        self.assertEqual((reply["status"], reply["content"]), ("ok", "Bonjour"))

    def test_a_failed_summary_does_not_stop_the_fresh_start(self):
        self.request("on garde le vert sapin")
        self.env["FAKE_LOST_SESSION"] = "1"
        self.env["FAKE_SUMMARY_FAIL"] = "1"
        result = self.request("et le logo ?")
        self.assertEqual((result.status, result.fresh_start), ("ok", "automatic"))
        memory_text = self.runs()[-1]["memory"]
        self.assertNotIn("## Résumé", memory_text)  # no summary, but the exchanges are there
        self.assertIn("on garde le vert sapin", memory_text)

    def test_without_a_summary_ai(self):
        self.request("on garde le vert sapin")
        self.env["FAKE_LOST_SESSION"] = "1"
        result = Request(self.conn, self.cid, self.agent, "et le logo ?", command=FAKE_COMMAND, env=self.env,
                         settings=config.validate({"summary_model": "aucun"})).run()
        self.assertEqual(result.status, "ok")
        self.assertFalse(any("summary_prompt" in r for r in self.runs()))

    def test_summarizer(self):
        self.assertIsNone(summarizer(FAKE_COMMAND, str(self.tmp), "aucun"))
        summarize = summarizer(FAKE_COMMAND, str(self.tmp), "haiku", env=self.env)
        text, tokens_in, tokens_out, model = summarize(memory.summary_prompt("", [{"role": "user", "content": "logo"}]))
        self.assertEqual((text, model), ("Résumé : logo", "haiku"))
        args = self.runs()[-1]["args"]
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        self.assertEqual(resummarizer(summarize)("un deux trois quatre", 2), "un deux")


if WebTestCase is not None:
    class FreshStartWebTests(WebTestCase):
        def setUp(self):
            super().setUp()
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())

        def fresh(self, cid, **body):
            return self.client.post(f"/api/conversations/{cid}/fresh-start", json=body)

        def test_prepare_then_use(self):
            cid = self.new_conversation()
            self.send(cid, "on garde le vert sapin")
            self.wait_idle(cid)
            r = self.fresh(cid).json()
            self.assertTrue(r["ready"])
            self.assertEqual(r["package"]["summary"], "Résumé : on garde le vert sapin")
            self.assertEqual(r["budget"], 8000)
            self.assertIsNotNone(self.conversation(cid)["conversation"]["fresh_package"])
            self.send(cid, "et le logo ?")
            self.wait_idle(cid)
            self.assertIn("Résumé : on garde le vert sapin", read_log(self.log)["memory"])
            self.assertIsNone(self.conversation(cid)["conversation"]["fresh_package"])

        def test_cancel(self):
            cid = self.new_conversation()
            self.fresh(cid)
            self.assertEqual(self.client.delete(f"/api/conversations/{cid}/fresh-start").json(), {"ready": False})
            self.assertIsNone(self.conversation(cid)["conversation"]["fresh_package"])

        def over_budget(self):
            cid = self.new_conversation()
            conn = db.connect(self.db_path)
            try:
                conn.execute("UPDATE conversations SET rag_budget = 1000 WHERE id = ?", (cid,))
            finally:
                conn.close()
            self.send(cid, "question " + "a" * 5000)
            self.wait_idle(cid)
            return cid

        def test_over_the_cap_nothing_is_kept_until_a_choice(self):
            cid = self.over_budget()
            r = self.fresh(cid).json()
            self.assertEqual((r["ready"], r["over"], r["budget"]), (False, True, 1000))
            self.assertIsNone(self.conversation(cid)["conversation"]["fresh_package"])

        def test_raise_the_cap(self):
            cid = self.over_budget()
            r = self.fresh(cid, decision="raise", budget=20000, remember=True).json()
            self.assertTrue(r["ready"])
            self.assertEqual(r["package"]["budget"], 20000)
            self.assertEqual(self.conversation(cid)["conversation"]["rag_budget"], 20000)  # remembered
            self.assertEqual(self.fresh(cid, decision="raise", budget=10).status_code, 400)

        def test_automatic_compression(self):
            cid = self.over_budget()
            r = self.fresh(cid, decision="auto").json()
            self.assertTrue(r["ready"])
            self.assertLessEqual(r["package"]["tokens"], 1000)

        def test_compress_by_hand(self):
            cid = self.over_budget()
            big = {"summary": "s " * 3000, "exchanges": [{"role": "user", "content": "q"}]}
            r = self.fresh(cid, decision="custom", package=big)
            self.assertEqual(r.status_code, 400)
            self.assertIn("plafond de 1000", r.json()["detail"])
            r = self.fresh(cid, decision="custom", package=big, allow_over=True).json()
            self.assertTrue(r["ready"] and r["over"])  # over it, but the user chose so
            small = {"summary": "le logo est vert", "exchanges": [{"role": "assistant", "content": "noté", "message_id": 3}]}
            r = self.fresh(cid, decision="custom", package=small).json()
            self.assertEqual(r["package"]["summary"], "le logo est vert")
            for bad in ({"summary": 3}, {"exchanges": [{"role": "système", "content": "x"}]}, {"exchanges": "x"}):
                with self.subTest(bad=bad):
                    self.assertEqual(self.fresh(cid, decision="custom", package=bad).status_code, 400)
            # Not even an object: refused by the request's own shape.
            self.assertEqual(self.fresh(cid, decision="custom", package="texte").status_code, 422)

        def test_unknown_choice_is_refused_before_any_summary(self):
            cid = self.new_conversation()
            self.send(cid, "quelque chose")
            self.wait_idle(cid)
            logs = Path(self.env["FAKE_CLAUDE_LOG_DIR"])
            before = len(list(logs.glob("run-*.json")))
            self.assertEqual(self.fresh(cid, decision="peut-être").status_code, 400)
            self.assertEqual(len(list(logs.glob("run-*.json"))), before)  # no summary written: no quota

        def test_not_while_running(self):
            self.use_scenario("hang")
            cid = self.new_conversation()
            self.send(cid)
            from tests.helpers import wait_for_log_key
            wait_for_log_key(self.log, "child_pid")
            self.assertEqual(self.fresh(cid).status_code, 409)
            self.client.post(f"/api/conversations/{cid}/stop")
            self.wait_idle(cid)

        def test_search(self):
            cid = self.new_conversation(title="Logo")
            self.send(cid, "le logo sera vert sapin")
            self.wait_idle(cid)
            [hit] = [r for r in self.client.get("/api/search?q=sapin").json()["results"] if r["role"] == "user"]
            self.assertEqual((hit["conversation_id"], hit["title"]), (cid, "Logo"))
            self.assertIn("[[sapin]]", hit["excerpt"])
            self.assertEqual(self.client.get("/api/search?q=").json(), {"results": []})


if __name__ == "__main__":
    unittest.main()
