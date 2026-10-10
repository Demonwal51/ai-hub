"""Codex provider (phase 9), against the fake app server of tests/fake_codex.py."""

import json
import threading
import time
import unittest
import unittest.mock

from aihub import config, db, providers, routing, store
from aihub.providers import codex
from aihub.providers.claude_code import Decision, ProviderError
from aihub.runner import Request, summarizer, summary_model

from .helpers import (FAKE_CODEX, FAKE_COMMAND, FAKE_COMMANDS, TempDirMixin, fake_env, read_log,
                      wait_dead, wait_for_log_key)


class CodexBase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.home = self.tmp / "codex-home"  # no config.toml of the user's
        self.home.mkdir()
        self.log = self.tmp / "codex.json"
        self.env = fake_env("hello", self.tmp / "claude.json", codex_log=self.log)

    def scenario(self, name):
        self.env["FAKE_CODEX_SCENARIO"] = name

    def run_codex(self, prompt="bonjour", **kwargs):
        kwargs.setdefault("permission_mode", "manual")
        run = codex.CodexRun(FAKE_CODEX, prompt=prompt, workdir=str(self.tmp), env=self.env,
                             home=self.home, **kwargs)
        return run, run.run()

    def logged(self):
        return read_log(self.log)


class CommandLineTest(CodexBase):
    def test_the_users_own_extensions_stay_out(self):
        (self.home / "config.toml").write_text(
            '[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers."mon.serveur"]\ncommand = "y"\n'
            '[mcp_servers.node_repl.env]\nSECRET = "jamais lu"\n', encoding="utf-8")
        args = codex.server_args(["codex"], self.home)
        self.assertEqual(args[:2], ["codex", "app-server"])
        self.assertIn("mcp_servers.node_repl.enabled=false", args)
        self.assertIn('mcp_servers."mon.serveur".enabled=false', args)
        for feature in ("apps", "plugins"):
            self.assertEqual(args[args.index(feature) - 1], "--disable")
        self.assertNotIn("jamais lu", " ".join(args))

    def test_unreadable_config_disables_nothing_more(self):
        (self.home / "config.toml").write_text("[[[pas du toml", encoding="utf-8")
        self.assertEqual(codex.server_args(["codex"], self.home),
                         ["codex", "app-server", "--disable", "apps", "--disable", "plugins"])

    def test_explicit_path_must_exist(self):
        with self.assertRaisesRegex(ProviderError, "introuvable"):
            codex.find_cli(str(self.tmp / "absent.exe"))
        exe = self.tmp / "codex.exe"
        exe.write_text("")
        self.assertEqual(codex.find_cli(str(exe)), [str(exe)])

    def test_the_newest_copy_of_the_app_is_found(self):
        import os
        local = self.tmp / "local"
        old = self.home / ".sandbox-bin" / ("codex.exe" if os.name == "nt" else "codex")
        new = local / "OpenAI" / "Codex" / "bin" / "2e5e00da" / old.name
        for path in (old, new):
            path.parent.mkdir(parents=True)
            path.write_text("")
        os.utime(old, (1_700_000_000, 1_700_000_000))  # left behind by the update
        with unittest.mock.patch("shutil.which", return_value=None):
            self.assertEqual(codex.find_cli(home=self.home, local_appdata=str(local)), [str(new)])
            new.unlink()
            self.assertEqual(codex.find_cli(home=self.home, local_appdata=str(local)), [str(old)])
            old.unlink()
            with self.assertRaisesRegex(ProviderError, "Codex introuvable"):
                codex.find_cli(home=self.home, local_appdata=str(local))

    def test_modes(self):
        def params(mode, **kw):
            return codex.CodexRun(["codex"], prompt="x", workdir=str(self.tmp), permission_mode=mode,
                                  **kw).thread_params()
        self.assertEqual((params("manual")["sandbox"], params("manual")["approvalPolicy"]), ("read-only", "on-request"))
        self.assertEqual((params("plan")["sandbox"], params("plan")["approvalPolicy"]), ("read-only", "never"))
        self.assertEqual((params("bypass")["sandbox"], params("bypass")["approvalPolicy"]),
                         ("danger-full-access", "never"))
        # A restricted run (conductor, summary) is read-only whatever the conversation's mode.
        self.assertEqual(params("bypass", tools=[])["sandbox"], "read-only")
        self.assertNotIn("model", params("manual"))
        self.assertEqual(params("manual", model="gpt-5.6-luna")["model"], "gpt-5.6-luna")
        with self.assertRaisesRegex(ProviderError, "mode de permission inconnu"):
            params("partout")

    def test_hub_mcp_server_and_instructions(self):
        spec = {"mcpServers": {"aihub": {"type": "stdio", "command": "py", "args": ["-I", "s.py"]}}}
        p = codex.CodexRun(["codex"], prompt="x", workdir=str(self.tmp), permission_mode="plan",
                           mcp_config=spec, instructions="Projet X").thread_params()
        self.assertEqual(p["config"], {"mcp_servers": {"aihub": {"command": "py", "args": ["-I", "s.py"]}}})
        self.assertEqual(p["developerInstructions"], "Projet X")


