"""Instructions the AI always gets, for a conversation and for a project."""

import unittest
from pathlib import Path

from .helpers import read_log
from .test_sharing import H, SharingTestCase, wait_idle


class InstructionsTest(SharingTestCase):
    def sent_system(self):
        args = read_log(Path(self.env["FAKE_CLAUDE_LOG"]))["args"]
        return args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""

    def test_the_projects_then_the_conversations_at_each_request(self):
        pid = self.owner.post("/api/projects", json={"name": "Boutique", "instructions": "Le code est en Python."},
                              headers=H).json()["project"]["id"]
        cid = self.conversation("Site", project_id=pid)
        r = self.owner.patch(f"/api/conversations/{cid}", json={"instructions": "Réponds en français, sois bref."},
                             headers=H)
        self.assertEqual(r.json()["conversation"]["instructions"], "Réponds en français, sois bref.")
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Bonjour"}, headers=H)
        wait_idle(self.owner, cid)
        system = self.sent_system()
        self.assertIn("Le code est en Python.", system)
        self.assertIn("Réponds en français, sois bref.", system)
        self.assertLess(system.index("Python"), system.index("français"))  # the project's first
        # Cleared: nothing more is sent.
        self.owner.patch(f"/api/conversations/{cid}", json={"instructions": "  "}, headers=H)
        self.owner.patch(f"/api/projects/{pid}", json={"instructions": ""}, headers=H)
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Encore"}, headers=H)
        wait_idle(self.owner, cid)
        self.assertNotIn("Consignes de la personne", self.sent_system())

    def test_a_writer_does_not_set_them_and_the_size_is_bounded(self):
        cid = self.conversation("Partagée")
        guest, uid = self.guest("Lou")
        self.share(uid, "write", conversation_id=cid)
        self.assertEqual(guest.patch(f"/api/conversations/{cid}", json={"instructions": "x"}, headers=H).status_code, 403)
        r = self.owner.patch(f"/api/conversations/{cid}", json={"instructions": "x" * 4001}, headers=H)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
