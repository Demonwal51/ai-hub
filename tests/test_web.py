import json
import threading
import time
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:  # web dependencies not installed: pip install -r requirements-dev.txt
    raise unittest.SkipTest("fastapi et httpx ne sont pas installés")

from aihub import db, routing, store
from aihub.web.app import create_app
from tests.helpers import FAKE_CODEX, FAKE_COMMAND, TempDirMixin, fake_env, read_log, wait_for_log_key


def wait_until(predicate, timeout=20, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"condition non remplie en {timeout} s")


class WebTestCase(TempDirMixin, unittest.TestCase):
    scenario = "text"
    app_kwargs = {}

    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.log = tmp / "log.json"
        self.workdir = self.make_tempdir()
        self.env = fake_env(self.scenario, self.log)
        self.env["FAKE_CLAUDE_LOGGED_IN"] = "1"
        self.app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, sse_ping_s=0.1,
                              **self.app_kwargs)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def use_scenario(self, scenario):
        self.env["FAKE_CLAUDE_SCENARIO"] = scenario

    def new_conversation(self, **body):
        body = {"title": "Essai", "workdir": str(self.workdir), **body}
        response = self.client.post("/api/conversations", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["conversation"]["id"]

    def conversation(self, cid):
        return self.client.get(f"/api/conversations/{cid}").json()

    def send(self, cid, prompt="salut"):
        response = self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": prompt})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["message_id"]

    def wait_idle(self, cid):
        wait_until(lambda: not self.conversation(cid)["running"])
        return self.conversation(cid)

    def wait_pending(self, cid):
        return wait_until(lambda: self.conversation(cid)["pending"])[0]


class PageTests(WebTestCase):
    def test_index_and_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("AI Hub", page.text)
        for asset in ("/static/app.js", "/static/style.css"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)

    def test_options_carry_the_server_warning(self):
        options = self.client.get("/api/options").json()
        self.assertEqual(options["bypass_warning"], store.BYPASS_WARNING)
        self.assertIn("default", options["models"])

    def test_status(self):
        status = self.client.get("/api/status").json()
        self.assertTrue(status["logged_in"])
        # A test's own Claude Code: the real Codex of the computer is not searched for.
        self.assertEqual((status["codex"]["offered"], status["codex"]["cli"]), (False, None))
        self.assertNotIn("codex", self.client.get("/api/options").json()["models"])


class NotificationWebTests(WebTestCase):
    def setUp(self):
        from tests.test_notify import Device, Post
        self.push = Post()
        self.app_kwargs = {"push_post": self.push}
        super().setUp()
        self.phone = Device()

    def subscribe(self, events):
        r = self.client.post("/api/push/subscription", json={"subscription": self.phone.subscription,
                                                             "events": events})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["subscription"]

    def alerts(self):
        self.assertTrue(self.app.state.notifier.flush())
        return [self.phone.decrypt(s["content"]) for s in self.push.sent]

    def test_a_finished_reply_reaches_the_phone(self):
        self.assertEqual(len(self.client.get("/api/push/key").json()["public_key"]), 87)  # 65 bytes, base64url
        self.assertEqual(self.subscribe(["done"])["events"], ["done"])
        cid = self.new_conversation()
        self.send(cid)
        self.wait_idle(cid)
        alerts = self.alerts()
        self.assertEqual([(a["event"], a["conversation_id"]) for a in alerts], [("done", cid)])
        self.assertIn("« Essai »", alerts[0]["body"])

    def test_the_agent_waiting_for_an_answer(self):
        self.subscribe(["permission"])
        self.use_scenario("permission")
        cid = self.new_conversation()
        self.send(cid, "écris")
        pending = self.wait_pending(cid)
        self.client.post(f"/api/permissions/{pending['id']}", json={"allow": False})
        self.wait_idle(cid)
        alerts = self.alerts()
        self.assertEqual([a["event"] for a in alerts], ["permission"])  # not « done »: not chosen
        self.assertIn("l'IA demande", alerts[0]["body"])

    def test_a_failure(self):
        self.subscribe(["error", "done"])
        self.use_scenario("not_logged_in")
        cid = self.new_conversation()
        self.send(cid)
        self.wait_idle(cid)
        self.assertEqual([a["event"] for a in self.alerts()], ["error"])

    def test_state_test_and_unsubscribe(self):
        endpoint = self.phone.subscription["endpoint"]
        self.assertIsNone(self.client.post("/api/push/state", json={"endpoint": endpoint}).json()["subscription"])
        self.subscribe(["quota"])
        self.client.post("/api/push/test")
        self.assertTrue(self.alerts()[0]["test"])  # even though « done » is not chosen
        self.assertTrue(self.client.post("/api/push/unsubscribe", json={"endpoint": endpoint}).json()["removed"])
        bad = self.client.post("/api/push/subscription", json={"subscription": {
            **self.phone.subscription, "endpoint": "https://evil.example/x"}, "events": ["done"]})
        self.assertEqual(bad.status_code, 400)


class AutonomousWebTests(WebTestCase):
    scenario = "auto"

    def setUp(self):
        from tests.test_notify import Device, Post
        self.push = Post()
        self.app_kwargs = {"push_post": self.push}
        super().setUp()
        self.phone = Device()
        self.client.post("/api/push/subscription", json={"subscription": self.phone.subscription,
                                                         "events": ["autonomous_done"]})

    def test_a_run_from_start_to_end(self):
        cid = self.new_conversation(permission_mode="plan")
        r = self.client.post(f"/api/conversations/{cid}/autonomous",
                             json={"goal": "écris les tours", "max_rounds": 4, "max_minutes": 5})
        self.assertEqual(r.status_code, 202, r.text)
        run_id = r.json()["run_id"]
        conv = self.wait_idle(cid)
        run = conv["autonomous"][0]
        self.assertEqual((run["id"], run["status"], run["rounds"]), (run_id, "done", 2))
        self.assertEqual([m["role"] for m in conv["messages"]], ["user", "assistant", "assistant"])
        self.assertEqual([r["message_id"] for r in run["round_list"]], [m["id"] for m in conv["messages"][1:]])
        self.assertTrue(self.app.state.notifier.flush())
        alert = self.phone.decrypt(self.push.sent[0]["content"])
        self.assertEqual(alert["event"], "autonomous_done")
        self.assertIn("objectif atteint (2 tour(s))", alert["body"])

    def test_refusals(self):
        cid = self.new_conversation()
        r = self.client.post(f"/api/conversations/{cid}/autonomous", json={"goal": "x", "check_command": "pytest"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "check_confirmation_required"))
        r = self.client.post(f"/api/conversations/{cid}/autonomous", json={"goal": "x", "max_rounds": 99})
        self.assertEqual(r.status_code, 400)
        orchestra = self.new_conversation(mode="orchestration", conductor_model="opus")
        r = self.client.post(f"/api/conversations/{orchestra}/autonomous", json={"goal": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.conversation(cid)["messages"], [])  # nothing written by a refusal


class ScheduleWebTests(WebTestCase):
    scenario = "auto"

    def setUp(self):
        from datetime import datetime, timezone
        from tests.test_notify import Device, Post
        self.now = {"t": datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)}
        self.push = Post()
        self.app_kwargs = {"push_post": self.push, "run_scheduler": False, "schedule_clock": lambda: self.now["t"]}
        super().setUp()
        self.phone = Device()
        self.client.post("/api/push/subscription", json={"subscription": self.phone.subscription,
                                                         "events": ["scheduled", "done"]})

    def test_a_task_runs_when_due(self):
        from datetime import timedelta
        cid = self.new_conversation()
        r = self.client.post("/api/schedules", json={"conversation_id": cid, "prompt": "salut",
                                                     "every": "interval", "interval_minutes": 60})
        self.assertEqual(r.status_code, 201, r.text)
        task = r.json()["schedule"]
        self.assertEqual(task["rule"], "toutes les 1 h")
        self.now["t"] += timedelta(minutes=61)
        self.assertEqual(self.app.state.scheduler.tick(), [task["id"]])
        conv = self.wait_idle(cid)
        self.assertEqual([m["content"] for m in conv["messages"]], ["salut", "Bonjour"])
        listed = self.client.get(f"/api/schedules?conversation_id={cid}").json()["schedules"]
        self.assertEqual(listed[0]["last_status"], "ok")
        self.assertTrue(self.app.state.notifier.flush())
        alerts = [self.phone.decrypt(s["content"]) for s in self.push.sent]
        self.assertEqual([a["event"] for a in alerts], ["scheduled"])  # one alert of its own, not « done » too
        self.assertIn("réussie", alerts[0]["body"])

    def test_run_now_pause_and_delete(self):
        cid = self.new_conversation()
        sid = self.client.post("/api/schedules", json={"conversation_id": cid, "prompt": "salut",
                                                       "at_time": "08:00"}).json()["schedule"]["id"]
        self.assertEqual(self.client.post(f"/api/schedules/{sid}/run").status_code, 202)
        self.wait_idle(cid)
        off = self.client.patch(f"/api/schedules/{sid}", json={"enabled": False}).json()["schedule"]
        self.assertFalse(off["enabled"])
        self.assertEqual(self.client.delete(f"/api/schedules/{sid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/schedules/{sid}").status_code, 404)
        bad = self.client.post("/api/schedules", json={"conversation_id": cid, "prompt": "x", "at_time": "25:00"})
        self.assertEqual(bad.status_code, 400)
        unconfirmed = self.client.post("/api/schedules", json={"conversation_id": cid, "prompt": "x",
                                                               "at_time": "08:00", "kind": "autonomous",
                                                               "check_command": "pytest"})
        self.assertEqual((unconfirmed.status_code, unconfirmed.json()["code"]), (409, "check_confirmation_required"))


class LocalWebTests(WebTestCase):
    def setUp(self):
        from tests.fake_ollama import FakeOllama
        self.fake = FakeOllama().__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        self.app_kwargs = {"ollama_url": self.fake.url}
        super().setUp()
        self.addCleanup(routing.set_available, {"claude_code": None})

    def test_status_models_and_a_reply(self):
        local = self.client.get("/api/status").json()["local"]
        self.assertEqual((local["running"], local["models"]), (True, ["local:qwen3.5:4b", "local:llama3.2:3b"]))
        self.assertIn("local:qwen3.5:4b", self.client.get("/api/options").json()["models"])
        cid = self.new_conversation(model="local:qwen3.5:4b")
        self.send(cid, "bonjour")
        reply = self.wait_idle(cid)["messages"][-1]
        self.assertEqual((reply["status"], reply["content"], reply["provider"]), ("ok", "Bonjour", "ollama"))
        refused = self.client.post("/api/conversations", json={"title": "x", "workdir": str(self.workdir),
                                                               "model": "local:absent"})
        self.assertEqual(refused.status_code, 400)

    def test_stopped_ollama(self):
        self.fake.__exit__(None, None, None)
        local = self.client.get("/api/status").json()["local"]
        self.assertFalse(local["running"])
        self.assertIn("ollama serve", local["error"])


class CodexWebTests(WebTestCase):
    app_kwargs = {"codex_command": FAKE_CODEX}

    def setUp(self):
        super().setUp()
        self.env["FAKE_CODEX_MODELS"] = "gpt-5.6-sol,gpt-5.6-luna"
        self.addCleanup(routing.set_available, {"claude_code": None})

    def test_status_and_models_offered(self):
        gpt = self.client.get("/api/status").json()["codex"]
        self.assertEqual((gpt["logged_in"], gpt["models"]), (True, ["gpt-5.6-sol", "gpt-5.6-luna"]))
        models = self.client.get("/api/options").json()["models"]
        self.assertIn("codex", models)
        self.assertIn("gpt-5.6-luna", models)
        self.assertNotIn("gpt-6-astra", models)  # needs a newer Codex than this one
        self.assertIn("opus", models)
        self.assertEqual(routing.available()["codex"], {"gpt-5.6-sol", "gpt-5.6-luna"})
        response = self.client.post("/api/conversations", json={"title": "x", "workdir": str(self.workdir),
                                                                "model": "gpt-6-astra"})
        self.assertEqual(response.status_code, 400)

    def test_a_codex_conversation(self):
        cid = self.new_conversation(model="gpt-5.6-luna")
        self.send(cid, "bonjour")
        conv = self.wait_idle(cid)
        reply = conv["messages"][-1]
        self.assertEqual((reply["status"], reply["content"], reply["provider"]), ("ok", "Bonjour", "codex"))

    def test_changing_the_ai_of_a_conversation(self):
        cid = self.new_conversation(model="haiku")
        self.send(cid, "on garde le vert sapin")
        self.wait_idle(cid)
        response = self.client.patch(f"/api/conversations/{cid}", json={"model": "gpt-5.6-luna"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.conversation(cid)["conversation"]["model"], "gpt-5.6-luna")
        self.send(cid, "et le logo ?")
        reply = self.wait_idle(cid)["messages"][-1]
        self.assertEqual((reply["status"], reply["provider"]), ("ok", "codex"))
        refused = self.client.patch(f"/api/conversations/{cid}", json={"model": "gpt-6-astra"})
        self.assertEqual(refused.status_code, 400)  # not offered by this Codex
        conn = db.connect(self.db_path)
        try:
            store.start_request(conn, cid, store.get_conversation(conn, cid)["agent_id"])  # a request in progress
        finally:
            conn.close()
        busy = self.client.patch(f"/api/conversations/{cid}", json={"model": "haiku"})
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(self.conversation(cid)["conversation"]["model"], "gpt-5.6-luna")

    def test_quota_of_both(self):
        data = self.client.get("/api/quota?refresh=true").json()
        self.assertTrue(data["codex_connected"])
        self.assertEqual(data["codex"]["utilization"], 0.42)
        self.assertIsNone(data["codex_error"])

    def test_codex_logged_out(self):
        self.env["FAKE_CODEX_LOGGED_OUT"] = "1"
        self.assertFalse(self.client.get("/api/status").json()["codex"]["logged_in"])
        self.assertNotIn("codex", self.client.get("/api/options").json()["models"])
        data = self.client.get("/api/quota?refresh=true").json()
        self.assertEqual((data["codex_connected"], data["codex"]), (False, None))


class ConversationApiTests(WebTestCase):
    def test_create_list_get(self):
        cid = self.new_conversation(title="Mon projet", model="haiku")
        listed = self.client.get("/api/conversations").json()["conversations"]
        self.assertEqual([(c["id"], c["title"], c["running"]) for c in listed], [(cid, "Mon projet", False)])
        data = self.conversation(cid)
        self.assertEqual((data["conversation"]["model"], data["messages"], data["pending"]),
                         ("haiku", [], []))

    def test_workdir_checks(self):
        for workdir, fragment in (("relatif", "chemin complet"),
                                  (str(self.workdir / "absent"), "n'existe pas")):
            with self.subTest(workdir=workdir):
                r = self.client.post("/api/conversations", json={"title": "t", "workdir": workdir})
                self.assertEqual(r.status_code, 400)
                self.assertIn(fragment, r.json()["detail"])

    def test_unknown_model_and_mode(self):
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": str(self.workdir),
                                                         "model": "gpt"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": str(self.workdir),
                                                         "permission_mode": "auto"})
        self.assertEqual(r.status_code, 400)

    def test_bypass_needs_confirmation(self):
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": str(self.workdir),
                                                         "permission_mode": "bypass"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "bypass_confirmation_required")
        self.assertEqual(self.client.get("/api/conversations").json()["conversations"], [])
        cid = self.new_conversation(permission_mode="bypass", confirm_bypass=True)
        self.assertEqual(self.conversation(cid)["conversation"]["permission_mode"], "bypass")

    def test_rename_and_change_mode(self):
        cid = self.new_conversation()
        r = self.client.patch(f"/api/conversations/{cid}", json={"title": "Nouveau"})
        self.assertEqual(r.json()["conversation"]["title"], "Nouveau")
        r = self.client.patch(f"/api/conversations/{cid}", json={"permission_mode": "bypass"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "bypass_confirmation_required"))
        r = self.client.patch(f"/api/conversations/{cid}", json={"permission_mode": "bypass",
                                                                 "confirm_bypass": True})
        self.assertEqual(r.json()["conversation"]["permission_mode"], "bypass")

    def test_a_long_conversation_comes_in_part(self):
        cid = self.new_conversation()
        conn = db.connect(self.db_path)
        ids = [store.add_user_message(conn, cid, f"message {i}") for i in range(5)]
        conn.close()
        data = self.client.get(f"/api/conversations/{cid}?last=2").json()
        self.assertEqual(([m["id"] for m in data["messages"]], data["older"]), (ids[-2:], 3))
        whole = self.client.get(f"/api/conversations/{cid}").json()
        self.assertEqual(([m["id"] for m in whole["messages"]], whole["older"]), (ids, 0))
        self.assertEqual(self.client.get(f"/api/conversations/{cid}?last=9").json()["older"], 0)
        for bad in (0, 5001):
            self.assertEqual(self.client.get(f"/api/conversations/{cid}?last={bad}").status_code, 400)

    def test_unknown_conversation(self):
        for method, path in (("get", "/api/conversations/99"), ("delete", "/api/conversations/99"),
                             ("get", "/api/conversations/99/events")):
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 404)

    def test_delete(self):
        cid = self.new_conversation()
        self.send(cid)
        self.wait_idle(cid)
        self.assertEqual(self.client.delete(f"/api/conversations/{cid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}").status_code, 404)