class ParsingTest(unittest.TestCase):
    def test_rate_limits(self):
        snap = {"primary": {"usedPercent": 85, "windowDurationMins": 300, "resetsAt": 1791653530},
                "secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": None}}
        info = codex.parse_rate_limits(snap)
        self.assertEqual((info.status, info.limit_type, info.utilization), ("allowed_warning", "five_hour", 0.85))
        self.assertEqual(info.windows["seven_day"], {"utilization": 0.1, "resets_at": None})
        self.assertTrue(info.resets_at.startswith("2026-10-1"))
        self.assertEqual(codex.parse_rate_limits(snap, warning_from=0.9).status, "allowed")
        self.assertEqual(codex.parse_rate_limits({**snap, "rateLimitReachedType": "primary"}).status, "rejected")
        self.assertIsNone(codex.parse_rate_limits({"primary": {"usedPercent": True}}))
        self.assertIsNone(codex.parse_rate_limits("?"))

    def test_error_inside_json(self):
        text, kind = codex._error_text({"message": json.dumps({"error": {"message": "Trop tôt"}}),
                                        "codexErrorInfo": {"other": {}}, "additionalDetails": "détail"})
        self.assertEqual((text, kind), ("Trop tôt (détail)", "other"))
        self.assertIn("aucun compte", codex.describe_error("", kind="unauthorized"))

    def test_lost_session(self):
        self.assertTrue(providers.lost_session("codex", "no rollout found for thread id x"))
        self.assertFalse(providers.lost_session("codex", "No conversation found with session ID: x"))
        self.assertTrue(providers.lost_session("claude_code", "No conversation found with session ID: x"))


