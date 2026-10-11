"""Regenerate the last reply; edit a request in a new conversation (a branch)."""

import unittest
from pathlib import Path

from aihub import db

from .helpers import read_log
from .test_sharing import H, SharingTestCase, wait_idle


class RegenerateTest(SharingTestCase):
    def ask(self, cid, prompt, client=None, **more):
        client = client or self.owner
        r = client.post(f"/api/conversations/{cid}/messages", json={"prompt": prompt, **more}, headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        return wait_idle(client, cid)

    def test_the_last_request_sent_again_the_old_reply_gone(self):
        cid = self.conversation("Site")
        self.ask(cid, "Première")
        before = self.ask(cid, "Deuxième")["messages"]
        r = self.owner.post(f"/api/conversations/{cid}/regenerate", json={}, headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        after = wait_idle(self.owner, cid)["messages"]
        self.assertEqual([m["content"] for m in after if m["role"] == "user"], ["Première", "Deuxième"])
        self.assertEqual(len(after), 4)
        # The old reply is gone, a new one written (SQLite may give it the same number again).
        self.assertNotEqual(after[-1]["created_at"], before[-1]["created_at"])
        self.assertEqual(sum(1 for m in after if m["role"] == "assistant"), 2)
        sent = read_log(Path(self.env["FAKE_CLAUDE_LOG"]))["user_message"]["message"]["content"]
        self.assertTrue(sent.startswith("Deuxième"))

    def test_its_files_follow_and_another_ai_may_answer(self):
        from urllib.parse import quote
        cid = self.conversation("Fichiers")
        f = self.owner.post(f"/api/conversations/{cid}/attachments", content=b"print(1)\n",
                            headers={**H, "X-Filename": quote("a.py"), "Content-Type": "text/x-python"}).json()
        self.ask(cid, "Lis", attachments=[f["id"]])
        self.owner.post(f"/api/conversations/{cid}/regenerate", json={"model": "sonnet"}, headers=H)
        data = wait_idle(self.owner, cid)
        self.assertEqual([a["name"] for a in data["messages"][-2]["attachments"]], ["a.py"])
        self.assertEqual(data["conversation"]["model"], "sonnet")
        self.assertIn("print(1)", read_log(Path(self.env["FAKE_CLAUDE_LOG"]))["user_message"]["message"]["content"])

    def test_refusals(self):
        cid = self.conversation("Vide")
        r = self.owner.post(f"/api/conversations/{cid}/regenerate", json={}, headers=H)
        self.assertIn("rien à régénérer", r.json()["detail"])
        self.ask(cid, "Une")
        guest, uid = self.guest("Lou")
        self.share(uid, "read", conversation_id=cid)
        self.assertEqual(guest.post(f"/api/conversations/{cid}/regenerate", json={}, headers=H).status_code, 403)
        self.share(uid, "write", conversation_id=cid)
        r = guest.post(f"/api/conversations/{cid}/regenerate", json={"model": "sonnet"}, headers=H)
        self.assertEqual(r.status_code, 403)  # the AI of the conversation is its owner's to change
        self.assertEqual(guest.post(f"/api/conversations/{cid}/regenerate", json={}, headers=H).status_code, 202)
        wait_idle(guest, cid)

    def test_only_a_request_then_its_reply_is_regenerated(self):
        cid = self.conversation("Fin")
        self.ask(cid, "Une")
        conn = db.connect(self.db_path)
        last = conn.execute("SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                            (cid,)).fetchone()[0]
        for request_role in ("assistant", "user"):
            with conn:  # the end becomes two replies, then two requests
                conn.execute("UPDATE messages SET role = ? WHERE id = ?", (request_role, last - 1))
                conn.execute("UPDATE messages SET role = ? WHERE id = ?",
                             ("assistant" if request_role == "assistant" else "user", last))
            r = self.owner.post(f"/api/conversations/{cid}/regenerate", json={}, headers=H)
            self.assertIn("rien à régénérer", r.json()["detail"], request_role)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                                          (cid,)).fetchone()[0], 2)
        conn.close()


class BranchTest(SharingTestCase):
    ask = RegenerateTest.ask  # the same helper, not the same tests run twice

    def test_a_branch_from_a_request_the_original_untouched(self):
        cid = self.conversation("Boutique")
        self.ask(cid, "Le logo ?")
        second = self.ask(cid, "Les couleurs ?")["messages"]
        request = next(m for m in second if m["content"] == "Les couleurs ?")
        r = self.owner.post(f"/api/messages/{request['id']}/branch", json={"prompt": "Les polices ?"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        new = r.json()["conversation"]
        data = wait_idle(self.owner, new)
        self.assertEqual(data["conversation"]["title"], "Boutique (branche)")
        self.assertEqual([m["content"] for m in data["messages"] if m["role"] == "user"], ["Le logo ?", "Les polices ?"])
        # The branch has no session: its first request starts from the memory of what was copied.
        sent = read_log(Path(self.env["FAKE_CLAUDE_LOG"]))
        self.assertIn("Le logo ?", sent.get("memory", ""))
        self.assertNotIn("--resume", sent["args"])
        original = self.owner.get(f"/api/conversations/{cid}", headers=H).json()["messages"]
        self.assertEqual([m["id"] for m in original], [m["id"] for m in second])
        # The copies cost nothing in the consumption.
        conn = db.connect(self.db_path)
        copied = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND provider IS NULL "
                              "AND role = 'assistant'", (new,)).fetchone()[0]
        conn.close()
        self.assertEqual(copied, 1)

    def test_a_guest_branches_nothing_and_a_reply_is_no_request(self):
        cid = self.conversation("Partagée")
        reply = self.ask(cid, "Une")["messages"][-1]
        r = self.owner.post(f"/api/messages/{reply['id']}/branch", json={}, headers=H)
        self.assertIn("demande", r.json()["detail"])
        guest, uid = self.guest("Lou")
        self.share(uid, "write", conversation_id=cid)
        request = self.owner.get(f"/api/conversations/{cid}", headers=H).json()["messages"][0]
        self.assertEqual(guest.post(f"/api/messages/{request['id']}/branch", json={}, headers=H).status_code, 403)


if __name__ == "__main__":
    unittest.main()
