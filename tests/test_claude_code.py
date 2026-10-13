import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aihub.providers import claude_code as cc
from aihub.providers.claude_code import ClaudeCodeRun, Decision, ProviderError
from tests.helpers import (FAKE_COMMAND, TempDirMixin, fake_env, process_alive, read_log,
                           wait_dead, wait_for_log_key)


class FakeRunTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.workdir = self.make_tempdir()
        self.log = self.make_tempdir() / "log.json"

    def make_run(self, scenario, **kwargs):
        kwargs.setdefault("prompt", "salut")
        kwargs.setdefault("permission_mode", "manual")
        return ClaudeCodeRun(FAKE_COMMAND, workdir=str(self.workdir),
                             env=fake_env(scenario, self.log), **kwargs)


class StreamingTests(FakeRunTestCase):
    def test_text_quota_and_measurements(self):
        chunks, quotas = [], []
        result = self.make_run("text", on_text=chunks.append, on_quota=quotas.append).run()
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.error)
        self.assertEqual(chunks, ["Bon", "jour"])
        self.assertEqual(result.text, "Bonjour")
        self.assertEqual((result.tokens_in, result.tokens_out), (142, 7))
        self.assertEqual(result.session_id, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(result.model, "claude-test-model")
        self.assertGreater(result.duration_ms, 0)
        self.assertEqual(len(quotas), 1)
        quota = quotas[0]
        self.assertIs(result.quota, quota)
        self.assertEqual((quota.status, quota.limit_type, quota.utilization),
                         ("allowed_warning", "five_hour", 0.82))
        # resetsAt 1791000000 (epoch seconds) = 3 October 2026, 04:00 UTC.
        self.assertEqual(quota.resets_at, "2026-10-03T04:00:00+00:00")
        self.assertEqual(quota.windows["five_hour"]["resets_at"], "2026-10-03T04:00:00+00:00")

    def test_process_pipes_are_closed(self):
        for scenario in ("text", "crash"):
            with self.subTest(scenario=scenario):
                run = self.make_run(scenario)
                run.run()
                proc = run._proc
                self.assertEqual([s.closed for s in (proc.stdin, proc.stdout, proc.stderr)],
                                 [True, True, True])

    def test_accents_survive_without_utf8_settings(self):
        # A shell may set PYTHONIOENCODING or PYTHONUTF8; a server started
        # elsewhere does not. The exchange must not depend on them.
        env = fake_env("text", self.log)
        for name in ("PYTHONIOENCODING", "PYTHONUTF8"):
            env.pop(name, None)
        run = ClaudeCodeRun(FAKE_COMMAND, prompt="Écris « été » à côté", workdir=str(self.workdir),
                            permission_mode="manual", env=env)
        self.assertEqual(run.run().status, "ok")
        self.assertEqual(read_log(self.log)["user_message"]["message"]["content"],
                         "Écris « été » à côté")

    def test_prompt_and_working_folder_reach_the_cli(self):
        self.make_run("text", prompt="Écris un poème").run()
        log = read_log(self.log)
        self.assertEqual(log["user_message"],
                         {"type": "user", "message": {"role": "user", "content": "Écris un poème"}})
        self.assertEqual(os.path.normcase(log["cwd"]), os.path.normcase(str(self.workdir)))


class ArgumentTests(FakeRunTestCase):
    def test_permission_modes(self):
        for mode, expected in (("manual", "manual"), ("plan", "plan"), ("bypass", "bypassPermissions")):
            with self.subTest(mode=mode):
                self.make_run("text", permission_mode=mode).run()
                args = read_log(self.log)["args"]
                self.assertEqual(args[args.index("--permission-mode") + 1], expected)
                for flag in ("-p", "--strict-mcp-config", "--include-partial-messages", "--verbose"):
                    self.assertIn(flag, args)
                self.assertEqual(args[args.index("--permission-prompts") + 1], "host")
                self.assertEqual(args[args.index("--permission-prompt-tool") + 1], "stdio")
                self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
                self.assertEqual(args[args.index("--output-format") + 1], "stream-json")

    def test_model_and_resume(self):
        self.make_run("text").run()
        args = read_log(self.log)["args"]
        self.assertNotIn("--model", args)
        self.assertNotIn("--resume", args)
        self.make_run("text", model="opus", resume_session="abc").run()
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertEqual(args[args.index("--resume") + 1], "abc")

    def test_invalid_requests_refused_before_start(self):
        with self.assertRaisesRegex(ProviderError, "mode de permission inconnu"):
            self.make_run("text", permission_mode="auto")
        with self.assertRaisesRegex(ProviderError, "demande vide"):
            self.make_run("text", prompt="  ")
        run = ClaudeCodeRun(FAKE_COMMAND, prompt="x", workdir=str(self.workdir / "absent"),
                            permission_mode="manual", env=fake_env("text", self.log))
        with self.assertRaisesRegex(ProviderError, "dossier de travail introuvable"):
            run.run()
        self.assertFalse(self.log.exists())

    def test_missing_executable(self):
        run = ClaudeCodeRun([str(self.workdir / "nope.exe")], prompt="x", workdir=str(self.workdir),
                            permission_mode="manual")
        with self.assertRaisesRegex(ProviderError, "impossible de lancer"):
            run.run()


class PermissionTests(FakeRunTestCase):
    def test_allowed_write_happens(self):
        asked = []

        def ask(request):
            asked.append(request)
            return Decision(True)

        result = self.make_run("permission", ask_permission=ask).run()
        self.assertEqual(result.status, "ok")
        self.assertEqual([(r.tool_name, r.input, r.request_id) for r in asked],
                         [("Write", {"file_path": "note.txt", "content": "contenu"}, "req-1")])
        self.assertEqual((self.workdir / "note.txt").read_text(encoding="utf-8"), "contenu")
        answer = read_log(self.log)["answers"][0]
        self.assertEqual(answer, {"subtype": "success", "request_id": "req-1", "response": {
            "behavior": "allow", "updatedInput": {"file_path": "note.txt", "content": "contenu"}}})
        self.assertEqual(result.permission_denials, [])

    def test_modified_input_is_used(self):
        decision = Decision(True, updated_input={"file_path": "note.txt", "content": "modifié"})
        self.make_run("permission", ask_permission=lambda r: decision).run()
        self.assertEqual((self.workdir / "note.txt").read_text(encoding="utf-8"), "modifié")

    def test_denied_write_does_not_happen(self):
        result = self.make_run("permission",
                               ask_permission=lambda r: Decision(False, "pas maintenant")).run()
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertEqual(read_log(self.log)["answers"][0]["response"],
                         {"behavior": "deny", "message": "pas maintenant"})
        self.assertEqual(result.text, "refusé : pas maintenant")
        self.assertEqual(result.permission_denials,
                         [{"tool_name": "Write", "message": "pas maintenant"}])

    def test_rejected_plan_ends_the_turn_without_waiting_for_another_prompt(self):
        from unittest import mock
        run = self.make_run('exit_plan_wait', timeout_s=2,
                            ask_permission=lambda request: Decision(False, 'refaire le plan'))
        with mock.patch.object(run, '_send', wraps=run._send) as sent:
            result = run.run()
        self.assertEqual(result.status, 'stopped')
        self.assertIsNone(result.error)
        responses = [call.args[1] for call in sent.call_args_list if call.args[1].get('type')=='control_response']
        self.assertEqual(responses[-1]['response']['response'],
                         {'behavior':'deny','message':'refaire le plan','interrupt':True})
        self.assertEqual(result.permission_denials, [{'tool_name':'ExitPlanMode','message':'refaire le plan'}])

    def test_rejected_plan_discards_other_buffered_permission_requests(self):
        asked = []
        def reject(request):
            asked.append(request.tool_name)
            return Decision(False, 'non')
        result = self.make_run('exit_plan_buffered', timeout_s=2, ask_permission=reject).run()
        self.assertEqual(result.status, 'stopped')
        self.assertEqual(asked, ['ExitPlanMode'])

    def test_no_one_to_answer_means_deny(self):
        self.make_run("permission").run()
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertIn("refus par défaut", read_log(self.log)["answers"][0]["response"]["message"])

    def test_broken_callback_never_approves(self):
        def broken(request):
            raise RuntimeError("panne")

        for callback, fragment in ((broken, "validation impossible : panne"),
                                   (lambda r: "oui", "réponse de validation invalide")):
            with self.subTest(fragment=fragment):
                result = self.make_run("permission", ask_permission=callback).run()
                self.assertEqual(result.status, "ok")
                self.assertFalse((self.workdir / "note.txt").exists())
                self.assertIn(fragment, read_log(self.log)["answers"][0]["response"]["message"])

    def test_unknown_control_request_gets_an_error(self):
        result = self.make_run("unknown_control").run()
        self.assertEqual(result.status, "ok")
        answer = read_log(self.log)["answers"][0]
        self.assertEqual(answer["subtype"], "error")
        self.assertEqual(answer["request_id"], "req-x")


class ErrorTests(FakeRunTestCase):
    def test_not_logged_in(self):
        result = self.make_run("not_logged_in").run()
        self.assertEqual(result.status, "error")
        self.assertIn("claude auth login", result.error)

    def test_quota_names_the_ai_and_the_return_time(self):
        result = self.make_run("quota").run()
        self.assertEqual(result.status, "error")
        self.assertTrue(result.error.startswith("Quota de Claude Code (claude-test-model) atteint"))
        self.assertIn("retour prévu le", result.error)
        self.assertEqual(result.quota.status, "rejected")

    def test_crash_reports_exit_code_and_stderr(self):
        result = self.make_run("crash").run()
        self.assertEqual(result.status, "error")
        self.assertIn("code 3", result.error)
        self.assertIn("boom: erreur interne", result.error)


class StopTests(FakeRunTestCase):
    def test_stop_kills_cli_and_its_children(self):
        run = self.make_run("hang")
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("result", run.run()))
        thread.start()
        log = wait_for_log_key(self.log, "child_pid")
        self.assertTrue(process_alive(log["child_pid"]))
        run.stop()
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["result"].status, "stopped")
        self.assertIsNone(box["result"].error)
        self.assertTrue(wait_dead(log["pid"]))
        self.assertTrue(wait_dead(log["child_pid"]))

    def test_timeout_stops_with_an_error(self):
        run = self.make_run("hang", timeout_s=1)
        result = run.run()
        log = read_log(self.log)
        self.assertEqual(result.status, "error")
        self.assertIn("délai de 1 s", result.error)
        self.assertTrue(wait_dead(log["child_pid"]))

    def test_stop_before_run_starts_nothing(self):
        run = self.make_run("text")
        run.stop()
        self.assertEqual(run.run().status, "stopped")
        self.assertFalse(self.log.exists())