class RunTest(CodexBase):
    def test_hello(self):
        chunks, quotas = [], []
        run, result = self.run_codex("bonjour, ça va ?", on_text=chunks.append, on_quota=quotas.append,
                                     model="gpt-5.6-luna")
        self.assertEqual((result.status, result.text, chunks), ("ok", "Bonjour", ["Bon", "jour"]))
        self.assertEqual((result.tokens_in, result.tokens_out, result.num_turns), (12000, 5, 1))
        self.assertEqual(result.usage["cached_input_tokens"], 6000)
        self.assertEqual(quotas[0].windows["five_hour"]["utilization"], 0.12)
        log = self.logged()
        self.assertEqual(log["thread_method"], "thread/start")
        self.assertEqual(log["prompt"], "bonjour, ça va ?")  # sent as UTF-8, whatever the code page
        self.assertEqual(log["requests"][:3], ["initialize", "thread/start", "turn/start"])
        self.assertEqual(log["notifications"], ["initialized"])
        self.assertTrue(result.session_id)

    def test_resume_and_lost_session(self):
        run, result = self.run_codex(resume_session="t-1")
        self.assertEqual(self.logged()["thread_params"]["threadId"], "t-1")
        self.assertEqual((result.status, result.session_id), ("ok", "t-1"))
        self.env["FAKE_CODEX_LOST"] = "1"
        run, result = self.run_codex(resume_session="t-2")
        self.assertEqual(result.status, "error")
        self.assertTrue(codex.lost_session(result.error))

    def test_only_the_final_answer_is_kept(self):
        self.scenario("commentary")
        chunks = []
        run, result = self.run_codex(on_text=chunks.append)
        self.assertEqual(result.text, "Bonjour")
        self.assertEqual("".join(chunks), "Je regarde.\n\nBonjour")  # the commentary was shown as it came
        self.assertEqual(result.num_turns, 2)

    def test_command_approval(self):
        self.scenario("command")
        asked = []
        run, result = self.run_codex(ask_permission=lambda r: asked.append(r) or Decision(True))
        self.assertEqual(result.text, "commande lancée")
        self.assertEqual((asked[0].tool_name, asked[0].input),
                         ("Bash", {"command": "Set-Content x.txt bonjour", "description": "Créer x.txt ?"}))
        self.assertEqual(self.logged()["answers"][0]["result"], {"decision": "accept"})
        self.assertEqual(result.tool_calls, ["Bash"])

    def test_refused_and_unattended_commands(self):
        self.scenario("command")
        run, result = self.run_codex(ask_permission=lambda r: Decision(False, "non"))
        self.assertEqual(result.text, "commande refusée")
        self.assertEqual(self.logged()["answers"][0]["result"], {"decision": "decline"})
        self.assertEqual(result.permission_denials, [{"tool_name": "Bash", "message": "non"}])
        run, result = self.run_codex()  # nobody to ask: refused
        self.assertEqual(result.text, "commande refusée")
        run, result = self.run_codex(ask_permission=lambda r: 1 / 0)  # a broken callback approves nothing
        self.assertEqual(result.text, "commande refusée")
        run, result = self.run_codex(ask_permission=lambda r: True)  # not a Decision
        self.assertEqual(result.text, "commande refusée")

    def test_file_change_names_every_file(self):
        self.scenario("patch")
        asked = []
        run, result = self.run_codex(ask_permission=lambda r: asked.append(r) or Decision(True))
        self.assertEqual(result.text, "fichiers écrits")
        data = asked[0].input
        self.assertEqual((asked[0].tool_name, data["file_path"], data["file_paths"]),
                         ("Edit", "C:\\w\\a.txt", ["C:\\w\\a.txt", "C:\\w\\b.txt"]))
        self.assertIn("+y", data["content"])
        self.assertNotIn("description", data)

    def test_other_requests(self):
        self.scenario("requests")
        run, result = self.run_codex(auto_allow=["mcp__aihub__search_project"])
        answers = {(a["method"], i): a for i, a in enumerate(self.logged()["answers"])}
        by_order = [a for _, a in sorted(answers.items(), key=lambda kv: kv[0][1])]
        self.assertIn("error", by_order[0])  # never a token
        self.assertNotIn("result", by_order[0])
        self.assertEqual(by_order[1]["result"], {"permissions": {}, "scope": "turn"})
        self.assertEqual(by_order[2]["result"]["action"], "accept")  # the hub's own server
        self.assertEqual(by_order[3]["result"], {"action": "decline"})  # any other
        self.assertIn("error", by_order[4])
        self.assertEqual((result.status, result.tool_calls), ("ok", ["mcp__aihub__search_project"]))
        self.assertEqual(result.permission_denials[0]["tool_name"], "Permissions")

    def test_quota_reached(self):
        self.scenario("quota")
        run, result = self.run_codex(model="gpt-5.6-sol")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.error.startswith("Quota de Codex (gpt-5.6-sol) atteint : retour prévu le"))
        self.assertEqual(result.quota.status, "rejected")

    def test_model_needing_a_newer_codex(self):
        self.scenario("newer")
        run, result = self.run_codex(model="gpt-6-astra")
        self.assertIn("gpt-6-astra demande une version plus récente de Codex", result.error)

    def test_failed_turn(self):
        self.scenario("failed")
        run, result = self.run_codex()
        self.assertEqual((result.status, result.error), ("error", "Codex a renvoyé une erreur : boom"))

    def test_structured_answer(self):
        self.scenario("schema")
        schema = {"type": "object"}
        run, result = self.run_codex(json_schema=schema, tools=["Read"])
        self.assertEqual(result.structured, {"summary": "plan", "subtasks": []})
        self.assertEqual(self.logged()["turn_params"]["outputSchema"], schema)

    def test_stop_kills_the_server(self):
        self.scenario("slow")
        run = codex.CodexRun(FAKE_CODEX, prompt="x", workdir=str(self.tmp), env=self.env, home=self.home,
                             permission_mode="plan")
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("r", run.run()))
        thread.start()
        pid = wait_for_log_key(self.log, "prompt")["pid"]
        run.stop()
        thread.join(30)
        self.assertEqual(out["r"].status, "stopped")
        self.assertTrue(wait_dead(pid))

    def test_timeout(self):
        self.scenario("slow")
        started = time.monotonic()
        run, result = self.run_codex(timeout_s=2)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("délai de 2 s", result.error)

    def test_stopped_before_start(self):
        run = codex.CodexRun(FAKE_CODEX, prompt="x", workdir=str(self.tmp), env=self.env, permission_mode="plan")
        run.stop()
        self.assertEqual(run.run().status, "stopped")

    def test_free_queries(self):
        self.assertEqual(codex.read_usage(FAKE_CODEX, workdir=str(self.tmp), env=self.env, home=self.home).utilization,
                         0.42)
        self.env["FAKE_CODEX_MODELS"] = "gpt-6-astra,gpt-5.6-luna"
        self.assertEqual(codex.list_models(FAKE_CODEX, workdir=str(self.tmp), env=self.env, home=self.home),
                         ["gpt-6-astra", "gpt-5.6-luna"])
        self.assertTrue(codex.login_status(FAKE_CODEX, env=self.env))
        self.env["FAKE_CODEX_LOGGED_OUT"] = "1"
        self.assertFalse(codex.login_status(FAKE_CODEX, env=self.env))


