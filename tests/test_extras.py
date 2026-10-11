"""Favorite requests, the export of a conversation, and who used how much."""

import json
import unittest

from aihub import db, importer, store, usage

from .test_sharing import H, SharingTestCase, wait_idle


class FavoritesTest(SharingTestCase):
    def test_each_one_keeps_their_own(self):
        r = self.owner.post("/api/favorites", json={"title": "Point du jour", "text": "Fais le point du jour"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        mine = r.json()["favorites"]
        self.assertEqual([(f["title"], f["text"]) for f in mine], [("Point du jour", "Fais le point du jour")])
        guest, _ = self.guest("Lou")
        self.assertEqual(guest.get("/api/favorites", headers=H).json()["favorites"], [])  # not someone else's
        self.assertEqual(guest.delete(f"/api/favorites/{mine[0]['id']}", headers=H).status_code, 404)
        no_title = guest.post("/api/favorites", json={"text": "  Relis   ce code\net corrige  "}, headers=H).json()
        self.assertEqual(no_title["favorites"][0]["title"], "Relis ce code et corrige")
        self.assertEqual(self.owner.post("/api/favorites", json={"text": "   "}, headers=H).status_code, 400)
        self.assertEqual(self.owner.delete(f"/api/favorites/{mine[0]['id']}", headers=H).json()["favorites"], [])

    def test_a_hundred_at_most(self):
        conn = db.connect(self.db_path)
        for n in range(store.FAVORITES_MAX):
            store.add_favorite(conn, None, f"f{n}", "x")
        with self.assertRaisesRegex(store.StoreError, "déjà"):
            store.add_favorite(conn, None, "une de trop", "x")
        self.assertEqual(len(store.list_favorites(conn, 1)), 0)  # the owner's account is another person
        conn.close()


class ExportTest(SharingTestCase):
    def test_markdown_json_and_a_page_to_print(self):
        cid = self.conversation("Boutique <en> ligne")
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Quel budget ?"}, headers=H)
        wait_idle(self.owner, cid)
        md = self.owner.get(f"/api/conversations/{cid}/export?format=md", headers=H)
        self.assertEqual(md.status_code, 200)
        self.assertIn("attachment", md.headers["content-disposition"])
        self.assertTrue(md.text.startswith("# Boutique <en> ligne"))
        self.assertIn("Quel budget ?", md.text)
        page = self.owner.get(f"/api/conversations/{cid}/export?format=html", headers=H).text
        self.assertIn("Boutique &lt;en&gt; ligne", page)  # escaped: a title is text, never markup
        self.assertNotIn("<en>", page)
        exported = self.owner.get(f"/api/conversations/{cid}/export?format=json", headers=H).content
        # Read back by the import: the same conversation, here or elsewhere.
        [again] = importer.parse_export(exported)
        self.assertEqual((again.source, again.title), ("aihub", "Boutique <en> ligne"))
        self.assertEqual([r for r, _, _ in again.messages], ["user", "assistant"])
        self.assertEqual(again.messages[0][1], "Quel budget ?")
        self.assertEqual(self.owner.get(f"/api/conversations/{cid}/export?format=exe", headers=H).status_code, 400)

    def test_only_what_one_sees(self):
        cid = self.conversation("Secrète")
        guest, uid = self.guest("Lou")
        self.assertEqual(guest.get(f"/api/conversations/{cid}/export?format=md", headers=H).status_code, 404)
        self.share(uid, "read", conversation_id=cid)
        self.assertEqual(guest.get(f"/api/conversations/{cid}/export?format=md", headers=H).status_code, 200)


class UsageTest(SharingTestCase):
    def test_each_reply_counts_for_who_asked(self):
        guest, uid = self.guest("Lou")
        cid = self.conversation("Site")
        self.share(uid, "write", conversation_id=cid)
        conn = db.connect(self.db_path)
        agent = store.create_agent(conn, "Claude", "claude_code", "haiku")
        owner_id = conn.execute("SELECT id FROM users WHERE is_owner = 1").fetchone()[0]

        def exchange(user_id, tokens):
            store.add_user_message(conn, cid, "une demande", user_id)
            mid = store.start_request(conn, cid, agent)
            store.mark_message_running(conn, mid)
            store.finish_message(conn, mid, "ok", content="réponse", tokens_in=tokens, tokens_out=1)
        exchange(uid, 100)
        exchange(uid, 50)
        exchange(None, 7)   # before accounts, or the owner
        conn.execute("UPDATE messages SET provider = 'claude_code' WHERE role = 'assistant'")
        importer.write_messages(conn, cid, [importer.Imported("k", "chatgpt", "x", db.utc_now(),
                                                               [("user", "importé", None), ("assistant", "vieux", None)])],
                                merged=False)
        conn.close()
        people = {p["name"]: p for p in self.owner.get("/api/usage?days=7", headers=H).json()["people"]}
        self.assertEqual((people["Lou"]["requests"], people["Lou"]["runs"], people["Lou"]["tokens_in"]), (2, 2, 150))
        self.assertEqual(people["Lou"]["by_provider"]["claude_code"]["runs"], 2)
        owner = next(p for p in people.values() if p["user_id"] == owner_id)
        self.assertEqual((owner["requests"], owner["runs"], owner["tokens_in"]), (1, 1, 7))  # the import costs nothing
        self.assertEqual(guest.get("/api/usage", headers=H).status_code, 403)
        self.assertEqual(self.owner.get("/api/usage?days=3", headers=H).status_code, 400)

    def test_an_old_reply_is_out_of_the_period(self):
        conn = db.connect(self.db_path)
        cid = store.create_conversation(conn, "T", workdir=str(self.workdir))
        agent = store.create_agent(conn, "Claude", "claude_code", "haiku")
        store.add_user_message(conn, cid, "x")
        mid = store.start_request(conn, cid, agent)
        store.mark_message_running(conn, mid)
        store.finish_message(conn, mid, "ok", content="r", tokens_in=5, tokens_out=5)
        conn.execute("UPDATE messages SET provider = 'claude_code', created_at = '2020-01-01T00:00:00.000+00:00'")
        self.assertEqual(usage.usage(conn, 7), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
