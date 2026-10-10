import json
import re
import sqlite3
import subprocess
import unittest
from datetime import date
from pathlib import Path

from aihub import backup, db, project_access, project_mcp, store
from aihub.providers.claude_code import ClaudeCodeRun, Decision, RunResult
from tests.helpers import FAKE_COMMAND, TempDirMixin, read_log

try:
    from tests.test_web import WebTestCase
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = None


class StoreTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")

    def conv(self, title, project=None, shared=True):
        cid = store.create_conversation(self.conn, title, agent_id=self.agent, workdir=str(self.tmp),
                                        project_id=project)
        if not shared:
            store.set_shared(self.conn, cid, False)
        return cid

    def say(self, cid, text, reply="d'accord", status="ok"):
        store.add_user_message(self.conn, cid, text)
        mid = store.start_request(self.conn, cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, status, content=reply if status == "ok" else None,
                             error=None if status == "ok" else "boom")


class ProjectStoreTests(StoreTestCase):
    def test_create_list_update(self):
        pid = store.create_project(self.conn, "  Boutique ", description=" le site ", workdir=str(self.tmp))
        self.conv("a", pid)
        self.conv("b")
        [row] = store.list_projects(self.conn)
        self.assertEqual((row["name"], row["description"], row["conversations"]), ("Boutique", "le site", 1))
        with self.assertRaisesRegex(store.StoreError, "existe déjà"):
            store.create_project(self.conn, "Boutique")
        other = store.create_project(self.conn, "Autre")
        with self.assertRaisesRegex(store.StoreError, "existe déjà"):
            store.update_project(self.conn, other, name="Boutique")
        store.update_project(self.conn, pid, description="", workdir="")
        project = store.get_project(self.conn, pid)
        self.assertEqual((project["description"], project["workdir"]), (None, None))
        with self.assertRaisesRegex(store.StoreError, "nom du projet vide"):
            store.create_project(self.conn, " ")

    def test_delete_keeps_conversations_by_default(self):
        pid = store.create_project(self.conn, "P")
        cid = self.conv("a", pid)
        self.assertEqual(store.delete_project(self.conn, pid), [])
        self.assertIsNone(store.get_conversation(self.conn, cid)["project_id"])
        with self.assertRaises(store.NotFound):
            store.get_project(self.conn, pid)

    def test_delete_with_conversations(self):
        pid = store.create_project(self.conn, "P")
        a, b = self.conv("a", pid), self.conv("b", pid)
        loose = self.conv("hors projet")
        self.say(a, "secret")
        self.assertEqual(sorted(store.delete_project(self.conn, pid, with_conversations=True)), sorted([a, b]))
        self.assertEqual([c["id"] for c in store.list_conversations(self.conn)], [loose])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_delete_with_a_running_conversation_is_refused(self):
        pid = store.create_project(self.conn, "P")
        cid = self.conv("a", pid)
        store.add_user_message(self.conn, cid, "en cours")
        store.start_request(self.conn, cid, self.agent)
        with self.assertRaisesRegex(store.Busy, "requête en cours"):
            store.delete_project(self.conn, pid, with_conversations=True)
        self.assertEqual(store.get_project(self.conn, pid)["name"], "P")  # nothing deleted

    def test_move_and_share(self):
        pid = store.create_project(self.conn, "P")
        cid = self.conv("a")
        store.move_conversation(self.conn, cid, pid)
        self.assertEqual(store.get_conversation(self.conn, cid)["project_id"], pid)
        store.move_conversation(self.conn, cid, None)
        self.assertIsNone(store.get_conversation(self.conn, cid)["project_id"])
        with self.assertRaises(store.NotFound):
            store.move_conversation(self.conn, cid, 999)
        self.assertEqual(store.get_conversation(self.conn, cid)["shared"], 1)  # shared by default
        store.set_shared(self.conn, cid, False)
        self.assertEqual(store.get_conversation(self.conn, cid)["shared"], 0)

    def test_a_project_removed_behind_the_store_frees_its_conversations(self):
        pid = store.create_project(self.conn, "P")
        cid = self.conv("a", pid)
        self.conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        self.assertIsNone(store.get_conversation(self.conn, cid)["project_id"])  # SET NULL, not deleted


