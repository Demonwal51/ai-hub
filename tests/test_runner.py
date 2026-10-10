import threading
import unittest

from aihub import db, store
from aihub.providers.claude_code import Decision, ProviderError
from aihub.runner import Request
from aihub.store import StoreError
from tests.helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log, wait_for_log_key

SESSION = "11111111-2222-3333-4444-555555555555"


class RunnerTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.workdir = self.make_tempdir()
        self.log = self.make_tempdir() / "log.json"
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "opus")
        self.cid = store.create_conversation(self.conn, "c", agent_id=self.agent,
                                             workdir=str(self.workdir))

    def request(self, scenario, prompt="salut", **kwargs):
        return Request(self.conn, self.cid, self.agent, prompt, command=FAKE_COMMAND,
                       env=fake_env(scenario, self.log), **kwargs)

    def messages(self):
        return store.list_messages(self.conn, self.cid)


class SuccessfulRunTests(RunnerTestCase):
    def test_reply_is_recorded_with_measurements(self):
        result = self.request("text", prompt="dis bonjour").run()
        self.assertEqual(result.status, "ok")
        user, reply = self.messages()
        self.assertEqual((user["role"], user["content"]), ("user", "dis bonjour"))
        self.assertEqual(
            (reply["status"], reply["content"], reply["tokens_in"], reply["tokens_out"],
             reply["session_id"], reply["provider"], reply["model"]),
            ("ok", "Bonjour", 142, 7, SESSION, "claude_code", "opus"),
        )
        self.assertGreater(reply["duration_ms"], 0)
        self.assertIsNone(store.active_request(self.conn, self.cid))

    def test_text_reaches_the_database_while_streaming(self):
        seen = []

        def on_text(chunk):
            reply = store.active_request(self.conn, self.cid)
            seen.append((chunk, reply["content"], reply["status"]))

        self.request("text", on_text=on_text).run()
        self.assertEqual(seen, [("Bon", "Bon", "running"), ("jour", "Bonjour", "running")])

    def test_quota_is_recorded(self):
        self.assertIsNone(store.latest_quota(self.conn, "claude_code"))
        self.request("text").run()
        quota = store.latest_quota(self.conn, "claude_code")
        self.assertEqual((quota["status"], quota["limit_type"], quota["utilization"]),
                         ("allowed_warning", "five_hour", 0.82))
        self.assertIn("five_hour", quota["windows_json"])

    def test_next_request_resumes_the_session(self):
        self.request("text").run()
        self.assertNotIn("--resume", read_log(self.log)["args"])
        self.request("text").run()
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--resume") + 1], SESSION)

    def test_conversation_settings_reach_the_cli(self):
        store.set_permission_mode(self.conn, self.cid, "bypass", confirm_bypass=True)
        self.request("text").run()
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(args[args.index("--model") + 1], "opus")


class PermissionJournalTests(RunnerTestCase):
    def test_each_answer_is_journaled(self):
        self.request("permission", ask_permission=lambda r: Decision(True)).run()
        mid = self.messages()[1]["id"]
        (entry,) = store.list_permissions(self.conn, message_id=mid)
        self.assertEqual((entry["tool_name"], entry["decision"]), ("Write", "allow"))
        self.assertIn("note.txt", entry["input_json"])

        self.request("permission", ask_permission=lambda r: Decision(False, "non")).run()
        mid = self.messages()[3]["id"]
        (entry,) = store.list_permissions(self.conn, message_id=mid)
        self.assertEqual((entry["decision"], entry["reason"]), ("deny", "non"))

    def test_without_callback_the_default_refusal_is_journaled(self):
        self.request("permission").run()
        (entry,) = store.list_permissions(self.conn, message_id=self.messages()[1]["id"])
        self.assertEqual(entry["decision"], "deny")
        self.assertFalse((self.workdir / "note.txt").exists())


