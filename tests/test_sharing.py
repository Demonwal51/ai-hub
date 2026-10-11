"""Version 0.2: several people on one server, conversations and projects shared."""

import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from aihub import auth, db, store
from aihub.web.app import create_app

from .helpers import FAKE_COMMAND, TempDirMixin, fake_env
from .test_auth import Clock, new_password

H = {"X-AIHub": "1"}


def wait_idle(client, cid, timeout=20):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/conversations/{cid}", headers=H).json()
        if not data["running"]:
            return data
        time.sleep(0.05)
    raise AssertionError("toujours en cours")


class SharingTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.workdir = self.make_tempdir()
        self.password = new_password()
        conn = db.connect(self.db_path)
        auth.set_password(conn, self.password)
        conn.close()
        self.env = fake_env("text", tmp / "log.json")
        self.env["FAKE_CLAUDE_LOGGED_IN"] = "1"
        self.app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, auth_policy=auth.Policy(),
                              clock=Clock(), sse_ping_s=0.1, run_scheduler=False)
        self.owner = self.client()
        self.assertEqual(self.owner.post("/api/auth/login", json={"password": self.password}).status_code, 200)

    def client(self, address=("127.0.0.1", 50000)):
        client = TestClient(self.app, client=address)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def guest(self, name="Lou"):
        link = self.owner.post("/api/invitations", json={"note": name}, headers=H).json()["link"]
        token = link.split("invitation=", 1)[1]
        guest = self.client(("127.0.0.1", 50001))
        r = guest.post("/api/invitations/accept", json={"invitation": token, "name": name, "password": "un bon mot de passe"})
        self.assertEqual(r.status_code, 200, r.text)
        users = self.owner.get("/api/users", headers=H).json()["users"]
        return guest, next(u["id"] for u in users if u["name"] == name)

    def conversation(self, title="Site", **body):
        r = self.owner.post("/api/conversations", json={"title": title, "workdir": str(self.workdir), **body}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["conversation"]["id"]

    def share(self, uid, role, **target):
        r = self.owner.post("/api/shares", json={"user_id": uid, "role": role, **target}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["shared_with"]


class InvitationTest(SharingTestCase):
    def test_a_guest_comes_by_invitation(self):
        guest, uid = self.guest()
        session = guest.get("/api/auth/session").json()
        self.assertEqual(session["user"], {"name": "Lou", "is_owner": False, "role": "invite"})
        self.assertEqual(guest.get("/api/conversations", headers=H).json()["conversations"], [])
        # Logged in with a name afterwards, from another device.
        other = self.client(("127.0.0.1", 50002))
        self.assertEqual(other.post("/api/auth/login", json={"name": "lou", "password": "un bon mot de passe"}).status_code,
                         200)  # the name is not case-sensitive

    def test_a_link_works_once_and_not_after_it_expires(self):
        link = self.owner.post("/api/invitations", json={}, headers=H).json()["link"]
        token = link.split("invitation=", 1)[1]
        first = self.client(("127.0.0.1", 50001))
        self.assertEqual(first.post("/api/invitations/accept",
                                    json={"invitation": token, "name": "A", "password": "un bon mot de passe"}).status_code, 200)
        again = self.client(("127.0.0.1", 50003))
        r = again.post("/api/invitations/accept", json={"invitation": token, "name": "B", "password": "un bon mot de passe"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("déjà utilisé", r.json()["detail"])
        old = self.owner.post("/api/invitations", json={}, headers=H).json()["link"].split("invitation=", 1)[1]
        self.app.state.jobs  # noqa: B018 (the clock is the app's)
        with mock.patch.object(auth.Auth, "now", lambda self: auth.datetime.now(auth.timezone.utc) + auth.timedelta(days=8)):
            r = again.post("/api/invitations/accept", json={"invitation": old, "name": "C", "password": "un bon mot de passe"})
        self.assertEqual(r.status_code, 401)

    def test_the_owner_makes_an_account_without_a_link(self):
        r = self.owner.post("/api/users", json={"name": "Sam", "password": "un bon mot de passe"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["user"]["name"], r.json()["user"]["is_owner"]), ("Sam", False))
        sam = self.client(("127.0.0.1", 50005))
        self.assertEqual(sam.post("/api/auth/login", json={"name": "Sam", "password": "un bon mot de passe"}).status_code, 200)
        self.assertEqual(sam.get("/api/auth/session").json()["user"], {"name": "Sam", "is_owner": False, "role": "invite"})
        self.assertEqual(sam.post("/api/users", json={"name": "X", "password": "un bon mot de passe"}, headers=H).status_code,
                         403)  # the owner's alone
        again = self.owner.post("/api/users", json={"name": "SAM", "password": "un bon mot de passe"}, headers=H)
        self.assertIn("déjà pris", again.json()["detail"])
        short = self.owner.post("/api/users", json={"name": "Lea", "password": "court"}, headers=H)
        self.assertIn("au moins", short.json()["detail"])

    def test_renamed_people_log_in_with_their_new_name(self):
        guest, uid = self.guest("Lou")
        owner_id = next(u["id"] for u in self.owner.get("/api/users", headers=H).json()["users"] if u["is_owner"])
        r = self.owner.patch(f"/api/users/{owner_id}", json={"name": "Wal"}, headers=H)
        self.assertEqual(r.json()["user"]["name"], "Wal")
        self.assertEqual(self.owner.get("/api/auth/session").json()["user"]["name"], "Wal")  # still logged in
        other = self.client(("127.0.0.1", 50006))
        for name in ("Wal", ""):  # the new name, or no name at all
            self.assertEqual(other.post("/api/auth/login", json={"name": name, "password": self.password}).status_code, 200)
        self.assertEqual(other.post("/api/auth/login", json={"name": "Propriétaire", "password": self.password}).status_code,
                         401)
        taken = self.owner.patch(f"/api/users/{uid}", json={"name": "wal"}, headers=H)
        self.assertIn("déjà pris", taken.json()["detail"])
        self.assertEqual(self.owner.patch(f"/api/users/{uid}", json={"name": "Lou"}, headers=H).status_code, 200)  # same name
        self.assertEqual(guest.patch(f"/api/users/{uid}", json={"name": "Reine"}, headers=H).status_code, 403)

    def test_names_are_unique_and_unknown_names_say_nothing(self):
        self.guest("Lou")
        link = self.owner.post("/api/invitations", json={}, headers=H).json()["link"].split("invitation=", 1)[1]
        other = self.client(("127.0.0.1", 50004))
        r = other.post("/api/invitations/accept", json={"invitation": link, "name": "LOU", "password": "un bon mot de passe"})
        self.assertIn("déjà pris", r.json()["detail"])
        r = other.post("/api/auth/login", json={"name": "Personne", "password": "un bon mot de passe"})
        r2 = other.post("/api/auth/login", json={"name": "Lou", "password": "pas le bon mot de passe"})
        self.assertEqual((r.status_code, r2.status_code), (401, 401))
        self.assertEqual(r.json()["detail"].split("(")[0], r2.json()["detail"].split("(")[0])  # the same refusal

    def test_a_disabled_person_is_out_at_once(self):
        guest, uid = self.guest()
        self.assertEqual(self.owner.patch(f"/api/users/{uid}", json={"disabled": True}, headers=H).status_code, 200)
        self.assertEqual(guest.get("/api/conversations", headers=H).status_code, 401)
        r = guest.post("/api/auth/login", json={"name": "Lou", "password": "un bon mot de passe"})
        self.assertEqual(r.status_code, 401)

    def test_only_the_owner_manages_people(self):
        guest, uid = self.guest()
        for method, path, body in (("GET", "/api/users", None), ("POST", "/api/invitations", {}),
                                   ("DELETE", f"/api/users/{uid}", None), ("GET", "/api/config", None),
                                   ("POST", "/api/shares", {"user_id": uid, "role": "write", "conversation_id": 1}),
                                   ("POST", "/api/conversations", {"title": "x", "workdir": str(self.workdir)}),
                                   ("POST", "/api/projects", {"name": "p"}), ("GET", "/api/schedules", None)):
            with self.subTest(path=path):
                r = guest.request(method, path, json=body, headers=H)
                # Sharing a conversation they do not see: « introuvable », nothing said about it.
                self.assertEqual(r.status_code, 404 if path == "/api/shares" else 403, r.text)

    def test_each_one_sees_only_their_own_devices(self):
        guest, uid = self.guest()
        mine = self.owner.get("/api/sessions", headers=H).json()["sessions"]
        theirs = guest.get("/api/sessions", headers=H).json()["sessions"]
        self.assertEqual((len(mine), len(theirs)), (1, 1))
        self.assertEqual(guest.delete(f"/api/sessions/{mine[0]['id']}", headers=H).status_code, 404)
        self.assertEqual(self.owner.get("/api/sessions", headers=H).status_code, 200)  # still logged in


class ShareTest(SharingTestCase):
    def test_read_then_write(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.assertEqual(guest.get(f"/api/conversations/{cid}", headers=H).status_code, 404)  # not shared: nothing
        self.assertEqual(self.share(uid, "read", conversation_id=cid)[0]["role"], "read")
        data = guest.get(f"/api/conversations/{cid}", headers=H).json()
        self.assertEqual((data["conversation"]["my_role"], data["shared_with"]), ("read", None))
        r = guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        self.assertEqual(r.status_code, 403)
        self.assertIn("lecture seule", r.json()["detail"])
        self.share(uid, "write", conversation_id=cid)
        r = guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "salut"}, headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        data = wait_idle(guest, cid)
        self.assertEqual([(m["role"], m["author"]) for m in data["messages"]], [("user", "Lou"), ("assistant", None)])

    def test_what_stays_the_owners(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, "write", conversation_id=cid)
        for method, path, body in (("PATCH", f"/api/conversations/{cid}", {"permission_mode": "bypass"}),
                                   ("DELETE", f"/api/conversations/{cid}", None),
                                   ("POST", f"/api/conversations/{cid}/autonomous",
                                    {"goal": "x", "check_command": "dir", "confirm_check": True})):
            with self.subTest(path=path):
                self.assertEqual(guest.request(method, path, json=body, headers=H).status_code, 403)

    def test_a_project_opens_all_its_conversations(self):
        guest, uid = self.guest()
        pid = self.owner.post("/api/projects", json={"name": "Boutique"}, headers=H).json()["project"]["id"]
        a = self.conversation("A", project_id=pid)
        b = self.conversation("B", project_id=pid)
        alone = self.conversation("Hors projet")
        self.share(uid, "read", project_id=pid)
        self.share(uid, "write", conversation_id=b)  # the higher role wins
        seen = {c["id"]: c["my_role"] for c in guest.get("/api/conversations", headers=H).json()["conversations"]}
        self.assertEqual(seen, {a: "read", b: "write"})
        self.assertNotIn(alone, seen)
        self.assertEqual([p["id"] for p in guest.get("/api/projects", headers=H).json()["projects"]], [pid])

    def test_unshared_is_as_if_it_did_not_exist(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "secret de la boutique"}, headers=H)
        wait_idle(self.owner, cid)
        for method, path in (("GET", f"/api/conversations/{cid}"), ("GET", f"/api/conversations/{cid}/events"),
                             ("POST", f"/api/conversations/{cid}/stop"), ("POST", f"/api/conversations/{cid}/messages")):
            with self.subTest(path=path):
                r = guest.request(method, path, json={"prompt": "x"}, headers=H)
                self.assertEqual(r.status_code, 404)
        self.assertEqual(guest.get("/api/search?q=secret", headers=H).json()["results"], [])
        self.assertTrue(self.owner.get("/api/search?q=secret", headers=H).json()["results"])

    def test_a_guest_with_one_conversation_cannot_read_the_others_through_the_ai(self):
        from .helpers import read_log
        guest, uid = self.guest()
        pid = self.owner.post("/api/projects", json={"name": "Boutique"}, headers=H).json()["project"]["id"]
        secret = self.conversation("Budget", project_id=pid)
        self.owner.post(f"/api/conversations/{secret}/messages", json={"prompt": "le budget du logo est 900 euros"},
                        headers=H)
        wait_idle(self.owner, secret)
        mine = self.conversation("Logo", project_id=pid)
        self.share(uid, "write", conversation_id=mine)  # this one only, not the project
        log = self.env["FAKE_CLAUDE_LOG"]
        guest.post(f"/api/conversations/{mine}/messages", json={"prompt": "quel est le budget du logo ?"}, headers=H)
        wait_idle(guest, mine)
        sent = read_log(log)
        self.assertNotIn("--mcp-config", sent["args"])  # no project server for the agent
        self.assertNotIn("900 euros", sent["user_message"]["message"]["content"])  # nothing joined by the hub
        self.assertNotIn("--append-system-prompt", sent["args"])
        # The owner, asking the same in the same conversation, gets the project.
        self.owner.post(f"/api/conversations/{mine}/messages", json={"prompt": "quel est le budget du logo ?"}, headers=H)
        wait_idle(self.owner, mine)
        sent = read_log(log)
        self.assertIn("--mcp-config", sent["args"])
        # With the whole project shared, the guest gets it too.
        self.share(uid, "write", project_id=pid)
        guest.post(f"/api/conversations/{mine}/messages", json={"prompt": "et le budget ?"}, headers=H)
        wait_idle(guest, mine)
        self.assertIn("--mcp-config", read_log(log)["args"])

    def test_unshare(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, "read", conversation_id=cid)
        r = self.owner.post("/api/shares/remove", json={"user_id": uid, "conversation_id": cid}, headers=H)
        self.assertEqual(r.json()["shared_with"], [])
        self.assertEqual(guest.get(f"/api/conversations/{cid}", headers=H).status_code, 404)


class PromptsTest(SharingTestCase):
    def test_a_guest_never_answers_anothers_prompt(self):
        self.env["FAKE_CLAUDE_SCENARIO"] = "permission"
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, "write", conversation_id=cid)
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "écris"}, headers=H)
        import time
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pending = guest.get(f"/api/conversations/{cid}", headers=H).json()["pending"]
            if pending:
                break
            time.sleep(0.05)
        self.assertFalse(pending[0]["can_answer"])  # the owner's request
        self.assertEqual(guest.post(f"/api/permissions/{pending[0]['id']}", json={"allow": True}, headers=H).status_code,
                         404)
        owner_view = self.owner.get(f"/api/conversations/{cid}", headers=H).json()["pending"]
        self.assertTrue(owner_view[0]["can_answer"])
        self.owner.post(f"/api/permissions/{pending[0]['id']}", json={"allow": False}, headers=H)
        wait_idle(self.owner, cid)