class AccessTests(StoreTestCase):
    """What the MCP server lets an agent read."""

    def setUp(self):
        super().setUp()
        self.project = store.create_project(self.conn, "Boutique")
        self.me = self.conv("Page d'accueil", self.project)
        self.peer = self.conv("Couleurs", self.project)
        self.say(self.peer, "On garde le BLEU nuit pour le logo", "Noté : bleu nuit.")
        self.private = self.conv("Budget", self.project, shared=False)
        self.say(self.private, "bleu nuit confidentiel")
        elsewhere = store.create_project(self.conn, "Autre")
        self.outside = self.conv("Ailleurs", elsewhere)
        self.say(self.outside, "bleu nuit d'un autre projet")
        self.loose = self.conv("Sans projet")
        self.say(self.loose, "bleu nuit sans projet")

    def access(self, cid=None):
        return project_mcp.Access(self.db_path, cid or self.me)

    def test_list_shows_only_the_shared_conversations_of_the_project(self):
        text = self.access().list()
        self.assertIn(f"#{self.peer} « Couleurs » — 2 message(s)", text)
        self.assertIn("première demande : On garde le BLEU nuit pour le logo", text)
        for hidden in ("Budget", "Ailleurs", "Sans projet", "Page d'accueil"):
            self.assertNotIn(hidden, text)

    def test_search(self):
        text = self.access().search("bleu NUIT")
        self.assertIn(f"conversation #{self.peer} « Couleurs », message 1 (utilisateur)", text)
        self.assertIn(f"conversation #{self.peer} « Couleurs », message 2 (IA)", text)
        for hidden in ("confidentiel", "autre projet", "sans projet"):
            self.assertNotIn(hidden, text)
        self.assertIn("Rien trouvé", self.access().search("rouge vermillon"))
        self.assertIn("1 passage(s)", self.access().search("bleu", limit=1))
        for empty in ("  ", "le la de", "et ou 12"):  # nothing but common words
            with self.subTest(query=empty), self.assertRaisesRegex(project_mcp.Refused, "au moins un mot"):
                self.access().search(empty)

    def test_a_passage_comes_with_its_question_or_answer(self):
        text = self.access().search("noté")
        self.assertIn("message 2 (IA) : Noté : bleu nuit.", text)
        self.assertIn("← en réponse à : On garde le BLEU nuit pour le logo", text)
        text = self.access().search("garde")
        self.assertIn("message 1 (utilisateur)", text)
        self.assertIn("→ réponse : Noté : bleu nuit.", text)

    def test_ranking_resists_a_wordy_message(self):
        # 20 times "logo" in a long message must not beat the short decision.
        self.say(self.peer, "Polices et marges, sans rapport avec le logo. " * 20, "Réponse. " * 40)
        self.say(self.peer, "Pour le logo on a choisi la couleur vert sapin")
        text = self.access().search("Quelle couleur avons-nous choisie pour le logo ?")
        first = text.split("\n- ")[1]
        self.assertIn("vert sapin", first)
        self.assertNotIn("Polices et marges", text)  # far behind the best: left out

    def test_terms(self):
        self.assertEqual(project_mcp.terms("Les COULEURS choisies du logo, 12 fois"),
                         ["couleur", "choisi", "logo", "fois"])  # short words keep their s
        self.assertEqual(project_mcp.terms("la conversation de ce projet"), [])

    def test_cited(self):
        access = self.access()
        self.assertEqual(list(access.cited(f"regarde #{self.peer}")), [self.peer])
        self.assertEqual(list(access.cited("comme dans couleurs")), [self.peer])  # by title, any case
        # Named but not readable: never joined.
        self.assertEqual(access.cited(f"#{self.private} #{self.outside} Budget Ailleurs #{self.me}"), {})
        self.assertEqual(self.access(self.loose).cited(f"#{self.peer}"), {})

    def test_relevant_and_its_fallback(self):
        self.say(self.peer, "Et la police sera Inter", "D'accord pour Inter.")
        text = self.access().relevant(self.peer, "quelle police ?", 4000)
        self.assertTrue(text.startswith(f"Conversation #{self.peer} « Couleurs » (4 messages) :"))
        self.assertIn("Inter", text)
        self.assertNotIn("bleu", text.casefold())
        tail = self.access().relevant(self.peer, "xyzzy", 4000)  # nothing in common: the last exchanges
        self.assertIn("D'accord pour Inter.", tail)
        self.assertEqual(self.access().relevant(self.private, "bleu", 4000), "")
        self.assertLessEqual(len(self.access().relevant(self.peer, "police", 300)), 360)

    def test_closest_needs_two_words_in_common(self):
        self.assertIn("bleu nuit", self.access().closest("le bleu pour le logo ?", 1500).casefold())
        self.assertEqual(self.access().closest("un logo animé", 1500), "")  # one word in common
        self.assertEqual(self.access(self.private).closest("bleu logo", 1500), "")

    def test_read_in_slices(self):
        for i in range(5):
            self.say(self.peer, f"question {i} " + "x" * 400, f"réponse {i}")
        text = self.access().read(self.peer, max_chars=1000)
        self.assertTrue(text.startswith(f"Conversation #{self.peer} « Couleurs » — messages 1 à "))
        self.assertIn("[1] Utilisateur", text)
        self.assertIn("(Suite : read_conversation avec start=", text)
        following = int(re.search(r"avec start=(\d+)", text).group(1))
        self.assertNotIn(f"[{following}]", text)  # the next slice starts where this one stopped
        self.assertIn(f"[{following - 1}]", text)
        follow = self.access().read(self.peer, start=following, max_chars=20000)
        self.assertIn(f"[{following}]", follow)
        self.assertNotIn("Suite", follow)
        self.assertIn("aucun message à partir du numéro 99", self.access().read(self.peer, start=99))

    def test_one_long_message_is_cut_not_dropped(self):
        self.say(self.peer, "y" * 5000)
        text = self.access().read(self.peer, start=3, max_chars=800)
        self.assertIn("[…coupé]", text)
        self.assertLess(len(text), 1200)

    def test_read_refusals_say_nothing_about_the_others(self):
        answers = set()
        for cid in (self.private, self.outside, self.loose, 999):
            with self.assertRaises(project_mcp.Refused) as caught:
                self.access().read(cid)
            answers.add(str(caught.exception).replace(str(cid), "N"))
        self.assertEqual(len(answers), 1)  # the same words whatever the reason
        with self.assertRaisesRegex(project_mcp.Refused, "conversation en cours"):
            self.access().read(self.me)

    def test_failed_replies_are_not_shown(self):
        self.say(self.peer, "question perdue", status="error")
        # A reply still being written already has text: it is not reliable yet.
        store.add_user_message(self.conn, self.peer, "question en cours")
        mid = store.start_request(self.conn, self.peer, self.agent)
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "réponse à moitié écrite")
        text = self.access().read(self.peer)
        self.assertIn("question en cours", text)
        self.assertNotIn("à moitié", text)
        self.assertIn("question perdue", text)
        self.assertNotIn("boom", text)
        self.assertEqual(text.count("[IA"), 0)  # role labels: "IA (" only
        self.assertEqual(text.count("] IA ("), 1)

    def test_no_project_or_not_shared_sees_nothing(self):
        with self.assertRaisesRegex(project_mcp.Refused, "aucun projet"):
            self.access(self.loose).list()
        with self.assertRaisesRegex(project_mcp.Refused, "partage est coupé"):
            self.access(self.private).search("bleu")

    def test_rules_are_read_again_at_each_call(self):
        access = self.access()
        self.assertIn("Couleurs", access.list())
        store.set_shared(self.conn, self.peer, False)  # closed while the agent works
        self.assertNotIn("Couleurs", access.list())
        store.move_conversation(self.conn, self.me, None)
        with self.assertRaises(project_mcp.Refused):
            access.list()

    def test_the_database_is_opened_read_only(self):
        conn = self.access()._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")
        finally:
            conn.close()


class ProtocolTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        pid = store.create_project(self.conn, "P")
        self.me = self.conv("moi", pid)
        self.peer = self.conv("l'autre", pid)
        self.say(self.peer, "le mot magique")

    def rpc(self, method, params=None, mid=1):
        return project_mcp.handle(project_mcp.Access(self.db_path, self.me),
                                  {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_handshake_and_tools(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertEqual(init["serverInfo"]["name"], "aihub")
        self.assertIn("tools", init["capabilities"])
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["list_project_conversations", "search_project", "read_conversation",
                                 "search_this_conversation", "read_this_conversation"])
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertIsNone(project_mcp.handle(None, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)

    def test_calls(self):
        found = self.rpc("tools/call", {"name": "search_project", "arguments": {"query": "magique"}})["result"]
        self.assertFalse(found["isError"])
        self.assertIn("le mot magique", found["content"][0]["text"])
        refused = self.rpc("tools/call", {"name": "read_conversation", "arguments": {"conversation_id": "x"}})
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("numéro", refused["result"]["content"][0]["text"])
        unknown = self.rpc("tools/call", {"name": "write_file", "arguments": {}})["result"]
        self.assertTrue(unknown["isError"])

    def test_over_stdio_with_the_hubs_launch(self):
        config = project_access.mcp_config(self.conn, store.get_conversation(self.conn, self.me))
        server = config["mcpServers"]["aihub"]
        self.assertEqual(server["args"][:2], ["-I", str(project_access.SERVER_SCRIPT)])
        proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                cwd=str(self.make_tempdir()))  # like the agent: another folder
        try:
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_project", "arguments": {"query": "mot magique"}}}]
            out, _ = proc.communicate("\n".join(json.dumps(m, ensure_ascii=False) for m in lines) + "\n", timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        replies = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertIn("le mot magique", replies[1]["result"]["content"][0]["text"])


class ProjectAccessTests(StoreTestCase):
    def test_config_only_for_a_shared_conversation_in_a_project(self):
        pid = store.create_project(self.conn, "Boutique")
        shared = self.conv("a", pid)
        private = self.conv("b", pid, shared=False)
        loose = self.conv("c")
        get = lambda cid: store.get_conversation(self.conn, cid)  # noqa: E731
        config = project_access.mcp_config(self.conn, get(shared))
        args = config["mcpServers"]["aihub"]["args"]
        self.assertEqual(args[args.index("--conversation") + 1], str(shared))
        self.assertEqual(Path(args[args.index("--db") + 1]).resolve(), Path(self.db_path).resolve())
        self.assertIsNone(project_access.mcp_config(self.conn, get(private)))
        self.assertIsNone(project_access.mcp_config(self.conn, get(loose)))
        self.assertIsNone(project_access.instructions(self.conn, get(loose)))
        hint = project_access.instructions(self.conn, get(shared))
        self.assertIn("« Boutique »", hint)
        self.assertIn("pas encore d'autre conversation partagée", hint)

    def test_the_instruction_names_the_other_conversations(self):
        pid = store.create_project(self.conn, "Boutique")
        me = self.conv("moi", pid)
        peer = self.conv("Couleurs", pid)
        self.conv("Budget", pid, shared=False)
        hint = project_access.instructions(self.conn, store.get_conversation(self.conn, me))
        self.assertIn(f"#{peer} « Couleurs »", hint)
        self.assertNotIn("Budget", hint)
        self.assertNotIn("« moi »", hint)
        for i in range(project_access.CATALOG):
            self.conv(f"c{i}", pid)
        hint = project_access.instructions(self.conn, store.get_conversation(self.conn, me))
        self.assertIn("(et d'autres).", hint)

    def test_prefetch(self):
        pid = store.create_project(self.conn, "Boutique")
        me = self.conv("moi", pid)
        peer = self.conv("Couleurs", pid)
        self.say(peer, "Pour le logo on garde le vert sapin")
        conv = store.get_conversation(self.conn, me)
        cited = project_access.prefetch(self.conn, conv, f"que dit #{peer} ?")
        self.assertIn("<extraits_du_projet>", cited)
        self.assertIn("Passages des conversations citées", cited)
        self.assertIn("vert sapin", cited)
        auto = project_access.prefetch(self.conn, conv, "quel vert pour le logo ?")
        self.assertIn("semblent liés", auto)
        self.assertEqual(project_access.prefetch(self.conn, conv, "quel vert pour le logo ?", automatic=False), "")
        self.assertIn("vert sapin", project_access.prefetch(self.conn, conv, "et dans Couleurs ?", automatic=False))
        self.assertEqual(project_access.prefetch(self.conn, conv, "écris une fonction de tri"), "")
        store.set_shared(self.conn, me, False)
        self.assertEqual(project_access.prefetch(self.conn, store.get_conversation(self.conn, me), f"#{peer}"), "")
        memory = db.connect(":memory:")
        self.addCleanup(memory.close)
        self.assertIsNone(project_access.database_path(memory))


class ProviderTests(unittest.TestCase):
    def run_with(self, **kwargs):
        return ClaudeCodeRun(FAKE_COMMAND, prompt="x", workdir=".", permission_mode="manual", **kwargs)

    def test_arguments(self):
        config = {"mcpServers": {"aihub": {"type": "stdio", "command": "py", "args": []}}}
        args = self.run_with(mcp_config=config, auto_allow=project_access.TOOL_NAMES).arguments()
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), config)
        self.assertEqual(args[args.index("--allowedTools") + 1].split(","), project_access.TOOL_NAMES)
        self.assertIn("--strict-mcp-config", args)  # still only the hub's servers
        plain = self.run_with().arguments()
        self.assertNotIn("--mcp-config", plain)
        self.assertNotIn("--allowedTools", plain)

    def test_turns_and_tool_calls_are_measured(self):
        tmp = Path(__file__).resolve().parent
        env = dict(__import__("os").environ, FAKE_CLAUDE_SCENARIO="text")
        result = ClaudeCodeRun(FAKE_COMMAND, prompt="x", workdir=str(tmp), permission_mode="manual",
                               env=env).run()
        self.assertEqual(result.num_turns, 1)
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.usage, {"input_tokens": 12, "cache_creation_input_tokens": 100,
                                        "cache_read_input_tokens": 30, "output_tokens": 7})

    def test_tool_calls_are_read_from_the_stream(self):
        class Proc:  # the CLI's output, as the real one writes it
            stdout = iter(json.dumps(e) + "\n" for e in (
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "je cherche"},
                    {"type": "tool_use", "id": "t1", "name": "ToolSearch", "input": {}}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "t2", "name": "mcp__aihub__search_project", "input": {"query": "x"}}]}},
                {"type": "assistant", "message": {"content": "pas une liste"}},
                {"type": "result", "result": "fini", "num_turns": 3, "usage": {"input_tokens": 4}}))
        result = RunResult(status="error")
        self.run_with()._read_events(Proc(), result)
        self.assertEqual(result.tool_calls, ["ToolSearch", "mcp__aihub__search_project"])
        self.assertEqual((result.num_turns, result.usage), (3, {"input_tokens": 4}))

    def test_hub_tools_are_allowed_without_asking_and_nothing_else(self):
        asked = []
        run = self.run_with(auto_allow=["mcp__aihub__search_project"],
                            ask_permission=lambda r: asked.append(r.tool_name) or Decision(False, "non"))
        sent = []
        run._send = lambda proc, payload: sent.append(payload)
        for i, tool in enumerate(("mcp__aihub__search_project", "Write")):
            run._answer_permission(None, {"request_id": f"r{i}", "request": {
                "subtype": "can_use_tool", "tool_name": tool, "input": {"q": 1}}}, RunResult(status="error"))
        behaviors = [p["response"]["response"]["behavior"] for p in sent]
        self.assertEqual(behaviors, ["allow", "deny"])
        self.assertEqual(asked, ["Write"])