class RunnerTest(CodexBase):
    """A conversation whose AI changes: the memory carries it over."""

    def setUp(self):
        super().setUp()
        self.conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        self.claude = store.ensure_agent(self.conn, "claude_code", "haiku")
        self.gpt = store.ensure_agent(self.conn, "codex", "gpt-5.6-luna")
        self.cid = store.create_conversation(self.conn, "Logo", agent_id=self.claude, workdir=str(self.tmp))

    def ask(self, agent, prompt):
        return Request(self.conn, self.cid, agent, prompt, command=FAKE_COMMANDS, env=self.env,
                       settings=config.validate({"summary_model": "aucun"})).run()

    def test_codex_reply_and_quota_are_recorded(self):
        result = self.ask(self.gpt, "bonjour")
        self.assertEqual((result.status, result.fresh_start), ("ok", None))
        reply = store.list_messages(self.conn, self.cid)[-1]
        self.assertEqual((reply["content"], reply["provider"], reply["model"]), ("Bonjour", "codex", "gpt-5.6-luna"))
        self.assertEqual(store.latest_quota(self.conn, "codex")["utilization"], 0.12)
        self.assertEqual(self.logged()["thread_params"]["model"], "gpt-5.6-luna")

    def test_codex_default_model_sends_none(self):
        default = store.ensure_agent(self.conn, "codex", "codex")
        Request(self.conn, self.cid, default, "bonjour", command=FAKE_COMMANDS, env=self.env).run()
        self.assertNotIn("model", self.logged()["thread_params"])

    def test_switching_ai_starts_from_the_memory(self):
        self.ask(self.claude, "on garde le vert sapin")
        result = self.ask(self.gpt, "et le logo ?")
        self.assertEqual(result.fresh_start, "switched")
        sent = self.logged()
        self.assertEqual(sent["thread_method"], "thread/start")  # no Claude session handed to Codex
        self.assertIn("<memoire_de_la_conversation>", sent["prompt"])
        remembered, request = sent["prompt"].split("</memoire_de_la_conversation>")
        self.assertIn("on garde le vert sapin", remembered)
        self.assertNotIn("et le logo ?", remembered)  # the request comes after the memory, not in it
        self.assertIn("et le logo ?", request)
        self.assertIn("aihub", sent["thread_params"]["config"]["mcp_servers"])  # may search its own past
        # Codex again: its own session goes on.
        result = self.ask(self.gpt, "merci")
        self.assertEqual((result.fresh_start, self.logged()["thread_method"]), (None, "thread/resume"))
        self.assertEqual(self.logged()["prompt"], "merci")
        # Back to Claude: its old session missed what Codex said, so the memory again.
        result = self.ask(self.claude, "et maintenant ?")
        self.assertEqual(result.fresh_start, "switched")
        self.assertNotIn("--resume", read_log(self.tmp / "claude.json")["args"])

    def test_lost_codex_thread(self):
        self.ask(self.gpt, "on garde le vert sapin")
        self.env["FAKE_CODEX_LOST"] = "1"
        result = self.ask(self.gpt, "et le logo ?")
        self.assertEqual((result.status, result.fresh_start), ("ok", "automatic"))
        self.assertIn("on garde le vert sapin", self.logged()["prompt"])

    def test_codex_not_offered(self):
        commands = {"claude_code": FAKE_COMMAND, "codex": False}
        with self.assertRaisesRegex(ProviderError, "Codex n'est pas branché"):
            Request(self.conn, self.cid, self.gpt, "x", command=commands, env=self.env).run()
        self.assertEqual(store.list_messages(self.conn, self.cid), [])  # refused before anything is written

    def test_chatgpt_alone_summarizes_with_its_lightest_model(self):
        self.addCleanup(routing.set_available, {"claude_code": None})
        routing.set_available({"codex": {"gpt-5.6-sol", "gpt-5.6-luna"}})  # Claude not connected
        text, *_, model = summarizer(FAKE_COMMANDS, str(self.tmp), "haiku", env=self.env)("Résume")
        self.assertEqual((text, model), ("Bonjour", "gpt-5.6-luna"))
        self.assertEqual(self.logged()["thread_params"]["model"], "gpt-5.6-luna")
        routing.set_available({"codex": {"gpt-6-luna", "gpt-5.6-luna"}})  # the newer light one first
        self.assertEqual(summary_model("haiku"), "gpt-6-luna")
        routing.set_available({"codex": {"gpt-6-sol"}})  # no Luna in this Codex: its default model
        self.assertEqual(summary_model("haiku"), "codex")
        routing.set_available({"claude_code": None, "codex": None})  # both: Haiku, as decided
        self.assertEqual(summary_model("haiku"), "haiku")
        self.assertEqual(summary_model("aucun"), "aucun")

    def test_codex_summarizer(self):
        summarize = summarizer(FAKE_COMMANDS, str(self.tmp), "gpt-5.6-luna", env=self.env)
        text, *_ = summarize("Résume")
        self.assertEqual(text, "Bonjour")
        self.assertEqual(self.logged()["thread_params"]["sandbox"], "read-only")