class FindCliTests(TempDirMixin, unittest.TestCase):
    def test_explicit_path(self):
        exe = self.make_tempdir() / "claude.exe"
        exe.write_text("")
        self.assertEqual(cc.find_cli(str(exe)), [str(exe)])
        with self.assertRaisesRegex(ProviderError, "introuvable à l'emplacement"):
            cc.find_cli(str(exe) + ".absent")

    def test_newest_desktop_copy_wins(self):
        appdata = self.make_tempdir()
        root = appdata / "Claude" / "claude-code"
        for version in ("2.1.9", "2.1.293", "pas-une-version"):
            (root / version / "abc").mkdir(parents=True)
            (root / version / "abc" / "claude.exe").write_text("")
        empty_path = self.make_tempdir()
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata), "PATH": str(empty_path)}):
            found = cc.find_cli()
        # Numeric order: 2.1.293 is newer than 2.1.9.
        self.assertEqual(Path(found[0]), root / "2.1.293" / "abc" / "claude.exe")

    def test_nothing_found(self):
        empty = self.make_tempdir()
        with mock.patch.dict(os.environ, {"APPDATA": str(empty), "PATH": str(empty)}):
            with self.assertRaisesRegex(ProviderError, "Claude Code introuvable"):
                cc.find_cli()


