"""Regressions found in the 0.5 review, using only temporary accounts and fake providers."""
import unittest
from pathlib import Path
from unittest import mock

from aihub import accounts, db, routing, runner, store
from .test_sharing import H, SharingTestCase, wait_idle


class SecondaryAccountsTest(SharingTestCase):
    def test_manual_summary_uses_the_sender_accounts(self):
        cid = self.conversation("Mémoire")
        guest, uid = self.guest("Résumé")
        conn = db.connect(self.db_path)
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
        conn.close()
        with mock.patch.object(runner, "summarizer", return_value=None) as summarize:
            response = guest.post(f"/api/conversations/{cid}/fresh-start", json={}, headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        env = summarize.call_args.kwargs["env"]
        self.assertIn(str(uid), Path(env["CODEX_HOME"]).parts)
        self.assertIn(str(uid), Path(env["CLAUDE_CONFIG_DIR"]).parts)
        self.assertIsNot(env, self.env)

    def test_no_connected_personal_account_never_uses_the_owner_resumer(self):
        env = {"CODEX_HOME": "personal", "CLAUDE_CONFIG_DIR": "personal"}
        with mock.patch.object(accounts, "available_models", return_value={}) as available:
            self.assertIsNone(runner.summarizer({}, str(self.workdir), "haiku", env=env))
        self.assertEqual(available.call_args.args[1], env)


class BranchContextTest(SharingTestCase):
    def test_files_and_instructions_survive_the_source_deletion(self):
        cid = self.conversation("Documents")
        self.owner.patch(f"/api/conversations/{cid}", json={"instructions": "Réponds brièvement."}, headers=H)
        file = self.owner.post(f"/api/conversations/{cid}/attachments", content=b"un document",
                               headers={**H, "X-Filename": "note.txt", "Content-Type": "text/plain"}).json()
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Lis", "attachments": [file["id"]]},
                        headers=H)
        data = wait_idle(self.owner, cid)
        request = data["messages"][0]
        response = self.owner.post(f"/api/messages/{request['id']}/branch", json={"prompt": "Relis"}, headers=H)
        self.assertEqual(response.status_code, 201, response.text)
        branch = wait_idle(self.owner, response.json()["conversation"])
        self.assertEqual(branch["conversation"]["instructions"], "Réponds brièvement.")
        copied = branch["messages"][0]["attachments"][0]["id"]
        self.assertNotEqual(copied, file["id"])
        self.assertEqual(self.owner.delete(f"/api/conversations/{cid}", headers=H).status_code, 200)
        self.assertEqual(self.owner.get(f"/api/attachments/{copied}", headers=H).content, b"un document")

    def test_a_missing_file_does_not_leave_a_partial_branch(self):
        cid = self.conversation("Fichier absent")
        file = self.owner.post(f"/api/conversations/{cid}/attachments", content=b"x",
                               headers={**H, "X-Filename": "note.txt"}).json()
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Lis", "attachments": [file["id"]]},
                        headers=H)
        request = wait_idle(self.owner, cid)["messages"][0]
        conn = db.connect(self.db_path)
        Path(conn.execute("SELECT stored FROM attachments WHERE id = ?", (file["id"],)).fetchone()[0]).unlink()
        conn.close()
        response = self.owner.post(f"/api/messages/{request['id']}/branch", json={}, headers=H)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.owner.get("/api/conversations", headers=H).json()["conversations"]), 1)


class RecoveryAndLocalTest(unittest.TestCase):
    def test_interrupted_comparison_is_closed(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        cid = store.create_conversation(conn, "Interrompue")
        request = store.add_user_message(conn, cid, "Question")
        conn.execute("INSERT INTO variants (conversation_id, request_id, status, created_at) VALUES (?, ?, 'running', ?)",
                     (cid, request, db.utc_now()))
        store.recover_interrupted(conn)
        self.assertEqual(conn.execute("SELECT status FROM variants").fetchone()[0], "stopped")

    def test_local_fallback_is_explicit_and_respects_removed_agents(self):
        connected = {"ollama": {"local:modele"}}
        chosen = routing.route("code", connected)
        self.assertEqual(chosen.rule.provider, "ollama")
        self.assertIn("lecture seule", chosen.reason)
        self.assertIsNone(routing.route("code", connected, excluded={("ollama", "local:modele")}))


if __name__ == "__main__":
    unittest.main()