if WebTestCase is not None:
    class ProjectWebTests(WebTestCase):
        scenario = "auto"

        def setUp(self):
            super().setUp()
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())
            self.logs = Path(self.env["FAKE_CLAUDE_LOG_DIR"])

        def project(self, name="Boutique", **body):
            r = self.client.post("/api/projects", json={"name": name, "workdir": str(self.workdir), **body})
            self.assertEqual(r.status_code, 201, r.text)
            return r.json()["project"]["id"]

        def in_project(self, pid, title, **body):
            r = self.client.post("/api/conversations", json={"title": title, "project_id": pid, **body})
            self.assertEqual(r.status_code, 201, r.text)
            return r.json()["conversation"]["id"]

        def reply(self, cid, prompt):
            self.send(cid, prompt)
            return self.wait_idle(cid)["messages"][-1]

        def test_conversation_reads_another_of_its_project(self):
            pid = self.project()
            colors = self.in_project(pid, "Couleurs")
            self.reply(colors, "On garde le bleu nuit pour le logo")
            home = self.in_project(pid, "Accueil")
            answer = self.reply(home, "consulte cherche bleu nuit")
            self.assertEqual(answer["status"], "ok")
            self.assertIn(f"conversation #{colors} « Couleurs », message 1", answer["content"])
            log = read_log(self.log)
            self.assertEqual(log["mcp_tools"], ["list_project_conversations", "search_project", "read_conversation",
                                                "search_this_conversation", "read_this_conversation"])
            self.assertFalse(log["mcp_call"]["is_error"])
            self.assertEqual(log["answers"], [])  # allowed without asking the user
            args = log["args"]
            self.assertIn("« Boutique »", args[args.index("--append-system-prompt") + 1])
            read = self.reply(home, f"consulte lis {colors}")
            self.assertIn("[1] Utilisateur", read["content"])
            self.assertIn("On garde le bleu nuit", read["content"])

        def test_automatic_passages_only_for_what_the_user_typed(self):
            pid = self.project()
            colors = self.in_project(pid, "Couleurs")
            self.reply(colors, "Pour le logo on garde le vert sapin")
            # A plain conversation: the request shares words with it, passages are joined.
            single = self.in_project(pid, "Accueil")
            self.reply(single, "dis bonjour, et le logo vert ?")
            self.assertIn("vert sapin", read_log(self.log)["joined"])
            # The conductor's planning prompt is built by the hub: nothing automatic.
            for run in self.logs.glob("run-*.json"):
                run.unlink()
            plan = self.in_project(pid, "Plan", mode="orchestration", conductor_model="opus")
            self.send(plan, "fais le logo vert")
            self.wait_idle(plan)
            runs = [json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json")]
            planning = [r for r in runs if "--json-schema" in r["args"]]
            self.assertEqual(len(planning), 1)
            self.assertNotIn("joined", planning[0])

        def test_cited_passages_are_sent_not_recorded(self):
            pid = self.project()
            colors = self.in_project(pid, "Couleurs")
            self.reply(colors, "On garde le bleu nuit pour le logo")
            home = self.in_project(pid, "Accueil")
            self.reply(home, f"dis bonjour à propos de #{colors}")
            log = read_log(self.log)
            self.assertIn("On garde le bleu nuit", log["joined"])  # sent to the agent
            recorded = self.conversation(home)["messages"][0]["content"]
            self.assertEqual(recorded, f"dis bonjour à propos de #{colors}")  # not copied into this one

        def test_no_access_outside_a_project_or_when_private(self):
            loose = self.new_conversation(title="Seule")
            self.assertEqual(self.reply(loose, "consulte liste")["content"], "aucun accès aux autres conversations")
            self.assertNotIn("--mcp-config", read_log(self.log)["args"])
            pid = self.project()
            secret = self.in_project(pid, "Budget")
            self.reply(secret, "le budget est de mille euros")
            self.client.patch(f"/api/conversations/{secret}", json={"shared": False})
            other = self.in_project(pid, "Accueil")
            self.assertIn("Rien trouvé", self.reply(other, "consulte cherche budget")["content"])
            self.client.patch(f"/api/conversations/{other}", json={"shared": False})
            self.assertEqual(self.reply(other, "consulte liste")["content"], "aucun accès aux autres conversations")

        def test_moving_changes_what_is_seen(self):
            first, second = self.project("Un"), self.project("Deux")
            a = self.in_project(first, "A")
            self.reply(a, "le mot secret du projet un")
            b = self.in_project(second, "B")
            self.assertIn("Rien trouvé", self.reply(b, "consulte cherche secret")["content"])
            r = self.client.patch(f"/api/conversations/{b}", json={"project_id": first})
            self.assertEqual(r.json()["conversation"]["project_id"], first)
            self.assertIn("le mot secret", self.reply(b, "consulte cherche secret")["content"])
            self.client.patch(f"/api/conversations/{b}", json={"leave_project": True})
            self.assertIsNone(self.conversation(b)["conversation"]["project_id"])

        def test_subtasks_and_conductor_get_the_access_too(self):
            pid = self.project()
            cid = self.in_project(pid, "Plan", mode="orchestration", conductor_model="opus", assignment_mode="auto")
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.assertEqual(self.wait_idle(cid)["plans"][-1]["status"], "done")
            runs = [json.loads(p.read_text(encoding="utf-8")) for p in self.logs.glob("run-*.json")]
            self.assertGreaterEqual(len(runs), 5)  # conductor, 3 subtasks, synthesis
            self.assertTrue(all("--mcp-config" in run["args"] for run in runs))

        def test_project_folder_is_the_default(self):
            pid = self.project()
            cid = self.in_project(pid, "x")
            self.assertEqual(self.conversation(cid)["conversation"]["workdir"], str(self.workdir.resolve()))
            bare = self.client.post("/api/projects", json={"name": "Sans dossier"}).json()["project"]["id"]
            r = self.client.post("/api/conversations", json={"title": "y", "project_id": bare})
            self.assertEqual(r.status_code, 400)
            self.assertIn("dossier de travail", r.json()["detail"])
            r = self.client.post("/api/projects", json={"name": "Mauvais", "workdir": "relatif"})
            self.assertEqual(r.status_code, 400)

        def test_project_api(self):
            pid = self.project(description="le site")
            self.in_project(pid, "a")
            [row] = self.client.get("/api/projects").json()["projects"]
            self.assertEqual((row["name"], row["conversations"]), ("Boutique", 1))
            r = self.client.patch(f"/api/projects/{pid}", json={"name": "Boutique v2", "workdir": ""})
            self.assertEqual((r.json()["project"]["name"], r.json()["project"]["workdir"]), ("Boutique v2", None))
            data = self.client.get(f"/api/projects/{pid}").json()
            self.assertEqual([c["title"] for c in data["conversations"]], ["a"])
            self.assertEqual(self.client.get("/api/projects/999").status_code, 404)

        def test_delete_project_keeps_or_deletes_conversations(self):
            pid = self.project("Garde")
            kept = self.in_project(pid, "gardée")
            r = self.client.delete(f"/api/projects/{pid}")
            self.assertEqual(r.json()["conversations"], [])
            self.assertIsNone(self.conversation(kept)["conversation"]["project_id"])
            pid = self.project("Efface")
            gone = self.in_project(pid, "effacée")
            r = self.client.delete(f"/api/projects/{pid}?with_conversations=true")
            self.assertEqual(r.json()["conversations"], [gone])
            self.assertEqual(self.client.get(f"/api/conversations/{gone}").status_code, 404)

    class ProjectBackupTests(WebTestCase):
        def setUp(self):
            self.backups = None
            super().setUp()

        def test_deleted_project_leaves_the_backups(self):
            folder = self.make_tempdir()
            from aihub.web.app import create_app
            from fastapi.testclient import TestClient
            app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, backup_dir=str(folder))
            with TestClient(app) as client:
                pid = client.post("/api/projects", json={"name": "Projet-tres-secret-91"}).json()["project"]["id"]
                cid = client.post("/api/conversations", json={"title": "Conversation-secrete-92", "project_id": pid,
                                                              "workdir": str(self.workdir)}).json()["conversation"]["id"]
                backup.backup_now(self.db_path, folder, today=date(2020, 1, 1))
                old = folder / "aihub-2020-01-01.db"
                self.assertIn(b"Projet-tres-secret-91", old.read_bytes())
                r = client.delete(f"/api/projects/{pid}?with_conversations=true")
                self.assertEqual(r.json()["backup_errors"], {})
                self.assertEqual(r.json()["conversations"], [cid])
            raw = old.read_bytes()
            self.assertNotIn(b"Projet-tres-secret-91", raw)
            self.assertNotIn(b"Conversation-secrete-92", raw)


class OldBackupTests(TempDirMixin, unittest.TestCase):
    def test_purging_a_project_skips_copies_without_projects(self):
        folder = self.make_tempdir()
        old = sqlite3.connect(folder / "aihub-2020-01-01.db")
        old.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
        old.commit()
        old.close()
        self.assertEqual(backup.purge_project(folder, 1), {})


if __name__ == "__main__":
    unittest.main()