class UsageTests(FakeRunTestCase):
    def read(self, case="ok"):
        env = fake_env("text", self.log)
        env["FAKE_USAGE"] = case
        return cc.read_usage(FAKE_COMMAND, workdir=str(self.workdir), env=env)

    def test_current_quota_without_calling_the_model(self):
        quota = self.read()
        self.assertEqual((quota.status, quota.limit_type, quota.utilization), ("allowed", "five_hour", 0.29))
        self.assertEqual(quota.windows, {
            "five_hour": {"utilization": 0.29, "resets_at": "2026-10-09T17:39:59+00:00"},
            "seven_day": {"utilization": 0.13, "resets_at": "2026-10-15T22:59:59+00:00"},
        })
        log = read_log(self.log)
        self.assertNotIn("user_message", log)  # no prompt went to the model
        self.assertIn("--no-session-persistence", log["args"])
        self.assertNotIn("--permission-mode", log["args"])

    def test_close_to_the_limit(self):
        quota = self.read("high")
        self.assertEqual((quota.status, quota.utilization), ("allowed_warning", 0.92))

    def test_refusals(self):
        with self.assertRaisesRegex(ProviderError, "claude auth login"):
            self.read("error")
        with self.assertRaisesRegex(ProviderError, "ne donne pas de quota"):
            self.read("unavailable")
        with self.assertRaisesRegex(ProviderError, "dossier introuvable"):
            cc.read_usage(FAKE_COMMAND, workdir=str(self.workdir / "absent"))

    def test_parse_usage(self):
        full = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 100, "resets_at": None},
            "seven_day": {"utilization": "?"}}}
        quota = cc.parse_usage(full)
        self.assertEqual((quota.status, list(quota.windows)), ("rejected", ["five_hour"]))
        warned = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 10}, "limits": [{"severity": "warning"}]}}
        self.assertEqual(cc.parse_usage(warned).status, "allowed_warning")
        for bad in (None, {}, {"rate_limits_available": True, "rate_limits": {"five_hour": None}}):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                cc.parse_usage(bad)