class FailureTests(RunnerTestCase):
    def test_provider_error_is_recorded_and_unlocks(self):
        result = self.request("not_logged_in").run()
        self.assertEqual(result.status, "error")
        reply = self.messages()[1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("claude auth login", reply["error"])
        self.request("text").run()  # the conversation is free again

    def test_quota_error_names_the_ai(self):
        self.request("quota").run()
        reply = self.messages()[1]
        self.assertTrue(reply["error"].startswith("Quota de Claude Code"))
        self.assertEqual(store.latest_quota(self.conn, "claude_code")["status"], "rejected")

    def test_crash_is_recorded(self):
        self.request("crash").run()
        reply = self.messages()[1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("boom", reply["error"])

    def test_bug_in_callback_stops_the_reply(self):
        def on_text(chunk):
            raise RuntimeError("bogue d'affichage")

        with self.assertRaisesRegex(RuntimeError, "bogue d'affichage"):
            self.request("text", on_text=on_text).run()
        self.assertEqual(self.messages()[1]["status"], "stopped")
        self.assertIsNone(store.active_request(self.conn, self.cid))

    def test_refusals_before_start_leave_nothing(self):
        no_dir = store.create_conversation(self.conn, "sans dossier", agent_id=self.agent)
        with self.assertRaisesRegex(ProviderError, "aucun dossier de travail"):
            Request(self.conn, no_dir, self.agent, "x", command=FAKE_COMMAND).run()
        local = store.create_agent(self.conn, "Local", "ollama", "local:qwen")
        with self.assertRaisesRegex(ProviderError, "pas branché sur ce serveur"):
            Request(self.conn, self.cid, local, "x", command={"claude_code": FAKE_COMMAND, "ollama": False}).run()
        self.assertEqual(store.list_messages(self.conn, no_dir), [])
        self.assertEqual(self.messages(), [])

    def test_missing_working_folder_is_refused_before_anything(self):
        store.update_conversation(self.conn, self.cid, workdir=str(self.workdir / "disparu"))
        with self.assertRaisesRegex(ProviderError, "dossier de travail introuvable"):
            self.request("text").run()
        self.assertEqual(self.messages(), [])
        self.assertFalse(self.log.exists())


class StopTests(RunnerTestCase):
    def test_stop_from_another_thread(self):
        # sqlite3 connections belong to one thread: the worker opens its own,
        # and stop() from this thread touches only the process.
        path = str(self.make_tempdir() / "hub.db")
        setup = db.connect(path)
        agent = store.create_agent(setup, "Claude", "claude_code", "opus")
        cid = store.create_conversation(setup, "c", agent_id=agent, workdir=str(self.workdir))
        setup.close()
        box = {}
        ready = threading.Event()

        def worker():
            conn = db.connect(path)
            try:
                box["request"] = Request(conn, cid, agent, "travaille", command=FAKE_COMMAND,
                                         env=fake_env("hang", self.log))
                ready.set()
                box["result"] = box["request"].run()
            finally:
                conn.close()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(ready.wait(timeout=20))
        wait_for_log_key(self.log, "child_pid")
        box["request"].stop()
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["result"].status, "stopped")

        check = db.connect(path)
        self.addCleanup(check.close)
        user, reply = store.list_messages(check, cid)
        # The text streamed before the stop is kept; the conversation is free.
        self.assertEqual((reply["status"], reply["content"]), ("stopped", "je travaille"))
        self.assertIsNone(store.active_request(check, cid))

    def test_stop_before_run(self):
        request = self.request("text")
        request.stop()
        result = request.run()
        self.assertEqual(result.status, "stopped")
        self.assertEqual(self.messages()[1]["status"], "stopped")
        self.assertFalse(self.log.exists())


class ConcurrencyTests(RunnerTestCase):
    def test_store_refuses_a_second_request_while_one_runs(self):
        store.start_request(self.conn, self.cid, self.agent)
        with self.assertRaisesRegex(StoreError, "déjà en cours"):
            self.request("text").run()


if __name__ == "__main__":
    unittest.main()
