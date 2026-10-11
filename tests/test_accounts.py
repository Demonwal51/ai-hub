"""Version 0.2: each person runs their requests with their own AI accounts."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from aihub import accounts, db, notify, store, tools
from aihub.config import Settings
from aihub.providers import codex

from .helpers import FAKE_CODEX, FAKE_COMMAND, FAKE_COMMANDS, TempDirMixin, fake_env, read_log
from .test_sharing import H, SharingTestCase, wait_idle

GUEST = {"id": 7, "name": "Lou", "is_owner": False}
OWNER = {"id": 1, "name": "Propriétaire", "is_owner": True}


class RunEnvTest(TempDirMixin, unittest.TestCase):
    def test_the_owner_keeps_the_computer_s_accounts(self):
        base = {"PATH": "x", "ANTHROPIC_API_KEY": "k"}
        self.assertIs(accounts.run_env(base, OWNER, self.make_tempdir()), base)
        self.assertIs(accounts.run_env(base, None, self.make_tempdir()), base)  # a hub without accounts

    def test_a_guest_gets_folders_of_their_own_and_no_key_of_the_computer(self):
        root = self.make_tempdir()
        base = {"PATH": "x", **{name: "secret" for name in accounts.SECRET_NAMES}}
        env = accounts.run_env(base, GUEST, root)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(root / "7" / "claude"))
        self.assertEqual(env["CODEX_HOME"], str(root / "7" / "codex"))
        self.assertTrue(Path(env["CODEX_HOME"]).is_dir() and Path(env["CLAUDE_CONFIG_DIR"]).is_dir())
        self.assertEqual([n for n in accounts.SECRET_NAMES if n in env], [])
        self.assertEqual(env["PATH"], "x")
        self.assertIn("ANTHROPIC_API_KEY", base)  # the hub's own environment is left as it was
        self.assertEqual(accounts.account_vars(env), {"CLAUDE_CONFIG_DIR": env["CLAUDE_CONFIG_DIR"],
                                                      "CODEX_HOME": env["CODEX_HOME"]})
        self.assertEqual(accounts.account_vars(base), {})

    def test_two_bases_in_one_folder_never_share_a_persons_accounts(self):
        from aihub.web.jobs import side_folder
        root = self.make_tempdir()
        self.assertNotEqual(side_folder(root / "aihub.db", "comptes"), side_folder(root / "essai.db", "comptes"))
        self.assertEqual(side_folder(root / "aihub.db", "comptes"), (root / "aihub-comptes").resolve())

    def test_a_deleted_person_s_folders_go(self):
        root = self.make_tempdir()
        accounts.run_env({}, GUEST, root)
        hub = accounts.Accounts(root, commands=FAKE_COMMANDS)
        hub.forget(7)
        self.assertFalse((root / "7").exists())

    def test_a_delegation_is_paid_by_the_same_person(self):
        tmp = self.make_tempdir()
        conn = db.connect(str(tmp / "hub.db"))
        self.addCleanup(conn.close)
        cid = store.create_conversation(conn, title="T", workdir=str(tmp))
        store.set_tools(conn, cid, [tools.DELEGATE])
        conv = store.get_conversation(conn, cid)
        settings = Settings(models=["sonnet", "gpt-5.6-sol"])
        with mock.patch.object(tools, "delegate_models", return_value=["sonnet"]):
            mine = tools.for_run(conn, conv, settings, account_env={"CODEX_HOME": "C:/h"})
            computer = tools.for_run(conn, conv, settings)
        self.assertEqual(mine.mcp_config["mcpServers"][tools.DELEGATE]["env"], {"CODEX_HOME": "C:/h"})
        self.assertNotIn("env", computer.mcp_config["mcpServers"][tools.DELEGATE])

    def test_codex_leaves_out_the_person_s_own_extensions(self):
        tmp = self.make_tempdir()
        home = tmp / "codex"
        home.mkdir()
        (home / "config.toml").write_text('[mcp_servers.outil_de_lou]\ncommand = "x"\n', encoding="utf-8")
        log = tmp / "codex.json"
        env = dict(os.environ, CODEX_HOME=str(home), FAKE_CODEX_LOG=str(log))
        codex.read_usage(FAKE_CODEX, workdir=str(tmp), env=env)
        args = read_log(log)["args"]
        self.assertIn("mcp_servers.outil_de_lou.enabled=false", args)
        self.assertEqual(read_log(log)["codex_home"], str(home))


class QuotaTest(TempDirMixin, unittest.TestCase):
    def test_each_account_has_its_own_quota(self):
        conn = db.connect(str(self.make_tempdir() / "hub.db"))
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO users (id, name, password_hash, created_at) VALUES (2, 'Lou', 'x', 'n')")
        store.record_quota(conn, "claude_code", "allowed")
        store.record_quota(conn, "claude_code", "rejected", user_id=2)
        self.assertEqual(store.latest_quota(conn, "claude_code")["status"], "allowed")
        self.assertEqual(store.latest_quota(conn, "claude_code", 2)["status"], "rejected")
        self.assertIsNone(store.latest_quota(conn, "claude_code", 3))

    def test_a_person_s_quota_alert_is_theirs_and_counted_apart(self):
        notifier = notify.Notifier(str(self.make_tempdir() / "hub.db"), Settings, sync=True)
        sent = []
        info = mock.Mock(utilization=0.9, status="allowed_warning")
        with mock.patch.object(notifier, "notify", lambda *a, **k: sent.append((a, k))):
            notifier.quota("Claude Code", info, 80)       # the computer's
            notifier.quota("Claude Code", info, 80, user_id=2)  # that person's: not yet alerted
            notifier.quota("Claude Code", info, 80, user_id=2)  # already said for that person
        self.assertEqual([k.get("user_id") for _, k in sent], [None, 2])

    def test_an_alert_about_a_person_s_account_goes_to_their_devices_only(self):
        from .test_notify import ENDPOINT, Device, Post
        path = str(self.make_tempdir() / "hub.db")
        conn = db.connect(path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO users (id, name, password_hash, is_owner, created_at) VALUES (1, 'P', 'x', 1, 'n')")
        for uid in (2, 3):
            conn.execute("INSERT INTO users (id, name, password_hash, created_at) VALUES (?, ?, 'x', 'n')",
                         (uid, f"U{uid}"))
        devices = {}
        for uid in (1, 2, 3):
            conn.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at, user_id) "
                         "VALUES (?, 'x', 'x', '9999', ?)", (f"h{uid}", uid))
            sid = conn.execute("SELECT id FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0]
            devices[uid] = Device(f"{ENDPOINT}-{uid}")
            notify.subscribe(conn, devices[uid].subscription, ["quota"], session_id=sid)
        settings = Settings(ntfy_url="https://ntfy.sh/aihub-un-sujet-long")
        post = Post()
        notifier = notify.Notifier(path, lambda: settings, post=post, sync=True)
        notifier.notify("quota", None, "Claude Code : quota atteint", user_id=2)
        self.assertEqual([s["url"] for s in post.sent], [f"{ENDPOINT}-2"])  # no other device, no ntfy
        post.sent.clear()
        notifier.notify("quota", None, "Claude Code : quota atteint")  # the computer's: the owner's channels
        self.assertEqual(sorted(s["url"] for s in post.sent), [f"{ENDPOINT}-1", "https://ntfy.sh/aihub-un-sujet-long"])


class SignInTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        self.log = self.root / "log.json"
        self.env = fake_env("text", self.log)
        self.hub = accounts.Accounts(self.root, commands=FAKE_COMMANDS, env=self.env)

    def wait_done(self, provider, timeout=20):
        login = self.hub.login_of(GUEST, provider)
        self.assertTrue(login.done.wait(timeout), login.view())
        return login.view()

    def test_claude_page_then_the_code_pasted_back(self):
        self.assertFalse(self.hub.status(GUEST)["claude_code"]["signed_in"])
        view = self.hub.start_login(GUEST, "claude_code")
        self.assertEqual((view["state"], view["needs_code"]), ("waiting", True), view)
        self.assertTrue(view["url"].startswith("https://example.test/oauth/authorize"))
        self.assertEqual(read_log(self.log)["browser"], "none")  # never a browser on the server
        done = self.hub.send_code(GUEST, "claude_code", "bon-code")
        self.assertEqual(done["state"], "ok", done)
        self.assertTrue(self.hub.status(GUEST)["claude_code"]["signed_in"])
        self.hub.logout(GUEST, "claude_code")
        self.assertFalse(self.hub.status(GUEST)["claude_code"]["signed_in"])

    def test_claude_refuses_a_wrong_code(self):
        self.hub.start_login(GUEST, "claude_code")
        done = self.hub.send_code(GUEST, "claude_code", "mauvais")
        self.assertEqual(done["state"], "error")
        self.assertIn("Invalid code", done["error"])
        with self.assertRaisesRegex(accounts.AccountError, "aucune connexion"):
            self.hub.send_code(GUEST, "claude_code", "bon-code")  # over: start again
        for bad in ("", "deux mots", "x" * 600):
            self.hub.start_login(GUEST, "claude_code")
            with self.assertRaisesRegex(accounts.AccountError, "code invalide"):
                self.hub.send_code(GUEST, "claude_code", bad)
        self.hub.cancel_login(GUEST, "claude_code")

    def test_codex_shows_a_code_then_learns_the_end(self):
        view = self.hub.start_login(GUEST, "codex")
        self.assertEqual((view["state"], view["code"], view["url"]),
                         ("waiting", "ABCD-1234", "https://example.test/codex/device"), view)
        self.assertEqual(self.wait_done("codex")["state"], "ok")
        self.assertTrue(self.hub.status(GUEST)["codex"]["signed_in"])
        self.assertFalse(self.hub.status({**GUEST, "id": 8})["codex"]["signed_in"])  # someone else: not them
        self.hub.logout(GUEST, "codex")
        self.assertFalse(self.hub.status(GUEST)["codex"]["signed_in"])

    def test_codex_refusal_and_cancel(self):
        self.env["FAKE_CODEX_LOGIN"] = "fail"
        self.hub.start_login(GUEST, "codex")
        done = self.wait_done("codex")
        self.assertEqual(done["state"], "error")
        self.assertIn("code expiré", done["error"])
        self.env["FAKE_CODEX_LOGIN"] = "wait"
        self.hub.start_login(GUEST, "codex")
        login = self.hub.login_of(GUEST, "codex")
        self.hub.cancel_login(GUEST, "codex")
        self.assertEqual(login.view()["state"], "cancelled")
        login._server.proc.wait(10)  # the app server does not stay behind
        self.assertIsNotNone(login._server.proc.returncode)

    def test_a_sign_in_left_waiting_is_given_up(self):
        self.env["FAKE_CODEX_LOGIN"] = "wait"
        self.hub.start_login(GUEST, "codex")
        login = self.hub.login_of(GUEST, "codex")
        login.started -= accounts.LOGIN_TIMEOUT_S + 1
        self.assertEqual(self.hub.login_of(GUEST, "codex").view()["state"], "cancelled")
        self.assertIn("trop longue", login.view()["error"])

    def test_the_owner_signs_in_the_computers_accounts_but_never_out(self):
        view = self.hub.start_login(OWNER, "codex")
        self.assertEqual((view["state"], view["code"]), ("waiting", "ABCD-1234"), view)
        self.assertEqual(self.wait_owner("codex")["state"], "ok")
        self.hub.login_of(OWNER, "codex")._server.proc.wait(10)  # the app server is gone
        with self.assertRaisesRegex(accounts.AccountError, "terminal"):
            self.hub.logout(OWNER, "codex")  # Claude Code and Codex use them outside the hub too
        with self.assertRaisesRegex(accounts.AccountError, "inconnu"):
            self.hub.start_login(GUEST, "ollama")

    def wait_owner(self, provider, timeout=20):
        login = self.hub.login_of(OWNER, provider)
        self.assertTrue(login.done.wait(timeout), login.view())
        return login.view()


class WebAccountsTest(SharingTestCase):
    def test_a_guest_s_request_uses_their_account(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, "write", conversation_id=cid)
        log = Path(self.env["FAKE_CLAUDE_LOG"])
        guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        data = wait_idle(guest, cid)
        folder = self.app.state.jobs.accounts_root / str(uid) / "claude"
        self.assertEqual(read_log(log)["config_dir"], str(folder))
        reply = data["messages"][-1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("Mes comptes IA", reply["error"])  # not signed in: never the owner's account instead
        # Signed in from the hub: the next request goes through, on their account.
        self.assertEqual(guest.get("/api/accounts", headers=H).json()["providers"]["claude_code"]["signed_in"], False)
        view = guest.post("/api/accounts/claude_code/login", headers=H).json()
        self.assertTrue(view["needs_code"])
        self.assertEqual(guest.post("/api/accounts/claude_code/code", json={"code": "bon-code"}, headers=H).json()["state"],
                         "ok")
        guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        self.assertEqual(wait_idle(guest, cid)["messages"][-1]["status"], "ok")
        # The owner's own requests still use the computer's account.
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        wait_idle(self.owner, cid)
        self.assertIsNone(read_log(log)["config_dir"])

    def test_accounts_page_for_the_owner_and_deletion(self):
        # The owner's are the computer's: signed in here (the fake says so), ready to work.
        mine = self.owner.get("/api/accounts", headers=H).json()
        self.assertEqual((mine["own"], mine["ready"], mine["providers"]["claude_code"]["signed_in"]), (False, True, True))
        self.assertEqual(self.owner.post("/api/accounts/claude_code/logout", headers=H).status_code, 400)
        guest, uid = self.guest()
        guest.get("/api/accounts", headers=H)
        folder = self.app.state.jobs.accounts_root / str(uid)
        self.assertTrue(folder.is_dir())
        self.owner.delete(f"/api/users/{uid}", headers=H)
        self.assertFalse(folder.exists())

    def signed_in_guest(self):
        guest, uid = self.guest()
        guest.post("/api/accounts/claude_code/login", headers=H)
        self.assertEqual(guest.post("/api/accounts/claude_code/code", json={"code": "bon-code"}, headers=H)
                         .json()["state"], "ok")
        return guest, uid

    def test_a_delegation_asked_by_a_guest_names_their_account(self):
        guest, uid = self.signed_in_guest()
        cid = self.conversation()
        self.share(uid, "write", conversation_id=cid)
        conn = db.connect(self.db_path)
        store.set_tools(conn, cid, [tools.DELEGATE])
        conn.close()
        log = Path(self.env["FAKE_CLAUDE_LOG"])
        with mock.patch.object(tools, "delegate_models", return_value=["sonnet"]):
            guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
            wait_idle(guest, cid)
        args = read_log(log)["args"]
        servers = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
        folder = self.app.state.jobs.accounts_root / str(uid)
        self.assertEqual(servers[tools.DELEGATE]["env"], {"CLAUDE_CONFIG_DIR": str(folder / "claude"),
                                                          "CODEX_HOME": str(folder / "codex")})

    def test_a_plan_launched_by_a_guest_runs_on_their_account(self):
        runs = self.make_tempdir()
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(runs)
        self.env["FAKE_CLAUDE_SCENARIO"] = "auto"  # a plan, its subtasks, the synthesis
        guest, uid = self.signed_in_guest()
        r = self.owner.post("/api/conversations", json={"title": "Plan", "workdir": str(self.workdir),
                                                         "mode": "orchestration", "conductor_model": "opus"}, headers=H)
        cid = r.json()["conversation"]["id"]
        self.share(uid, "write", conversation_id=cid)
        agent = self.owner.post("/api/agents", json={"model": "sonnet"}, headers=H).json()["agent"]["id"]
        r = guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "fais le projet"}, headers=H)
        data = wait_idle(guest, cid)
        self.assertTrue(data["plans"], (r.text, data["messages"]))
        plan = data["plans"][0]
        self.assertEqual(guest.post(f"/api/plans/{plan['id']}/assign-all", json={"agent_id": agent}, headers=H)
                         .status_code, 200)
        self.assertEqual(guest.post(f"/api/plans/{plan['id']}/launch", headers=H).status_code, 202)
        self.assertEqual(wait_idle(guest, cid)["plans"][0]["status"], "done")
        folder = str(self.app.state.jobs.accounts_root / str(uid) / "claude")
        dirs = [read_log(p).get("config_dir", "?") for p in runs.glob("run-*.json")]
        dirs = [d for d in dirs if d != "?"]  # the sign-in's own runs log no request
        self.assertGreaterEqual(len(dirs), 5)  # the plan, three subtasks, the synthesis
        self.assertEqual(set(dirs), {folder})

    def test_a_guest_s_quota_read_and_kept_on_their_account(self):
        guest, uid = self.signed_in_guest()
        data = guest.get("/api/quota?refresh=true", headers=H).json()
        self.assertTrue(data["own_accounts"])
        folder = self.app.state.jobs.accounts_root / str(uid) / "claude"
        self.assertEqual(read_log(Path(self.env["FAKE_CLAUDE_LOG"]))["usage_config_dir"], str(folder))
        conn = db.connect(self.db_path)
        rows = conn.execute("SELECT user_id FROM quota_observations").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], [uid])
        self.assertIsNotNone(data["claude_code"])
        self.assertIsNone(self.owner.get("/api/quota", headers=H).json()["claude_code"])  # not the owner's

    def test_a_quota_met_during_a_guest_s_request_is_theirs(self):
        guest, uid = self.signed_in_guest()
        cid = self.conversation()
        self.share(uid, "write", conversation_id=cid)
        self.env["FAKE_CLAUDE_SCENARIO"] = "quota"
        guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        wait_idle(guest, cid)
        conn = db.connect(self.db_path)
        rows = conn.execute("SELECT user_id, status FROM quota_observations").fetchall()
        conn.close()
        self.assertEqual([tuple(r) for r in rows], [(uid, "rejected")])

    def test_each_person_sees_their_own_quota(self):
        guest, uid = self.guest()
        conn = db.connect(self.db_path)
        store.record_quota(conn, "claude_code", "rejected", user_id=uid)
        store.record_quota(conn, "claude_code", "allowed")
        conn.close()
        self.assertEqual(guest.get("/api/quota", headers=H).json()["claude_code"]["status"], "rejected")
        mine = self.owner.get("/api/quota", headers=H).json()
        self.assertEqual((mine["claude_code"]["status"], mine["own_accounts"]), ("allowed", False))


if __name__ == "__main__":
    unittest.main()
