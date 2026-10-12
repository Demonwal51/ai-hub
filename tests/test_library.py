"""Organisation is personal; search scopes before ranking; templates never run without a send."""
import json
import sqlite3
import unittest

from aihub import db, store
from .test_sharing import H, SharingTestCase


class LibraryTest(SharingTestCase):
    def test_archives_and_pins_are_personal_even_in_read_only_sharing(self):
        cid = self.conversation("Partagée")
        guest, uid = self.guest("Bibliothèque")
        self.share(uid, "read", conversation_id=cid)
        r = guest.patch(f"/api/conversations/{cid}/preferences", json={"pinned": True, "archived": True}, headers=H)
        self.assertEqual(r.status_code, 200)
        theirs = guest.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]
        mine = self.owner.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]
        self.assertTrue(theirs["archived"] and theirs["pinned"])
        self.assertFalse(mine["archived"] or mine["pinned"])
        guest.patch(f"/api/conversations/{cid}/preferences", json={"archived": False}, headers=H)
        self.assertTrue(guest.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]["pinned"])
        hidden = self.conversation("Non partagée")
        self.assertEqual(guest.patch(f"/api/conversations/{hidden}/preferences", json={"pinned": True}, headers=H).status_code, 404)

    def test_search_filters_before_limit_and_respects_access(self):
        guest, uid = self.guest("Recherche")
        visible = self.conversation("Visible")
        hidden = self.conversation("Privée")
        self.share(uid, "read", conversation_id=visible)
        conn = db.connect(self.db_path)
        for _ in range(110):
            store.add_user_message(conn, hidden, "sapin")
        store.add_user_message(conn, visible, "sapin")
        conn.close()
        rows = guest.get("/api/search?q=sapin&limit=1", headers=H).json()["results"]
        self.assertEqual([r["conversation_id"] for r in rows], [visible])
        guest.patch(f"/api/conversations/{visible}/preferences", json={"archived": True}, headers=H)
        self.assertEqual(guest.get("/api/search?q=sapin&archived=false", headers=H).json()["results"], [])
        self.assertEqual(len(guest.get("/api/search?q=sapin&archived=true", headers=H).json()["results"]), 1)
        self.assertEqual(guest.get("/api/search?q=sapin&model=opus", headers=H).json()["results"], [])
        self.assertEqual(guest.get("/api/search?q=sapin&after=2099-01-01", headers=H).json()["results"], [])
        self.assertEqual(guest.get("/api/search?q=sapin&after=hier", headers=H).status_code, 400)
        self.assertEqual(guest.get("/api/search?q=sapin&after=2026-02-01&before=2026-01-01", headers=H).status_code, 400)

    def test_template_is_a_draft_with_variables_and_no_ai_call(self):
        body = {"title": "Correction", "workdir": str(self.workdir), "template_id": "builtin:bug",
                "variables": {"probleme": "le bouton ne répond pas"}}
        r = self.owner.post("/api/conversations", json=body, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("le bouton ne répond pas", r.json()["prompt"])
        data = self.owner.get(f"/api/conversations/{r.json()['conversation']['id']}", headers=H).json()
        self.assertEqual(data["messages"], [])
        self.assertIn("vérifications", data["conversation"]["instructions"])
        self.assertFalse(data["running"])
        body["variables"] = {}
        self.assertEqual(self.owner.post("/api/conversations", json=body, headers=H).status_code, 400)

    def test_personal_template_is_private_and_never_inherits_bypass(self):
        cid = self.conversation("Modèle", permission_mode="bypass", confirm_bypass=True)
        response = self.owner.post("/api/templates", json={"conversation_id": cid, "title": "Mon travail",
                                                           "prompt": "Lis {{document}}"}, headers=H)
        self.assertEqual(response.status_code, 201, response.text)
        item = next(t for t in response.json()["templates"] if not t["builtin"])
        self.assertEqual(item["settings"]["permission_mode"], "manual")
        guest, uid = self.guest("Modèles")
        self.share(uid, "write", conversation_id=cid)
        self.assertFalse(any(not t["builtin"] for t in guest.get("/api/templates", headers=H).json()["templates"]))
        self.assertEqual(guest.delete(f"/api/templates/{item['id']}", headers=H).status_code, 404)
        self.assertEqual(self.owner.delete(f"/api/templates/{item['id']}", headers=H).status_code, 200)
        self.assertEqual(self.owner.delete("/api/templates/builtin:bug", headers=H).status_code, 404)

    def test_existing_v19_base_keeps_its_conversations(self):
        path = self.make_tempdir() / "old.db"
        old = sqlite3.connect(path)
        for version in range(1, 20):
            for statement in getattr(db, f"_SCHEMA_V{version}"):
                old.execute(statement)
        old.execute("PRAGMA user_version = 19")
        now = db.utc_now()
        old.execute("INSERT INTO conversations (title,mode,permission_mode,assignment_mode,created_at,updated_at) "
                    "VALUES ('Ancienne','single','manual','manual',?,?)", (now, now))
        old.commit()
        old.close()
        conn = db.connect(str(path))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT title FROM conversations").fetchone()[0], "Ancienne")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversation_preferences").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