class AllowedRootsTests(WebTestCase):
    def test_workdir_must_be_under_an_allowed_root(self):
        root = self.make_tempdir()
        inside = root / "projet"
        inside.mkdir()
        app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, allowed_roots=[root])
        with TestClient(app) as client:
            r = client.post("/api/conversations", json={"title": "t", "workdir": str(self.workdir)})
            self.assertEqual(r.status_code, 400)
            self.assertIn("non autorisé", r.json()["detail"])
            r = client.post("/api/conversations", json={"title": "t", "workdir": str(inside)})
            self.assertEqual(r.status_code, 201)


class RequestTests(WebTestCase):
    def test_reply_and_quota(self):
        cid = self.new_conversation()
        mid = self.send(cid, "dis bonjour")
        data = self.wait_idle(cid)
        user, reply = data["messages"]
        self.assertEqual((user["content"], reply["id"], reply["content"], reply["status"]),
                         ("dis bonjour", mid, "Bonjour", "ok"))
        quota = self.client.get("/api/quota").json()["claude_code"]
        self.assertEqual(quota["status"], "allowed_warning")
        self.assertEqual(quota["windows"]["five_hour"]["utilization"], 0.82)

    def test_one_request_at_a_time_and_stop(self):
        self.use_scenario("hang")
        cid = self.new_conversation()
        self.send(cid)
        wait_for_log_key(self.log, "child_pid")
        self.assertTrue(self.conversation(cid)["running"])
        r = self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": "encore"})
        self.assertEqual(r.status_code, 409)
        r = self.client.delete(f"/api/conversations/{cid}")
        self.assertEqual(r.status_code, 409)
        # The permission mode cannot change under a running agent.
        r = self.client.patch(f"/api/conversations/{cid}", json={"permission_mode": "plan"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.conversation(cid)["conversation"]["permission_mode"], "manual")
        self.assertTrue(self.client.post(f"/api/conversations/{cid}/stop").json()["stopped"])
        data = self.wait_idle(cid)
        self.assertEqual(data["messages"][-1]["status"], "stopped")
        self.assertFalse(self.client.post(f"/api/conversations/{cid}/stop").json()["stopped"])
        self.use_scenario("text")
        self.send(cid, "encore")
        self.assertEqual(self.wait_idle(cid)["messages"][-1]["content"], "Bonjour")

    def test_error_is_shown(self):
        self.use_scenario("not_logged_in")
        cid = self.new_conversation()
        self.send(cid)
        reply = self.wait_idle(cid)["messages"][-1]
        self.assertEqual(reply["status"], "error")
        self.assertIn("claude auth login", reply["error"])

    def test_refusal_before_start_is_returned(self):
        cid = self.new_conversation()
        store_conn = db.connect(self.db_path)
        try:
            store.update_conversation(store_conn, cid, workdir=str(self.workdir / "disparu"))
        finally:
            store_conn.close()
        r = self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": "x"})
        # 400, not 404: the conversation exists, its folder does not.
        self.assertEqual(r.status_code, 400)
        self.assertIn("dossier de travail introuvable", r.json()["detail"])
        self.assertFalse(self.conversation(cid)["running"])


class QuotaRefreshTests(WebTestCase):
    def test_refresh_reads_the_current_quota_once_per_interval(self):
        self.assertIsNone(self.client.get("/api/quota").json()["claude_code"])
        data = self.client.get("/api/quota?refresh=true").json()
        self.assertIsNone(data["error"])
        self.assertEqual(data["claude_code"]["windows"]["five_hour"]["utilization"], 0.29)
        self.assertEqual(data["claude_code"]["windows"]["seven_day"]["utilization"], 0.13)
        again = self.client.get("/api/quota?refresh=true").json()
        self.assertEqual(again["claude_code"]["observed_at"], data["claude_code"]["observed_at"])
        self.assertEqual(read_log(self.log)["usage_calls"], 1)

    def test_refresh_error_is_reported(self):
        self.env["FAKE_USAGE"] = "error"
        data = self.client.get("/api/quota?refresh=true").json()
        self.assertIn("claude auth login", data["error"])
        self.assertIsNone(data["claude_code"])


class ReleaseOrderTests(WebTestCase):
    def test_conversation_is_free_when_done_is_published(self):
        # Checked at the very moment "done" goes out, in the publishing
        # thread: a client could otherwise send its next message in the gap.
        jobs = self.app.state.jobs
        seen = []
        publish = jobs.hub.publish

        def spy(conversation_id, event):
            if event["type"] == "done":
                seen.append(jobs.running(conversation_id))
            publish(conversation_id, event)

        jobs.hub.publish = spy
        cid = self.new_conversation()
        self.send(cid)
        self.wait_idle(cid)
        wait_until(lambda: seen)
        self.assertEqual(seen, [False])


class PermissionTests(WebTestCase):
    scenario = "permission"

    def test_allow_from_the_interface(self):
        cid = self.new_conversation()
        self.send(cid, "écris")
        pending = self.wait_pending(cid)
        self.assertEqual((pending["tool_name"], pending["input"]["file_path"]), ("Write", "note.txt"))
        r = self.client.post(f"/api/permissions/{pending['id']}", json={"allow": True})
        self.assertEqual(r.status_code, 200)
        self.wait_idle(cid)
        self.assertEqual((self.workdir / "note.txt").read_text(encoding="utf-8"), "contenu")
        # Already answered: refused.
        r = self.client.post(f"/api/permissions/{pending['id']}", json={"allow": False})
        self.assertEqual(r.status_code, 404)

    def test_deny_with_a_reason(self):
        cid = self.new_conversation()
        self.send(cid)
        pending = self.wait_pending(cid)
        self.client.post(f"/api/permissions/{pending['id']}", json={"allow": False, "message": "non merci"})
        reply = self.wait_idle(cid)["messages"][-1]
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertEqual(reply["content"], "refusé : non merci")

    def test_unknown_permission(self):
        r = self.client.post("/api/permissions/inconnue", json={"allow": True})
        self.assertEqual(r.status_code, 404)

    def test_stop_while_waiting_for_an_answer(self):
        cid = self.new_conversation()
        self.send(cid)
        pending = self.wait_pending(cid)
        self.client.post(f"/api/conversations/{cid}/stop")
        data = self.wait_idle(cid)
        self.assertEqual(data["messages"][-1]["status"], "stopped")
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertEqual(self.client.post(f"/api/permissions/{pending['id']}",
                                          json={"allow": True}).status_code, 404)


class PermissionTimeoutTests(WebTestCase):
    scenario = "permission"
    app_kwargs = {"permission_timeout_s": 0.3}

    def test_no_answer_means_refusal(self):
        cid = self.new_conversation()
        self.send(cid)
        reply = self.wait_idle(cid)["messages"][-1]
        self.assertFalse((self.workdir / "note.txt").exists())
        self.assertIn("pas de réponse dans le délai", reply["content"])


class QuestionTests(WebTestCase):
    scenario = "question"

    def test_answer_reaches_the_agent(self):
        cid = self.new_conversation()
        self.send(cid)
        pending = self.wait_pending(cid)
        self.assertEqual(pending["tool_name"], "AskUserQuestion")
        r = self.client.post(f"/api/permissions/{pending['id']}", json={"allow": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("répondez", r.json()["detail"])
        r = self.client.post(f"/api/permissions/{pending['id']}",
                             json={"allow": True, "answers": {"Quelle couleur ?": "Bleu"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.wait_idle(cid)["messages"][-1]["content"], "réponse : Bleu")
        sent = read_log(self.log)["answers"][0]["response"]["updatedInput"]
        self.assertEqual(sent["answers"], {"Quelle couleur ?": "Bleu"})
        self.assertEqual(sent["questions"][0]["question"], "Quelle couleur ?")


class EventStreamTests(WebTestCase):
    """Live events over a real server: Starlette's TestClient waits for a
    response to finish before returning it, and an event stream never does."""

    def setUp(self):
        super().setUp()
        import socket

        import httpx
        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port,
                                                    log_level="error", lifespan="off"))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        wait_until(lambda: self.server.started, timeout=10)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20)

        def stop_server():
            self.http.close()
            self.server.should_exit = True
            thread.join(10)

        self.addCleanup(stop_server)

    def read_events(self, cid, until, collected, ready):
        with self.http.stream("GET", f"/api/conversations/{cid}/events") as response:
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                collected.append(event)
                if event["type"] == "snapshot":
                    ready.set()
                if event["type"] == until:
                    return

    def test_live_text_with_offsets(self):
        cid = self.new_conversation()
        collected, ready = [], threading.Event()
        reader = threading.Thread(target=self.read_events, args=(cid, "done", collected, ready),
                                  daemon=True)
        reader.start()
        self.assertTrue(ready.wait(10))
        mid = self.send(cid)
        reader.join(20)
        self.assertFalse(reader.is_alive())
        kinds = [e["type"] for e in collected]
        self.assertEqual(kinds[0], "snapshot")
        self.assertFalse(collected[0]["running"])
        self.assertIn("started", kinds)
        texts = [(e["offset"], e["chunk"]) for e in collected if e["type"] == "text"]
        self.assertEqual(texts, [(0, "Bon"), (3, "jour")])
        done = collected[-1]
        self.assertEqual((done["type"], done["message_id"], done["status"]), ("done", mid, "ok"))
        # Once "done" is announced, the conversation is free: a message sent
        # at once is accepted, not refused as still running.
        self.send(cid, "aussitôt")

    def test_late_subscriber_catches_up(self):
        self.use_scenario("permission")
        cid = self.new_conversation()
        self.send(cid)
        pending = self.wait_pending(cid)
        collected, ready = [], threading.Event()
        reader = threading.Thread(target=self.read_events, args=(cid, "done", collected, ready),
                                  daemon=True)
        reader.start()
        self.assertTrue(ready.wait(10))
        snapshot = collected[0]
        self.assertTrue(snapshot["running"])
        self.assertEqual([p["id"] for p in snapshot["pending"]], [pending["id"]])
        self.client.post(f"/api/permissions/{pending['id']}", json={"allow": True})
        reader.join(20)
        kinds = [e["type"] for e in collected]
        self.assertIn("permission_answered", kinds)
        self.assertEqual(kinds[-1], "done")


class OrchestrationWebTests(WebTestCase):
    scenario = "auto"

    def setUp(self):
        super().setUp()
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())

    def orchestration(self):
        return self.new_conversation(mode="orchestration", conductor_model="opus")

    def plan_of(self, cid):
        return self.conversation(cid)["plans"][-1]

    def use_plan(self, *subtasks, max_parallel=2):
        self.env["FAKE_PLAN"] = json.dumps({"summary": "essai", "max_parallel": max_parallel,
                                            "subtasks": list(subtasks)})

    def sonnet(self):
        r = self.client.post("/api/agents", json={"model": "sonnet"})
        self.assertEqual(r.status_code, 201)
        return r.json()["agent"]["id"]

    def test_plan_then_launch_then_synthesis(self):
        cid = self.orchestration()
        conv = self.conversation(cid)["conversation"]
        self.assertEqual((conv["mode"], conv["conductor_model"]), ("orchestration", "opus"))
        self.send(cid, "fais le projet")
        data = self.wait_idle(cid)
        plan = data["plans"][0]
        self.assertEqual((plan["status"], len(plan["subtasks"])), ("proposed", 3))
        self.assertEqual(data["messages"][-1]["content"],
                         "Plan proposé : Deux parties en parallèle, puis l'assemblage.")
        # Nothing starts while a subtask has no agent.
        r = self.client.post(f"/api/plans/{plan['id']}/launch")
        self.assertEqual(r.status_code, 400)
        self.assertIn("sans agent", r.json()["detail"])
        self.assertEqual(self.plan_of(cid)["status"], "proposed")
        agent = self.sonnet()
        self.assertIn(agent, [a["id"] for a in self.client.get("/api/agents").json()["agents"]])
        self.client.post(f"/api/plans/{plan['id']}/assign-all", json={"agent_id": agent})
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/launch").status_code, 202)
        data = self.wait_idle(cid)
        plan = data["plans"][0]
        self.assertEqual(plan["status"], "done")
        self.assertEqual([s["status"] for s in plan["subtasks"]], ["ok", "ok", "ok"])
        synthesis = data["messages"][-1]
        self.assertEqual((synthesis["id"], synthesis["content"]),
                         (plan["synthesis_message_id"], "Synthèse : 3 réussie(s), 0 en échec."))
        # The plan is closed: the conversation takes a new request.
        self.send(cid, "la suite")
        self.assertEqual(len(self.wait_idle(cid)["plans"]), 2)

    def test_editing_the_plan(self):
        cid = self.orchestration()
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        agent = self.sonnet()
        a, b, c = plan["subtasks"]
        r = self.client.patch(f"/api/subtasks/{a['id']}", json={"agent_id": agent, "model": "opus"})
        sub = r.json()["plan"]["subtasks"][0]
        self.assertEqual((sub["agent_id"], sub["model"]), (agent, "opus"))
        r = self.client.patch(f"/api/subtasks/{b['id']}", json={"model": "opus"})
        self.assertEqual(r.status_code, 400)  # a model needs an agent first
        r = self.client.patch(f"/api/subtasks/{b['id']}", json={"title": "B bis", "files": ["b2.txt"]})
        self.assertEqual(r.json()["plan"]["subtasks"][1]["title"], "B bis")
        self.assertEqual(r.json()["plan"]["subtasks"][1]["files"], ["b2.txt"])
        r = self.client.post(f"/api/plans/{plan['id']}/subtasks",
                             json={"title": "D", "description": "faire D", "depends_on": [c["id"]]})
        d = r.json()["plan"]["subtasks"][-1]
        self.assertEqual((d["title"], d["position"], d["depends_on"]), ("D", 4, [c["id"]]))
        r = self.client.delete(f"/api/subtasks/{c['id']}")
        self.assertEqual([s["title"] for s in r.json()["plan"]["subtasks"]], ["Partie A", "B bis", "D"])
        r = self.client.post(f"/api/subtasks/{d['id']}/dependencies", json={"depends_on_id": a["id"]})
        self.assertEqual(r.json()["plan"]["subtasks"][-1]["depends_on"], [a["id"]])
        r = self.client.delete(f"/api/subtasks/{d['id']}/dependencies/{a['id']}")
        self.assertEqual(r.json()["plan"]["subtasks"][-1]["depends_on"], [])
        r = self.client.post(f"/api/plans/{plan['id']}/remove-agent", json={"agent_id": agent})
        self.assertEqual([s["agent_id"] for s in r.json()["plan"]["subtasks"]], [None, None, None])

    def test_conductor_adapts_the_plan(self):
        cid = self.orchestration()
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        self.sonnet()
        r = self.client.post(f"/api/plans/{plan['id']}/adapt")
        self.assertEqual(r.status_code, 202)
        data = self.wait_idle(cid)
        plan = data["plans"][0]
        self.assertEqual(plan["summary"], "Plan adapté aux agents disponibles.")
        self.assertTrue(all(s["agent_id"] for s in plan["subtasks"]))

    def test_cancel(self):
        cid = self.orchestration()
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        r = self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": "autre"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("un plan est déjà ouvert", r.json()["detail"])
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/cancel").json()["plan"]["status"],
                         "stopped")
        self.send(cid, "autre")
        self.wait_idle(cid)

    def test_automatic_assignment(self):
        cid = self.new_conversation(mode="orchestration", conductor_model="opus", assignment_mode="auto")
        self.assertEqual(self.conversation(cid)["conversation"]["assignment_mode"], "auto")
        self.use_plan({"title": "Script", "description": "un script", "kind": "terminal",
                       "depends_on": [], "files": ["s.sh"]},
                      {"title": "Doc", "description": "la doc", "kind": "redaction",
                       "depends_on": [], "files": ["README.md"]})
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        self.assertEqual([(s["kind"], s["model"]) for s in plan["subtasks"]],
                         [("terminal", "opus"), ("redaction", "opus")])
        self.assertTrue(all(s["assignment_reason"] for s in plan["subtasks"]))
        # A new kind brings the AI of that kind along.
        r = self.client.patch(f"/api/subtasks/{plan['subtasks'][1]['id']}", json={"kind": "securite"})
        doc = r.json()["plan"]["subtasks"][1]
        self.assertEqual((doc["kind"], doc["model"]), ("securite", "haiku"))
        # Unless the user chose the agent by hand.
        sonnet = self.sonnet()
        self.client.patch(f"/api/subtasks/{doc['id']}", json={"agent_id": sonnet})
        r = self.client.patch(f"/api/subtasks/{doc['id']}", json={"kind": "recherche"})
        doc = r.json()["plan"]["subtasks"][1]
        self.assertEqual((doc["kind"], doc["agent_id"], doc["assignment_reason"]), ("recherche", sonnet, None))
        # "Répartir selon les benchmarks" applies the table to everything again.
        r = self.client.post(f"/api/plans/{plan['id']}/auto-assign")
        self.assertEqual(r.json()["plan"]["subtasks"][1]["model"], "fable")
        self.assertEqual(self.client.patch(f"/api/subtasks/{plan['subtasks'][1]['id']}",
                                           json={"kind": "poésie"}).status_code, 400)
        # Nothing is left without an agent: the plan can be launched at once.
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/launch").status_code, 202)
        self.assertEqual(self.wait_idle(cid)["plans"][0]["status"], "done")

    def test_switching_the_assignment_mode(self):
        cid = self.orchestration()
        self.assertEqual(self.conversation(cid)["conversation"]["assignment_mode"], "manual")
        # In manual mode, a new kind does not bring an agent along.
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        r = self.client.patch(f"/api/subtasks/{plan['subtasks'][0]['id']}", json={"kind": "ops"})
        self.assertEqual((r.json()["plan"]["subtasks"][0]["kind"], r.json()["plan"]["subtasks"][0]["agent_id"]),
                         ("ops", None))
        self.client.post(f"/api/plans/{plan['id']}/cancel")
        r = self.client.patch(f"/api/conversations/{cid}", json={"assignment_mode": "auto"})
        self.assertEqual(r.json()["conversation"]["assignment_mode"], "auto")
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][-1]
        self.assertTrue(all(s["agent_id"] for s in plan["subtasks"]))
        r = self.client.patch(f"/api/conversations/{cid}", json={"assignment_mode": "hasard"})
        self.assertEqual(r.status_code, 400)

    def test_discuss_with_the_conductor(self):
        cid = self.orchestration()
        r = self.client.post(f"/api/conversations/{cid}/messages",
                             json={"prompt": "bonjour chef", "kind": "discuss"})
        self.assertEqual(r.status_code, 202)
        data = self.wait_idle(cid)
        self.assertEqual(data["messages"][-1]["content"], "Le chef répond à : bonjour chef")
        self.assertEqual(data["plans"], [])
        # While a plan is proposed: discussing works, a second plan does not.
        self.send(cid, "fais le projet")
        self.wait_idle(cid)
        r = self.client.post(f"/api/conversations/{cid}/messages",
                             json={"prompt": "et ensuite ?", "kind": "discuss"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.wait_idle(cid)["messages"][-1]["content"],
                         "Le chef répond à : et ensuite ? (plan vu)")
        r = self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "kind": "autre"})
        self.assertEqual(r.status_code, 400)

    def test_subtask_permission_names_its_subtask(self):
        self.use_plan({"title": "Écriture", "description": "écris la note", "kind": "code",
                       "depends_on": [], "files": ["note.txt"]}, max_parallel=1)
        cid = self.orchestration()
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        self.client.post(f"/api/plans/{plan['id']}/assign-all", json={"agent_id": self.sonnet()})
        self.client.post(f"/api/plans/{plan['id']}/launch")
        pending = self.wait_pending(cid)
        self.assertEqual((pending["tool_name"], pending["subtask_title"]), ("Write", "Écriture"))
        self.client.post(f"/api/permissions/{pending['id']}", json={"allow": True})
        self.assertEqual(self.wait_idle(cid)["plans"][0]["status"], "done")
        self.assertEqual((self.workdir / "note.txt").read_text(encoding="utf-8"), "contenu")

    def test_stop_a_running_plan(self):
        self.use_plan({"title": "Long", "description": "attends longtemps", "kind": "code",
                       "depends_on": [], "files": ["l.txt"]}, max_parallel=1)
        cid = self.orchestration()
        self.send(cid)
        plan = self.wait_idle(cid)["plans"][0]
        self.client.post(f"/api/plans/{plan['id']}/assign-all", json={"agent_id": self.sonnet()})
        self.client.post(f"/api/plans/{plan['id']}/launch")
        wait_until(lambda: self.plan_of(cid)["subtasks"][0]["status"] == "running")
        # Editing a running plan is refused.
        self.assertEqual(self.client.delete(f"/api/subtasks/{plan['subtasks'][0]['id']}").status_code, 409)
        self.client.post(f"/api/conversations/{cid}/stop")
        data = self.wait_idle(cid)
        self.assertEqual((data["plans"][0]["status"], data["plans"][0]["subtasks"][0]["status"]),
                         ("stopped", "stopped"))


