"""Compare two AIs: the other one answers the same request beside the reply, read only."""

import json
import unittest
from pathlib import Path

from aihub import db

from .helpers import read_log
from .test_sharing import H, SharingTestCase, wait_idle


class CompareTest(SharingTestCase):
    def setUp(self):
        super().setUp()
        self.runs = self.make_tempdir()
        self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.runs)  # one log per run: both AIs' are kept

    def all_runs(self):
        return [read_log(p) for p in self.runs.glob("run-*.json")]

    def test_another_ai_answers_beside_read_only(self):
        cid = self.conversation("Site")
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Avant"}, headers=H)
        wait_idle(self.owner, cid)
        r = self.owner.post(f"/api/conversations/{cid}/messages",
                            json={"prompt": "Quel plan ?", "compare_with": "sonnet"}, headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        data = wait_idle(self.owner, cid)
        request = data["messages"][-2]
        [variant] = request["variants"]
        self.assertEqual((variant["model"], variant["status"], variant["content"]), ("sonnet", "ok", "Bonjour"))
        side = [run for run in self.all_runs() if "--model" in run["args"]
                and run["args"][run["args"].index("--model") + 1] == "sonnet"]
        self.assertEqual(len(side), 1)
        args = side[0]["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")  # never changes a file
        self.assertNotIn("--resume", args)
        prompt = side[0]["user_message"]["message"]["content"]
        self.assertTrue(prompt.startswith("<memoire_de_la_conversation>"))  # it knows what was said before
        self.assertIn("Avant", prompt)
        self.assertTrue(prompt.endswith("Quel plan ?"))
        # It counts for whoever asked.
        people = self.owner.get("/api/usage?days=7", headers=H).json()["people"]
        self.assertEqual(sum(p["runs"] for p in people), 3)  # two replies and the compared answer

    def test_keep_it_and_the_next_request_starts_from_the_memory(self):
        cid = self.conversation("Choix")
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Une idée ?", "compare_with": "sonnet"},
                        headers=H)
        data = wait_idle(self.owner, cid)
        reply, variant = data["messages"][-1], data["messages"][-2]["variants"][0]
        conn = db.connect(self.db_path)
        conn.execute("UPDATE variants SET content = 'La réponse de Sonnet' WHERE id = ?", (variant["id"],))
        conn.close()
        self.assertEqual(self.owner.post(f"/api/variants/{variant['id']}/keep", headers=H).status_code, 200)
        data = self.owner.get(f"/api/conversations/{cid}", headers=H).json()
        self.assertEqual((data["messages"][-1]["content"], data["messages"][-1]["model"]),
                         ("La réponse de Sonnet", "sonnet"))
        self.assertEqual(data["messages"][-2]["variants"][0]["content"], reply["content"])  # the old one, beside
        for p in self.runs.glob("run-*.json"):
            p.unlink()
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "Et ensuite ?"}, headers=H)
        wait_idle(self.owner, cid)
        # The request's own run (the memory's summary may be written first, by another run).
        [run] = [r for r in self.all_runs() if "Et ensuite ?" in ((r.get("user_message") or {}).get("message") or {})
                 .get("content", "")]
        self.assertNotIn("--resume", run["args"])  # no session knows the text kept
        self.assertIn("La réponse de Sonnet", run.get("memory", ""))

    def test_refusals(self):
        cid = self.conversation("x")
        r = self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "compare_with": "inconnue"},
                            headers=H)
        self.assertEqual(r.status_code, 400)
        r = self.owner.post("/api/conversations", json={"title": "Plan", "workdir": str(self.workdir),
                                                         "mode": "orchestration", "conductor_model": "opus"}, headers=H)
        plan = r.json()["conversation"]["id"]
        r = self.owner.post(f"/api/conversations/{plan}/messages", json={"prompt": "x", "compare_with": "sonnet"},
                            headers=H)
        self.assertIn("une IA", r.json()["detail"])
        self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "compare_with": "sonnet"}, headers=H)
        variant = wait_idle(self.owner, cid)["messages"][-2]["variants"][0]
        guest, uid = self.guest("Lou")
        self.share(uid, "read", conversation_id=cid)
        self.assertEqual(guest.post(f"/api/variants/{variant['id']}/keep", headers=H).status_code, 403)
        other = self.conversation("Secrète")
        self.assertEqual(guest.post(f"/api/variants/{variant['id'] + 99}/keep", headers=H).status_code, 404)


if __name__ == "__main__":
    unittest.main()