class RoutingTest(unittest.TestCase):
    def tearDown(self):
        routing.set_available({"claude_code": None})

    def test_gpt_kinds_go_to_codex_once_connected(self):
        routing.set_available({"claude_code": None, "codex": {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol"}})
        self.assertEqual(routing.route("ops").rule.model, "gpt-6-astra")
        self.assertEqual(routing.route("securite").rule.model, "gpt-6-sol")
        self.assertEqual(routing.route("code").rule.model, "opus")  # Claude stays better there

    def test_a_model_the_installed_codex_lacks_is_passed_over(self):
        routing.set_available({"claude_code": None, "codex": {"gpt-5.6-sol"}})
        choice = routing.route("ops")
        self.assertEqual(choice.rule.model, "opus")
        self.assertIn("GPT-6 Astra serait meilleur", choice.reason)
        self.assertIn("le Codex installé ne le propose pas", choice.reason)

    def test_default_is_claude_alone(self):
        self.assertIn("Codex n'est pas branché", routing.route("ops").reason)


class ModelsTest(unittest.TestCase):
    def test_provider_of(self):
        self.assertEqual(config.provider_of("gpt-6-astra"), "codex")
        self.assertEqual(config.provider_of("codex"), "codex")
        self.assertEqual(config.provider_of("opus"), "claude_code")
        self.assertEqual(config.validate({"summary_model": "gpt-5.6-luna"}).summary_model, "gpt-5.6-luna")
        self.assertEqual(store.agent_name("codex", "codex"), "Codex")
        self.assertEqual(store.agent_name("codex", "gpt-6-sol"), "Codex (gpt-6-sol)")

    def test_unknown_provider(self):
        self.assertEqual(providers.module("ollama").PROVIDER, "ollama")  # connected since phase 10
        with self.assertRaisesRegex(ProviderError, "pas encore branché"):
            providers.module("autre")


if __name__ == "__main__":
    unittest.main()
