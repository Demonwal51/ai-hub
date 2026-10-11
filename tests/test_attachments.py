"""Files and images sent with a request: what each AI gets, who may send or see them."""

import base64
import json
import os
import unittest
from pathlib import Path

from aihub import attachments, db, providers
from aihub.providers import codex, ollama

from .fake_ollama import FakeOllama
from .helpers import FAKE_CODEX, TempDirMixin, read_log
from .test_sharing import H, SharingTestCase, wait_idle

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class WebAttachmentTest(SharingTestCase):
    def upload(self, client, cid, name, data, mime="application/octet-stream"):
        from urllib.parse import quote
        r = client.post(f"/api/conversations/{cid}/attachments", content=data,
                        headers={**H, "X-Filename": quote(name), "Content-Type": mime})
        return r

    def test_a_text_file_in_the_request_and_an_image_by_its_path(self):
        cid = self.conversation("Code")
        text = self.upload(self.owner, cid, "notes été.py", "print('bonjour')\n".encode(), "text/x-python")
        self.assertEqual(text.status_code, 201, text.text)
        image = self.upload(self.owner, cid, "capture.png", PNG, "image/png").json()
        self.assertTrue(image["image"])
        self.assertNotIn("stored", image)  # the page never knows the server's paths
        r = self.owner.post(f"/api/conversations/{cid}/messages",
                            json={"prompt": "Regarde", "attachments": [text.json()["id"], image["id"]]}, headers=H)
        self.assertEqual(r.status_code, 202, r.text)
        data = wait_idle(self.owner, cid)
        sent = read_log(Path(self.env["FAKE_CLAUDE_LOG"]))
        prompt = sent["user_message"]["message"]["content"]
        self.assertIn('<piece_jointe nom="notes été.py">', prompt)
        self.assertIn("print('bonjour')", prompt)  # the text itself: every AI reads it
        folder = str(Path(self.db_path).resolve().with_name("hub-pieces") / str(cid))
        self.assertIn(f'chemin="{folder}', prompt)
        self.assertEqual(sent["args"][sent["args"].index("--add-dir") + 1], folder)  # Claude Code may read it
        self.assertEqual([a["name"] for a in data["messages"][0]["attachments"]], ["notes été.py", "capture.png"])
        # A picture is shown; anything else is downloaded, never opened as a page of the hub.
        shown = self.owner.get(f"/api/attachments/{image['id']}", headers=H)
        self.assertEqual((shown.headers["content-type"], shown.content), ("image/png", PNG))
        self.assertTrue(shown.headers["content-disposition"].startswith("inline"))
        page = self.upload(self.owner, cid, "piège.html", b"<script>alert(1)</script>", "text/html").json()
        got = self.owner.get(f"/api/attachments/{page['id']}", headers=H)
        self.assertEqual(got.headers["content-type"], "application/octet-stream")
        self.assertTrue(got.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        # Taken once: sent again, it is unknown.
        r = self.owner.post(f"/api/conversations/{cid}/messages",
                            json={"prompt": "Encore", "attachments": [image["id"]]}, headers=H)
        self.assertEqual(r.status_code, 404)

    def test_who_may_send_and_see(self):
        cid = self.conversation("Partagée")
        guest, uid = self.guest("Lou")
        self.share(uid, "read", conversation_id=cid)
        self.assertEqual(self.upload(guest, cid, "a.txt", b"x").status_code, 403)  # reading only
        mine = self.upload(self.owner, cid, "a.txt", b"x").json()
        self.share(uid, "write", conversation_id=cid)
        # Someone else's file is not one's to send nor to take back.
        r = guest.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "attachments": [mine["id"]]}, headers=H)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(guest.delete(f"/api/attachments/{mine['id']}", headers=H).status_code, 404)
        other = self.conversation("Secrète")
        secret = self.upload(self.owner, other, "s.txt", b"secret").json()
        self.assertEqual(guest.get(f"/api/attachments/{secret['id']}", headers=H).status_code, 404)
        self.assertEqual(self.upload(self.owner, cid, "vide.txt", b"").status_code, 400)
        too_many = [self.upload(self.owner, cid, f"{n}.txt", b"x").json()["id"] for n in range(attachments.MAX_PER_REQUEST + 1)]
        r = self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "attachments": too_many}, headers=H)
        self.assertIn("au plus", r.json()["detail"])

    def test_the_files_go_with_their_conversation(self):
        cid = self.conversation("À effacer")
        kept = self.upload(self.owner, cid, "a.txt", b"x").json()
        self.assertEqual(self.owner.delete(f"/api/attachments/{kept['id']}", headers=H).status_code, 200)
        self.upload(self.owner, cid, "b.txt", b"y")
        folder = Path(self.db_path).resolve().with_name("hub-pieces") / str(cid)
        self.assertEqual(sorted(p.name.split("-", 1)[1] for p in folder.iterdir()), ["b.txt"])
        self.assertEqual(self.owner.delete(f"/api/conversations/{cid}", headers=H).status_code, 200)
        self.assertFalse(folder.exists())

    def test_a_conductor_gets_none_yet(self):
        r = self.owner.post("/api/conversations", json={"title": "Plan", "workdir": str(self.workdir),
                                                         "mode": "orchestration", "conductor_model": "opus"}, headers=H)
        cid = r.json()["conversation"]["id"]
        f = self.upload(self.owner, cid, "a.txt", b"x").json()
        r = self.owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "x", "attachments": [f["id"]]},
                            headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("une IA", r.json()["detail"])