class MigrationTest(TempDirMixin, unittest.TestCase):
    def test_the_single_password_becomes_the_owners(self):
        path = str(self.make_tempdir() / "hub.db")
        with mock.patch.object(db, "SCHEMA_VERSION", 14), mock.patch.object(db, "_SCHEMA_V15", []), \
                mock.patch.object(db, "_SCHEMA_V16", []), mock.patch.object(db, "_SCHEMA_V17", []), \
                mock.patch.object(db, "_SCHEMA_V18", []), mock.patch.object(db, "_SCHEMA_V19", []):
            db.connect(path).close()  # a base as version 14 left it
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version = 14")
        raw.execute("INSERT INTO auth_password (id, password_hash, changed_at) VALUES (1, ?, 'x')",
                    (auth.hash_password("un bon mot de passe"),))
        raw.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at) VALUES ('h', 'x', 'x', '9999')")
        raw.commit()
        raw.close()
        conn = db.connect(path)
        try:
            owner = auth.owner(conn)
            self.assertEqual(owner["name"], auth.OWNER_NAME)
            self.assertEqual(auth.Auth().verify(conn, "un bon mot de passe", "c")["id"], owner["id"])
            self.assertEqual(conn.execute("SELECT user_id FROM sessions").fetchone()[0], owner["id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_password").fetchone()[0], 0)  # no copy left behind
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