class EnvironmentTests(unittest.TestCase):
    def test_host_session_variables_are_removed(self):
        env = cc.clean_env({
            "CLAUDECODE": "1", "CLAUDE_PID": "42", "CLAUDE_CODE_SESSION_ID": "s",
            "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDE_AGENT_SDK_VERSION": "1",
            "CLAUDE_PREVIEW_CLASSIFIER_FLOOR": "x", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
            "PATH": "p", "APPDATA": "a", "CLAUDE_CONFIG_DIR": "c",
        })
        self.assertEqual(env, {"PATH": "p", "APPDATA": "a", "CLAUDE_CONFIG_DIR": "c"})

    def test_outside_a_host_session_nothing_is_removed(self):
        base = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000", "ANTHROPIC_BASE_URL": "https://proxy", "PATH": "p"}
        self.assertEqual(cc.clean_env(base), base)

    def test_run_uses_the_cleaned_environment_by_default(self):
        captured = {}

        def fake_popen(args, **kwargs):
            captured.update(kwargs)
            raise OSError("arrêt du test")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s"}), \
                mock.patch.object(cc.subprocess, "Popen", fake_popen):
            run = ClaudeCodeRun(["claude"], prompt="x", workdir=tmp, permission_mode="manual")
            with self.assertRaises(ProviderError):
                run.run()
        self.assertNotIn("CLAUDECODE", captured["env"])
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", captured["env"])
        self.assertIn("PATH", captured["env"])


class ParsingTests(unittest.TestCase):
    def test_parse_quota(self):
        self.assertIsNone(cc.parse_quota(None))
        self.assertIsNone(cc.parse_quota({"status": "unknown"}))
        quota = cc.parse_quota({"status": "allowed", "utilization": True, "resetsAt": "demain",
                                "rateLimitType": 5})
        self.assertEqual((quota.status, quota.utilization, quota.resets_at, quota.limit_type),
                         ("allowed", None, None, None))

    def test_describe_error(self):
        self.assertIn("claude auth login", cc.describe_error("Not logged in · Please run /login"))
        self.assertEqual(cc.describe_error("x", api_error_status=429), "Quota de Claude Code atteint.")
        self.assertEqual(cc.describe_error("panne réseau"),
                         "Claude Code a renvoyé une erreur : panne réseau")
        self.assertEqual(cc.describe_error(""), "Claude Code a renvoyé une erreur : erreur sans message")


if __name__ == "__main__":
    unittest.main()
