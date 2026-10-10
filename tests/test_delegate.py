"""Delegation: an agent asks another AI for its opinion (aihub/delegate_mcp.py)."""

import json
import unittest

from aihub import config, db, delegate_mcp, routing, store, tools

from .helpers import FAKE_COMMANDS, TempDirMixin, fake_env, read_log


class DelegateTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "c", agent_id=agent, workdir=str(self.tmp))
        self.codex_log = self.tmp / "codex.json"
        env = fake_env("text", self.tmp / "claude.json", codex_log=self.codex_log)
        self._env = dict(os.environ)
        os.environ.update(env)  # the delegated CLI inherits the server's environment
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.delegate = delegate_mcp.Delegate(self.db_path, self.cid, ["haiku", "gpt-5.6-luna"], FAKE_COMMANDS)

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM delegations ORDER BY id")]

    def test_ask_another_ai(self):
        answer = self.delegate.ask("gpt-5.6-luna", "relis mon plan")
        self.assertEqual(answer, "Réponse de gpt-5.6-luna :\n\nBonjour")
        sent = read_log(self.codex_log)
        self.assertEqual(sent["thread_params"]["sandbox"], "read-only")  # it reads, it changes nothing
        self.assertIn("relis mon plan", sent["prompt"])
        self.assertNotIn("deleguer", json.dumps(sent["thread_params"]))  # no delegation of its own
        row = self.rows()[0]
        self.assertEqual((row["status"], row["model"], row["provider"], row["answer"]),
                         ("ok", "gpt-5.6-luna", "codex", "Bonjour"))

    def test_claude_answers_with_read_only_tools(self):
        self.delegate.ask("haiku", "un avis ?")
        args = read_log(self.tmp / "claude.json")["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")

    def test_limits(self):
        with self.assertRaisesRegex(delegate_mcp.Refused, "non proposé"):
            self.delegate.ask("opus", "x")
        with self.assertRaisesRegex(delegate_mcp.Refused, "vide"):
            self.delegate.ask("haiku", "  ")
        with self.assertRaisesRegex(delegate_mcp.Refused, "trop longue"):
            self.delegate.ask("haiku", "x" * (delegate_mcp.QUESTION_MAX + 1))
        for _ in range(delegate_mcp.MAX_CALLS):
            self.delegate.ask("haiku", "encore")
        with self.assertRaisesRegex(delegate_mcp.Refused, "déjà 3 demandes"):
            self.delegate.ask("haiku", "une de plus")
        self.assertEqual(len(self.rows()), delegate_mcp.MAX_CALLS)

    def test_a_failed_answer_is_said(self):
        import os
        os.environ["FAKE_CODEX_SCENARIO"] = "failed"
        with self.assertRaisesRegex(delegate_mcp.Refused, "n'a pas pu répondre"):
            self.delegate.ask("gpt-5.6-luna", "x")
        self.assertEqual(self.rows()[0]["status"], "error")

    def test_protocol(self):
        listed = delegate_mcp.handle(self.delegate, {"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(listed[0]["inputSchema"]["properties"]["model"]["enum"], ["haiku", "gpt-5.6-luna"])
        reply = delegate_mcp.handle(self.delegate, {"id": 2, "method": "tools/call", "params": {
            "name": "ask_ai", "arguments": {"model": "inconnu", "question": "x"}}})
        self.assertTrue(reply["result"]["isError"])


class GivenTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "c", agent_id=agent, workdir=str(self.tmp))
        self.addCleanup(routing.set_available, {"claude_code": None})

    def test_given_when_chosen(self):
        settings = config.Settings()
        conv = store.get_conversation(self.conn, self.cid)
        self.assertIsNone(tools.for_run(self.conn, conv, settings).mcp_config)
        store.set_tools(self.conn, self.cid, ["deleguer"])
        routing.set_available({"claude_code": None, "codex": {"gpt-6-astra"}})
        given = tools.for_run(self.conn, store.get_conversation(self.conn, self.cid), settings)
        spec = given.mcp_config["mcpServers"]["deleguer"]
        models = spec["args"][spec["args"].index("--models") + 1].split(",")
        self.assertIn("opus", models)
        self.assertIn("gpt-6-astra", models)
        self.assertNotIn("gpt-6-sol", models)  # not offered by this Codex
        self.assertIn("mcp__deleguer__ask_ai", given.auto_allow)
        self.assertEqual(given.env["MCP_TOOL_TIMEOUT"], "900000")
        self.assertEqual(spec["tool_timeout_sec"], 900)

    def test_a_request_raises_claudes_tool_delay(self):
        from aihub.runner import Request

        from .helpers import FAKE_COMMAND
        store.set_tools(self.conn, self.cid, ["deleguer"])
        log = self.tmp / "log.json"
        agent = store.get_conversation(self.conn, self.cid)["agent_id"]
        Request(self.conn, self.cid, agent, "salut", command=FAKE_COMMAND, env=fake_env("text", log)).run()
        sent = read_log(log)
        self.assertEqual(sent["mcp_tool_timeout"], "900000")
        self.assertIn("deleguer", sent["args"][sent["args"].index("--mcp-config") + 1])

    def test_claude_gets_only_the_keys_it_knows(self):
        from aihub.providers import claude_code
        run = claude_code.ClaudeCodeRun(["c"], prompt="x", workdir=str(self.tmp), permission_mode="plan",
                                        mcp_config={"mcpServers": {"deleguer": {
                                            "type": "stdio", "command": "py", "args": [], "tool_timeout_sec": 900}}})
        args = run.arguments()
        self.assertNotIn("tool_timeout_sec", args[args.index("--mcp-config") + 1])


if __name__ == "__main__":
    unittest.main()
