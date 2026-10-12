"""Version 0.3: a server created from its page, optional passwords, roles (invité, membre, admin)."""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from aihub import auth, db
from aihub.web.app import create_app

from .helpers import FAKE_COMMAND, TempDirMixin, fake_env
from .test_auth import Clock
from .test_sharing import H, SharingTestCase

HERE = ("127.0.0.1", 50000)


class SetupTest(TempDirMixin, unittest.TestCase):
    """A new server: created once, from its own computer, with optional passwords."""

    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.app = create_app(self.db_path, command=FAKE_COMMAND, env=fake_env("text", tmp / "log.json"),
                              auth_policy=auth.Policy(), clock=Clock(), run_scheduler=False)

    def client(self, address=HERE):
        client = TestClient(self.app, client=address)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_created_here_once_then_names_and_the_server_password(self):
        here = self.client()
        session = here.get("/api/auth/session").json()
        self.assertEqual((session["setup"], session["setup_here"], session["logged_in"]), (True, True, False))
        self.assertEqual(here.get("/api/conversations", headers=H).status_code, 401)
        r = here.post("/api/setup", json={"name": "Wal", "server_password": "le mot du serveur"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        session = here.get("/api/auth/session").json()
        self.assertEqual((session["user"]["name"], session["user"]["is_owner"], session["server_password"]),
                         ("Wal", True, True))
        self.assertIn(here.post("/api/setup", json={"name": "Autre"}, headers=H).status_code, (401, 400))  # once
        # Another device: the server's password, then the name (no password for this account).
        phone = self.client(("100.64.0.2", 50001))
        r = phone.post("/api/auth/login", json={"name": "Wal", "server_password": "pas le bon"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("serveur", r.json()["detail"])
        self.assertEqual(phone.post("/api/auth/login", json={"name": "Wal", "server_password": "le mot du serveur"})
                         .status_code, 200)

    def test_never_from_elsewhere(self):
        for address, headers in ((("100.64.0.2", 50001), H), (HERE, {**H, "X-Forwarded-For": "100.64.0.9"})):
            with self.subTest(address=address, headers=headers):
                c = self.client(address)
                self.assertFalse(c.get("/api/auth/session", headers=headers).json()["setup_here"])
                r = c.post("/api/setup", json={"name": "Intrus"}, headers=headers)
                self.assertEqual(r.status_code, 403)
        r = self.client().post("/api/setup", json={"name": "Wal"})  # no X-AIHub: another site's page
        self.assertEqual(r.status_code, 403)
        conn = db.connect(self.db_path)
        self.assertFalse(auth.password_set(conn))
        conn.close()


class RolesTestCase(SharingTestCase):
    def person(self, name, role="invite", password="", grants=()):
        r = self.owner.post("/api/users", json={"name": name, "password": password, "role": role,
                                                "grants": list(grants)}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        client = self.client(("127.0.0.1", 50100 + r.json()["user"]["id"]))
        self.assertEqual(client.post("/api/auth/login", json={"name": name, "password": password}).status_code, 200)
        return client, r.json()["user"]["id"]


class AccountTest(RolesTestCase):
    def test_an_account_without_a_password_and_its_accesses(self):
        cid = self.conversation("Site")
        sam, uid = self.person("Sam", grants=[{"conversation_id": cid, "role": "read"}])
        self.assertEqual([c["id"] for c in sam.get("/api/conversations", headers=H).json()["conversations"]], [cid])
        self.assertEqual(sam.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]["my_role"], "read")
        users = self.owner.get("/api/users", headers=H).json()["users"]
        sam_row = next(u for u in users if u["id"] == uid)
        self.assertEqual((sam_row["has_password"], sam_row["role"], [g["title"] for g in sam_row["grants"]]),
                         (False, "invite", ["Site"]))
        # A wrong password is refused even on an account without one.
        other = self.client(("127.0.0.1", 50300))
        self.assertEqual(other.post("/api/auth/login", json={"name": "Sam", "password": "quelque chose"}).status_code, 401)
        # The person sets one, then removes it again.
        self.assertEqual(sam.post("/api/me/password", json={"current": "", "new": "un mot de passe"}, headers=H)
                         .status_code, 200)
        self.assertEqual(other.post("/api/auth/login", json={"name": "Sam"}).status_code, 401)
        self.assertEqual(sam.post("/api/me/password", json={"current": "un mot de passe", "new": ""}, headers=H)
                         .status_code, 200)
        self.assertEqual(other.post("/api/auth/login", json={"name": "Sam"}).status_code, 200)

    def test_an_invitation_carries_the_role_and_the_accesses(self):
        cid = self.conversation("Boutique")
        link = self.owner.post("/api/invitations", json={"role": "membre",
                                                          "grants": [{"conversation_id": cid, "role": "write"}]},
                               headers=H).json()["link"]
        new = self.client(("127.0.0.1", 50400))
        r = new.post("/api/invitations/accept", json={"invitation": link.split("invitation=")[1], "name": "Léa"})
        self.assertEqual(r.status_code, 200, r.text)  # no password: optional
        self.assertEqual(new.get("/api/auth/session").json()["user"]["role"], "membre")
        self.assertEqual(new.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]["my_role"], "write")

    def test_the_server_password_is_the_owners_to_set(self):
        sam, _ = self.person("Sam")
        self.assertEqual(sam.post("/api/server-password", json={"password": "mot du serveur"}, headers=H).status_code, 403)
        r = self.owner.post("/api/server-password", json={"password": "mot du serveur"}, headers=H)
        self.assertEqual(r.json(), {"server_password": True})
        other = self.client(("127.0.0.1", 50500))
        self.assertEqual(other.post("/api/auth/login", json={"name": "Sam"}).status_code, 401)
        self.assertEqual(other.post("/api/auth/login", json={"name": "Sam", "server_password": "mot du serveur"})
                         .status_code, 200)
        self.assertEqual(self.owner.post("/api/server-password", json={"password": ""}, headers=H).json(),
                         {"server_password": False})


class MemberTest(RolesTestCase):
    def test_a_member_makes_conversations_of_their_own(self):
        lea, uid = self.person("Léa", role="membre")
        r = lea.post("/api/conversations", json={"title": "Mes notes", "workdir": "C:\\Windows"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        conv = r.json()["conversation"]
        space = Path(self.db_path).resolve().with_name("hub-espaces") / str(uid)
        self.assertEqual(Path(conv["workdir"]).parent, space)  # never the folder asked for
        self.assertTrue(Path(conv["workdir"]).is_dir())
        self.assertEqual(lea.get(f"/api/conversations/{conv['id']}", headers=H).json()["conversation"]["my_role"], "owner")
        self.assertEqual([c["id"] for c in lea.get("/api/conversations", headers=H).json()["conversations"]],
                         [conv["id"]])  # in her list too
        # The owner sees it (they see everything); a guest does not.
        self.assertIn(conv["id"], [c["id"] for c in self.owner.get("/api/conversations", headers=H).json()["conversations"]])
        guest, gid = self.guest("Lou")
        self.assertEqual(guest.get(f"/api/conversations/{conv['id']}", headers=H).status_code, 404)
        # What stays out of a member's reach.
        for body, said in (({"tools": ["coffre"]}, "recherche web"), ({"obsidian": True}, "Obsidian")):
            with self.subTest(body=body):
                r = lea.patch(f"/api/conversations/{conv['id']}", json=body, headers=H)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn(said, r.json()["detail"])
        self.assertEqual(lea.patch(f"/api/conversations/{conv['id']}", json={"tools": ["web"]}, headers=H).status_code, 200)
        r = lea.post("/api/conversations", json={"title": "x", "permission_mode": "bypass", "confirm_bypass": True},
                     headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        isolated = lea.get(f"/api/conversations/{r.json()['conversation']['id']}/isolation", headers=H).json()
        self.assertTrue(isolated['policy']['isolated'])  # bypass is confined to the VM
        r = lea.post(f"/api/conversations/{conv['id']}/autonomous",
                     json={"goal": "x", "check_command": "dir", "confirm_check": True}, headers=H)
        self.assertEqual(r.status_code, 403)
        # A member shares what is theirs, and nothing else.
        self.assertEqual(lea.post("/api/shares", json={"user_id": gid, "role": "read", "conversation_id": conv["id"]},
                                  headers=H).status_code, 200)
        self.assertEqual(guest.get(f"/api/conversations/{conv['id']}", headers=H).status_code, 200)
        owners = self.conversation("Au propriétaire")
        self.share(uid, "write", conversation_id=owners)
        r = lea.post("/api/shares", json={"user_id": gid, "role": "read", "conversation_id": owners}, headers=H)
        self.assertEqual(r.status_code, 403)

    def test_a_members_project(self):
        lea, uid = self.person("Léa", role="membre")
        pid = lea.post("/api/projects", json={"name": "Perso", "workdir": "C:\\Windows"}, headers=H).json()["project"]["id"]
        self.assertIsNone(lea.get(f"/api/projects/{pid}", headers=H).json()["project"]["workdir"])
        r = lea.post("/api/conversations", json={"title": "Dans mon projet", "project_id": pid}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        owners = self.owner.post("/api/projects", json={"name": "Au propriétaire"}, headers=H).json()["project"]["id"]
        r = lea.post("/api/conversations", json={"title": "x", "project_id": owners}, headers=H)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(lea.patch(f"/api/projects/{owners}", json={"name": "pris"}, headers=H).status_code, 404)
        self.assertEqual(lea.delete(f"/api/projects/{pid}", headers=H).status_code, 200)


class AdminTest(RolesTestCase):
    def test_an_admin_sees_and_manages_but_not_the_settings(self):
        cid = self.conversation("Secrète")
        ana, _ = self.person("Ana", role="admin", password="mot de passe d'Ana")
        self.assertEqual(ana.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]["my_role"], "owner")
        self.assertEqual(ana.get("/api/users", headers=H).status_code, 200)
        sam = ana.post("/api/users", json={"name": "Sam"}, headers=H)
        self.assertEqual(sam.status_code, 201)
        self.assertEqual(ana.patch(f"/api/users/{sam.json()['user']['id']}", json={"role": "membre"}, headers=H)
                         .json()["user"]["role"], "membre")
        self.assertEqual(ana.get("/api/config", headers=H).status_code, 403)
        self.assertEqual(ana.post("/api/server-password", json={"password": "x" * 9}, headers=H).status_code, 403)
        owner_id = next(u["id"] for u in ana.get("/api/users", headers=H).json()["users"] if u["is_owner"])
        self.assertEqual(ana.patch(f"/api/users/{owner_id}", json={"role": "invite"}, headers=H).status_code, 401)
        self.assertEqual(ana.delete(f"/api/users/{owner_id}", headers=H).status_code, 401)

    def test_nothing_is_shared_with_an_admin_nor_someone_elses_reading_used(self):
        import json
        cid = self.conversation("Site")
        _, ana = self.person("Ana", role="admin")
        r = self.owner.post("/api/shares", json={"user_id": ana, "role": "read", "conversation_id": cid}, headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("voient déjà tout", r.json()["detail"])
        lea, _ = self.person("Léa", role="membre")
        export = json.dumps([{"name": "x", "created_at": "2026-09-01T10:00:00Z", "chat_messages": [
            {"sender": "human", "text": "bonjour", "created_at": "2026-09-01T10:00:01Z"}]}]).encode()
        reading = self.owner.post("/api/import/file", content=export, headers=H).json()
        r = lea.post("/api/import", json={"reading": reading["reading"], "keys": [reading["conversations"][0]["key"]]},
                     headers=H)
        self.assertIn("expirée", r.json()["detail"])  # the owner's reading is not hers

    def test_a_guest_creates_nothing(self):
        lou, _ = self.person("Lou")
        self.assertEqual(lou.post("/api/conversations", json={"title": "x"}, headers=H).status_code, 403)
        self.assertEqual(lou.post("/api/projects", json={"name": "x"}, headers=H).status_code, 403)
        self.assertEqual(lou.post("/api/import/file", content=b"{}", headers=H).status_code, 403)


class AlertTest(TempDirMixin, unittest.TestCase):
    def test_the_admins_get_everything_and_a_member_their_own(self):
        from aihub import config, notify, store
        from .test_notify import ENDPOINT, Device, Post
        path = str(self.make_tempdir() / "hub.db")
        conn = db.connect(path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO users (id, name, password_hash, is_owner, created_at) VALUES (1, 'P', '', 1, 'n')")
        for uid, role in ((2, "admin"), (3, "membre"), (4, "invite")):
            conn.execute("INSERT INTO users (id, name, password_hash, role, created_at) VALUES (?, ?, '', ?, 'n')",
                         (uid, f"U{uid}", role))
        for uid in (1, 2, 3, 4):
            conn.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at, user_id) "
                         "VALUES (?, 'x', 'x', '9999', ?)", (f"h{uid}", uid))
            sid = conn.execute("SELECT id FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0]
            notify.subscribe(conn, Device(f"{ENDPOINT}-{uid}").subscription, ["done"], session_id=sid)
        agent = store.create_agent(conn, "Claude", "claude_code", "haiku")
        mine = store.create_conversation(conn, "Celle du membre", agent_id=agent, workdir=str(self.make_tempdir()))
        conn.execute("UPDATE conversations SET owner_id = 3 WHERE id = ?", (mine,))
        post = Post()
        notify.Notifier(path, lambda: config.Settings(), post=post, sync=True).notify("done", mine)
        self.assertEqual(sorted(s["url"] for s in post.sent), [f"{ENDPOINT}-{n}" for n in (1, 2, 3)])


if __name__ == "__main__":
    unittest.main()