class UnitTest(TempDirMixin, unittest.TestCase):
    def test_names_are_ours(self):
        for given, kept in (("../../windows/system.ini", "system.ini"), ("a:b?.txt", "a-b-.txt"), ("", "fichier"),
                            ("..", "fichier"), ("C:\\x\\y.txt", "y.txt")):
            self.assertEqual(attachments.safe_name(given), kept)

    def test_an_unsent_file_goes_after_a_day(self):
        tmp = self.make_tempdir()
        conn = db.connect(str(tmp / "hub.db"))
        self.addCleanup(conn.close)
        from aihub import store
        cid = store.create_conversation(conn, "T", workdir=str(tmp))
        row = attachments.save(conn, tmp / "pieces", cid, None, "a.txt", b"x")
        self.assertEqual(attachments.purge_unsent(conn), 0)  # just sent: kept
        conn.execute("UPDATE attachments SET created_at = '2020-01-01T00:00:00.000+00:00'")
        self.assertEqual(attachments.purge_unsent(conn), 1)
        self.assertFalse(Path(row["stored"]).exists())

    def test_codex_and_the_local_ai_see_the_image(self):
        tmp = self.make_tempdir()
        image = tmp / "photo.png"
        image.write_bytes(PNG)
        log = tmp / "codex.json"
        env = dict(os.environ, FAKE_CODEX_LOG=str(log))
        run = providers.new_run(codex.PROVIDER, FAKE_CODEX, prompt="Que vois-tu ?", workdir=str(tmp),
                                permission_mode="plan", env=env, attachments=[{"path": str(image), "image": True}])
        self.assertEqual(run.run().status, "ok")
        sent = read_log(log)["turn_params"]["input"]
        self.assertIn({"type": "localImage", "path": str(image)}, sent)
        with FakeOllama() as fake:
            run = providers.new_run(ollama.PROVIDER, [fake.url], prompt="Que vois-tu ?", workdir=str(tmp),
                                    permission_mode="plan", model="local:qwen3.5:4b",
                                    attachments=[{"path": str(image), "image": True},
                                                 {"path": str(tmp / "x.pdf"), "image": False}])
            run.run()
            user = [m for m in fake.requests[0]["messages"] if m["role"] == "user"][0]
        self.assertEqual(user["images"], [base64.b64encode(PNG).decode()])


if __name__ == "__main__":
    unittest.main()