class RecoveryTests(TempDirMixin, unittest.TestCase):
    def test_requests_left_running_by_a_killed_server_are_closed(self):
        path = str(self.make_tempdir() / "hub.db")
        conn = db.connect(path)
        agent = store.create_agent(conn, "Claude", "claude_code", "opus")
        cid = store.create_conversation(conn, "c", agent_id=agent, workdir=str(self.make_tempdir()))
        mid = store.start_request(conn, cid, agent)
        store.mark_message_running(conn, mid)
        ocid = store.create_conversation(conn, "o", mode="orchestration")
        uid = store.add_user_message(conn, ocid, "demande")
        pid = store.create_plan(conn, ocid, uid, [{"description": "a", "agent_id": agent},
                                                  {"description": "b", "agent_id": agent}])
        store.validate_plan(conn, pid)
        store.start_plan(conn, pid)
        store.start_subtask(conn, store.get_plan(conn, pid)["subtasks"][0]["id"])
        conn.close()

        app = create_app(path, command=FAKE_COMMAND)
        self.assertEqual(app.state.jobs.recovered, 2)  # one reply, one plan
        conn = db.connect(path)
        try:
            message = store.get_message(conn, mid)
            self.assertEqual(message["status"], "stopped")
            self.assertIn("serveur s'est arrêté", message["error"])
            self.assertIsNone(store.active_request(conn, cid))
            plan = store.get_plan(conn, pid)
            self.assertEqual(plan["status"], "stopped")
            self.assertEqual([s["status"] for s in plan["subtasks"]], ["stopped", "stopped"])
            self.assertIsNone(store.open_plan(conn, ocid))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
