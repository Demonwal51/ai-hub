import os
import secrets
import unittest
from unittest import mock

from aihub import auth, cli, db, store
from tests.helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log


class CliTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.workdir = self.make_tempdir()
        self.log = tmp / "log.json"
        self.output = []

    def out(self, text="", end="\n"):
        self.output.append(text + end)

    def text(self):
        return "".join(self.output)

    def run_cli(self, argv, scenario="text", answers=(), logged_in=False):
        answers = list(answers)
        env = fake_env(scenario, self.log)
        env["FAKE_CLAUDE_LOGGED_IN"] = "1" if logged_in else "0"
        with mock.patch.dict(os.environ, env):
            return cli.main(["--db", self.db_path] + argv, ask=lambda prompt: answers.pop(0),
                            out=self.out, command=FAKE_COMMAND)

    def conn(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn


class AskTests(CliTestCase):
    def test_new_conversation_streams_and_summarises(self):
        code = self.run_cli(["ask", "--workdir", str(self.workdir), "dis bonjour"])
        self.assertEqual(code, 0)
        text = self.text()
        self.assertIn("Conversation 1 créée (manual", text)
        self.assertIn("Bonjour", text)
        self.assertIn("[ok en", text)
        self.assertIn("142 tokens envoyés, 7 reçus", text)
        # The quota was close to its limit: the CLI says so, window by window.
        self.assertIn("Quota de Claude Code : bientôt atteint", text)
        self.assertIn("fenêtre 5 heures : reste 18 % (82 % utilisés), retour le 03/10/2026", text)
        self.assertIn("--conversation 1", text)
        conv = store.get_conversation(self.conn(), 1)
        self.assertEqual((conv["title"], conv["permission_mode"]), ("dis bonjour", "manual"))

    def test_default_model_is_not_forced(self):
        self.run_cli(["ask", "--workdir", str(self.workdir), "x"])
        self.assertNotIn("--model", read_log(self.log)["args"])
        self.run_cli(["ask", "--workdir", str(self.workdir), "--model", "sonnet", "x"])
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--model") + 1], "sonnet")

    def test_continue_a_conversation(self):
        self.run_cli(["ask", "--workdir", str(self.workdir), "premier"])
        self.output.clear()
        self.assertEqual(self.run_cli(["ask", "--conversation", "1", "second"]), 0)
        self.assertIn("--resume", read_log(self.log)["args"])
        self.assertEqual(len(store.list_messages(self.conn(), 1)), 4)

    def test_permission_answered_at_the_keyboard(self):
        self.run_cli(["ask", "--workdir", str(self.workdir), "écris"], scenario="permission",
                     answers=["o"])
        self.assertIn("L'agent demande à utiliser « Write »", self.text())
        self.assertIn('"file_path": "note.txt"', self.text())
        self.assertEqual((self.workdir / "note.txt").read_text(encoding="utf-8"), "contenu")

    def test_permission_refused_with_a_reason(self):
        self.run_cli(["ask", "--workdir", str(self.workdir), "écris"], scenario="permission",
                     answers=["n", "trop tôt"])
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertEqual(read_log(self.log)["answers"][0]["response"],
                         {"behavior": "deny", "message": "trop tôt"})

    def test_bypass_needs_typed_confirmation(self):
        code = self.run_cli(["ask", "--workdir", str(self.workdir), "--mode", "bypass", "x"],
                            answers=["non"])
        self.assertEqual(code, 2)
        self.assertIn("sans rien demander", self.text())
        self.assertIn("rien n'a été lancé", self.text())
        self.assertEqual(store.list_conversations(self.conn()), [])
        self.assertFalse(self.log.exists())

        self.output.clear()
        code = self.run_cli(["ask", "--workdir", str(self.workdir), "--mode", "bypass", "x"],
                            answers=["oui"])
        self.assertEqual(code, 0)
        args = read_log(self.log)["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")

    def test_error_is_explained(self):
        code = self.run_cli(["ask", "--workdir", str(self.workdir), "x"], scenario="not_logged_in")
        self.assertEqual(code, 1)
        self.assertIn("claude auth login", self.text())

    def test_bad_arguments(self):
        self.assertEqual(self.run_cli(["ask", "x"]), 2)
        self.assertIn("--workdir", self.text())
        self.assertEqual(self.run_cli(["ask", "--workdir", str(self.workdir / "absent"), "x"]), 2)
        self.assertEqual(self.run_cli(["ask", "--conversation", "9", "x"]), 1)
        self.assertIn("conversation introuvable", self.text())


class OtherCommandTests(CliTestCase):
    def test_status(self):
        self.assertEqual(self.run_cli(["status"], logged_in=True), 0)
        self.assertIn("Connecté (claude.ai)", self.text())
        self.output.clear()
        self.assertEqual(self.run_cli(["status"], logged_in=False), 1)
        self.assertIn("auth login", self.text())

    def test_quota_refresh(self):
        self.assertEqual(self.run_cli(["quota", "--refresh"]), 0)
        self.assertIn("fenêtre 5 heures : reste 71 % (29 % utilisés)", self.text())
        self.assertIn("fenêtre 7 jours : reste 87 % (13 % utilisés)", self.text())

    def test_quota_and_conversations(self):
        self.run_cli(["quota"])
        self.assertIn("inconnu", self.text())
        self.run_cli(["conversations"])
        self.assertIn("Aucune conversation", self.text())
        self.run_cli(["ask", "--workdir", str(self.workdir), "--title", "Mon projet", "x"])
        self.output.clear()
        self.run_cli(["quota"])
        self.assertIn("bientôt atteint", self.text())
        self.run_cli(["conversations"])
        self.assertIn("Mon projet", self.text())


class PasswordAndServeTests(CliTestCase):
    def set_password(self, *typed):
        typed = list(typed)
        return cli.main(["--db", self.db_path, "mot-de-passe"], out=self.out,
                        secret=lambda prompt: typed.pop(0))

    def serve(self, *argv):
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            self.skipTest("web dependencies missing")
        with mock.patch("uvicorn.run") as run:
            code = cli.main(["--db", self.db_path, "serve", *argv], out=self.out, command=FAKE_COMMAND)
        return code, run

    def test_password_is_typed_twice_and_kept_hashed(self):
        password = secrets.token_urlsafe(18)
        self.assertEqual(self.set_password(password, password + "x"), 1)
        self.assertIn("diffèrent", self.text())
        self.assertFalse(auth.password_set(self.conn()))
        self.assertEqual(self.set_password("court", "court"), 1)
        self.assertIn("au moins 12", self.text())
        self.assertEqual(self.set_password(password, password), 0)
        stored = self.conn().execute("SELECT password_hash FROM auth_password").fetchone()[0]
        self.assertTrue(auth.verify_password(password, stored))
        self.assertNotIn(password, stored)

    def test_serve_refuses_to_start_without_a_password(self):
        code, run = self.serve()
        self.assertEqual(code, 2)
        self.assertIn("python -m aihub mot-de-passe", self.text())
        run.assert_not_called()

    def test_serve_with_a_password(self):
        password = secrets.token_urlsafe(18)
        self.set_password(password, password)
        code, run = self.serve("--host", "100.64.0.1", "--sauvegardes", str(self.workdir / "s"))
        self.assertEqual(code, 0)
        app = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs["host"], "100.64.0.1")
        self.assertIn("(mot de passe demandé)", self.text())
        self.assertEqual(str(app.state.backup.folder), str(self.workdir / "s"))

    def test_serve_without_password_stays_on_this_computer(self):
        code, run = self.serve("--sans-mot-de-passe", "--host", "0.0.0.0")
        self.assertEqual(code, 2)
        self.assertIn("refusé", self.text())
        run.assert_not_called()
        code, run = self.serve("--sans-mot-de-passe", "--sans-sauvegarde")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        app = run.call_args.args[0]
        self.assertIsNone(app.state.backup)
        self.assertIn("Sauvegarde quotidienne : désactivée", self.text())
        # What was started answers this computer only, and no proxy.
        from fastapi.testclient import TestClient
        with TestClient(app, client=("127.0.0.1", 1)) as local:
            self.assertEqual(local.get("/api/conversations").status_code, 200)
            self.assertEqual(local.get("/api/conversations", headers={"X-Forwarded-For": "100.64.0.2"}).status_code, 403)

    def test_serve_with_a_password_asks_for_it(self):
        password = secrets.token_urlsafe(18)
        self.set_password(password, password)
        code, run = self.serve("--sans-sauvegarde")
        from fastapi.testclient import TestClient
        with TestClient(run.call_args.args[0], client=("127.0.0.1", 1)) as client:
            self.assertEqual(client.get("/api/conversations").status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={"password": password}).status_code, 200)
            self.assertEqual(client.get("/api/conversations").status_code, 200)


if __name__ == "__main__":
    unittest.main()
