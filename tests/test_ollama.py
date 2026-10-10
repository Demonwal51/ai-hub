"""The local AI (phase 10), against tests/fake_ollama.py."""

import threading
import time
import unittest

from aihub import config, db, providers, routing, store
from aihub.providers import ollama
from aihub.providers.claude_code import ProviderError
from aihub.runner import Request, summary_model

from .fake_ollama import FakeOllama
from .helpers import FAKE_COMMAND, TempDirMixin, fake_env


class OllamaTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama().__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.tmp = self.make_tempdir()

    def run_local(self, prompt="bonjour", **kw):
        kw.setdefault("model", "local:qwen3.5:4b")
        run = ollama.OllamaRun([self.fake.url], prompt=prompt, workdir=str(self.tmp), permission_mode="manual", **kw)
        return run, run.run()


class RunTest(OllamaTestCase):
    def test_hello(self):
        chunks = []
        run, result = self.run_local(on_text=chunks.append, instructions="Projet X")
        self.assertEqual((result.status, result.text, chunks), ("ok", "Bonjour", ["Bon", "jour"]))
        self.assertEqual((result.tokens_in, result.tokens_out, result.num_turns, result.model),
                         (100, 5, 1, "local:qwen3.5:4b"))
        sent = self.fake.requests[0]
        self.assertEqual((sent["model"], sent["think"]), ("qwen3.5:4b", False))
        self.assertIn("Projet X", sent["messages"][0]["content"])
        self.assertIn("ne peux rien modifier", sent["messages"][0]["content"])
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["list_files", "read_file", "search_files"])

    def test_it_reads_the_folder(self):
        (self.tmp / "notes.txt").write_text("le logo est vert", encoding="utf-8")
        self.fake.scenario = "tool"
        run, result = self.run_local()
        self.assertEqual(result.text, "Le fichier dit : le logo est vert")
        self.assertEqual((result.tool_calls, result.num_turns), (["read_file"], 2))
        self.assertEqual(self.fake.requests[1]["messages"][-1]["role"], "tool")

    def test_endless_tools_are_stopped(self):
        (self.tmp / "notes.txt").write_text("x", encoding="utf-8")
        self.fake.scenario = "loop"
        run, result = self.run_local()
        self.assertEqual(result.status, "error")
        self.assertIn("plus de 8 appels", result.error)

    def test_missing_model(self):
        self.fake.scenario = "missing"
        run, result = self.run_local(model="local:absent")
        self.assertIn("n'est pas installé dans Ollama", result.error)

    def test_not_running(self):
        run = ollama.OllamaRun(["http://127.0.0.1:9"], prompt="x", workdir=str(self.tmp), permission_mode="plan",
                               model="local:qwen3.5:4b")
        self.assertIn("démarrez Ollama", run.run().error)
        with self.assertRaisesRegex(ProviderError, "ne répond pas"):
            ollama.list_models(["http://127.0.0.1:9"], timeout_s=1)

    def test_stop_and_timeout(self):
        self.fake.scenario = "slow"
        run = ollama.OllamaRun([self.fake.url], prompt="x", workdir=str(self.tmp), permission_mode="plan",
                               model="local:qwen3.5:4b")
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("r", run.run()))
        thread.start()
        time.sleep(1.2)
        run.stop()
        thread.join(10)
        self.assertEqual(box["r"].status, "stopped")
        run, result = self.run_local(timeout_s=1)
        self.assertIn("délai de 1 s", result.error)

    def test_a_summary_gets_no_tool(self):
        run, result = self.run_local(tools=[])
        self.assertNotIn("tools", self.fake.requests[0])

    def test_structured(self):
        self.fake.scenario = "json"
        run, result = self.run_local(json_schema={"type": "object"})
        self.assertEqual(result.structured, {"summary": "plan", "subtasks": []})
        self.assertEqual(self.fake.requests[0]["format"], {"type": "object"})

    def test_models(self):
        self.assertEqual(ollama.list_models([self.fake.url]), ["local:qwen3.5:4b", "local:llama3.2:3b"])
        self.assertEqual(config.provider_of("local:qwen3.5:4b"), "ollama")
        self.assertEqual(config.validate({"summary_model": "local:qwen3.5:4b"}).summary_model, "local:qwen3.5:4b")


class FolderTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        base = self.make_tempdir()  # the folder and a file just outside it, both in the test's own temp dir
        self.root = base / "travail"
        self.root.mkdir()
        (base / "dehors.txt").write_text("hors du dossier", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('vert sapin')\n", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / "image.bin").write_bytes(b"\x00\x01")
        self.folder = ollama.Folder(self.root)

    def test_reads_only_inside(self):
        self.assertEqual(self.folder.list_files(), "image.bin\nsrc/app.py")
        self.assertIn("vert sapin", self.folder.read_file("src/app.py"))
        self.assertIn("src/app.py:1:", self.folder.search_files("sapin"))
        for path in ("../dehors.txt", str(self.root.parent / "dehors.txt"), ".git/config", "image.bin", "absent.txt"):
            with self.subTest(path=path):
                self.assertTrue(self.folder.call("read_file", {"path": path}).startswith("refusé"))
        # Out of the folder: said so, to the model (not « file not found » or a stray error).
        self.assertIn("hors du dossier de travail", self.folder.call("read_file", {"path": "../dehors.txt"}))
        self.assertIn("hors du dossier de travail", self.folder.call("list_files", {"path": ".."}))


class RunnerTest(OllamaTestCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        self.local = store.ensure_agent(self.conn, "ollama", "local:qwen3.5:4b")
        self.cid = store.create_conversation(self.conn, "Local", agent_id=self.local, workdir=str(self.tmp))
        self.commands = {"claude_code": FAKE_COMMAND, "ollama": [self.fake.url]}
        self.env = fake_env("text", self.tmp / "claude.json")

    def ask(self, prompt, agent=None):
        return Request(self.conn, self.cid, agent or self.local, prompt, command=self.commands, env=self.env,
                       settings=config.validate({"summary_model": "aucun"})).run()

    def test_every_request_carries_the_memory(self):
        first = self.ask("le logo sera vert sapin")
        self.assertEqual((first.status, first.fresh_start), ("ok", None))
        self.assertEqual(self.fake.requests[0]["messages"][-1]["content"], "le logo sera vert sapin")
        second = self.ask("et le logo ?")
        sent = self.fake.requests[-1]["messages"][-1]["content"]
        self.assertIn("<memoire_de_la_conversation>", sent)
        remembered, request = sent.split("</memoire_de_la_conversation>")
        self.assertIn("le logo sera vert sapin", remembered)
        self.assertNotIn("et le logo ?", remembered)
        self.assertEqual(second.fresh_start, None)  # not a « fresh start »: the local AI's normal way
        reply = store.list_messages(self.conn, self.cid)[-1]
        self.assertEqual((reply["provider"], reply["content"]), ("ollama", "Bonjour"))

    def test_the_local_cap(self):
        self.ask("début " + "mot " * 3000)  # an exchange far above the local cap
        Request(self.conn, self.cid, self.local, "et alors ?", command=self.commands, env=self.env,
                settings=config.validate({"summary_model": "aucun", "rag_budget_local": 1000})).run()
        sent = self.fake.requests[-1]["messages"][-1]["content"]
        self.assertLess(len(sent), 1000 * 3.5 * 1.3)  # compressed to the local cap (8 000 for the others)

    def test_after_claude(self):
        claude = store.ensure_agent(self.conn, "claude_code", "haiku")
        self.ask("on garde le vert sapin", agent=claude)
        result = self.ask("et le logo ?")
        self.assertEqual(result.fresh_start, "switched")
        self.assertIn("on garde le vert sapin", self.fake.requests[-1]["messages"][-1]["content"])

    def test_summary_falls_back_to_the_local_ai(self):
        self.addCleanup(routing.set_available, {"claude_code": None})
        routing.set_available({"ollama": {"local:qwen3.5:4b"}})
        self.assertEqual(summary_model("haiku"), "local:qwen3.5:4b")
        routing.set_available({"claude_code": None, "ollama": {"local:qwen3.5:4b"}})
        self.assertEqual(summary_model("haiku"), "haiku")

    def test_stateless(self):
        self.assertTrue(providers.stateless("ollama"))
        self.assertFalse(providers.stateless("claude_code"))


if __name__ == "__main__":
    unittest.main()
