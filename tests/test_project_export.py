"""1.1: a whole project in one .zip, exported and imported back (here or on another AI Hub)."""

import io
import json
import unittest
import zipfile
from unittest import mock

from aihub import project_export
from tests.helpers import TempDirMixin

try:
    from tests.test_web import WebTestCase
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = RolesTestCase = None


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


HEAD = {"aihub_project": 1, "name": "P", "conversations": [], "documents": []}


class ReadTests(TempDirMixin, unittest.TestCase):
    def test_refused_before_anything(self):
        cases = {
            "pas un zip": b"bonjour",
            "sans projet.json": make_zip({"autre.txt": "x"}),
            "autre format": make_zip({"projet.json": json.dumps({**HEAD, "aihub_project": 99})}),
            "sans nom": make_zip({"projet.json": json.dumps({**HEAD, "name": " "})}),
            "conversation hors liste": make_zip({"projet.json": json.dumps({**HEAD, "conversations": [{"file": "../x.json"}]}),
                                                 "../x.json": "{}"}),
            "document hors dossier": make_zip({"projet.json": json.dumps({**HEAD, "documents": ["documents/../../x.md"]}),
                                               "documents/../../x.md": "texte"}),
            "document à côté": make_zip({"projet.json": json.dumps({**HEAD, "documents": ["autre/x.md"]}), "autre/x.md": "texte"}),
            "note au nom inattendu": make_zip({"projet.json": json.dumps({**HEAD, "conversations": [
                {"file": "conversations/001.json", "note": "C:/notes/x.md"}]}), "conversations/001.json": "{}",
                "C:/notes/x.md": "note"}),
            "annoncé mais absent": make_zip({"projet.json": json.dumps({**HEAD, "conversations": [{"file": "conversations/001.json"}]})}),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(project_export.ProjectFileError):
                project_export.read(data)

    def test_a_zip_bomb_and_a_crowded_zip(self):
        data = make_zip({"projet.json": json.dumps(HEAD), "documents/gros.md": "x" * 5000})
        with mock.patch.object(project_export, "MAX_FILE", 1000):
            with self.assertRaisesRegex(project_export.ProjectFileError, "trop gros"):
                project_export.read(data)
        with mock.patch.object(project_export, "MAX_FILES", 1):
            with self.assertRaisesRegex(project_export.ProjectFileError, "trop de fichiers"):
                project_export.read(data)
        with mock.patch.object(project_export, "MAX_ZIP", 10):
            with self.assertRaisesRegex(project_export.ProjectFileError, "trop gros"):
                project_export.read(data)


if WebTestCase is not None:
    class ExportImportTests(WebTestCase):
        def setUp(self):
            self.vault = self.make_tempdir() / "coffre"
            (self.vault / "Projets AI Hub").mkdir(parents=True)
            self.app_kwargs = {"obsidian_vault": str(self.vault)}
            super().setUp()
            r = self.client.post("/api/projects", json={"name": "Boutique", "description": "La boutique en ligne",
                                                        "instructions": "Réponds en français."})
            self.pid = r.json()["project"]["id"]
            self.first = self.new_conversation(title="Logo", project_id=self.pid)
            self.send(self.first, "le logo en vert")
            self.wait_idle(self.first)
            self.client.patch(f"/api/conversations/{self.first}", json={"instructions": "Sois bref.", "obsidian": True})
            self.second = self.new_conversation(title="Couleurs", project_id=self.pid)
            self.send(self.second, "les couleurs")
            self.wait_idle(self.second)
            self.client.post(f"/api/projects/{self.pid}/memory/documents", json={"name": "Charte.md", "content": "Vert sapin."})

        def exported(self):
            r = self.client.get(f"/api/projects/{self.pid}/export")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.headers["content-type"], "application/zip")
            return r.content

        def test_what_the_zip_holds(self):
            bundle = project_export.read(self.exported())
            self.assertEqual((bundle["name"], bundle["description"], bundle["instructions"]),
                             ("Boutique", "La boutique en ligne", "Réponds en français."))
            titles = [c["export"]["title"] for c in bundle["conversations"]]
            self.assertEqual(titles, ["Logo", "Couleurs"])  # oldest first
            logo = bundle["conversations"][0]
            self.assertEqual(logo["instructions"], "Sois bref.")
            self.assertIn("le logo en vert", [m["content"] for m in logo["export"]["messages"]])
            self.assertIn("## Création", logo["note"])
            self.assertIsNone(bundle["conversations"][1]["note"])
            self.assertEqual(bundle["documents"], [("Charte.md", "Vert sapin.")])

        def test_imported_back_as_a_new_project(self):
            existing = self.vault / "Projets AI Hub" / "Logo.md"
            before = existing.read_text(encoding="utf-8")
            r = self.client.post("/api/projects/import", params={"notes": "true", "workdir": str(self.workdir)},
                                 content=self.exported())
            self.assertEqual(r.status_code, 201, r.text)
            made = r.json()
            self.assertEqual((made["project"]["name"], made["conversations"], made["documents"], made["notes"]),
                             ("Boutique (importé)", 2, 1, 1))
            self.assertEqual(made["project"]["instructions"], "Réponds en français.")
            new_pid = made["project"]["id"]
            convs = [c for c in self.client.get("/api/conversations").json()["conversations"] if c["project_id"] == new_pid]
            logo = next(c for c in convs if c["title"] == "Logo")
            data = self.conversation(logo["id"])
            self.assertEqual(data["conversation"]["instructions"], "Sois bref.")
            self.assertIn("le logo en vert", [m["content"] for m in data["messages"]])
            # The note is copied, never over the one that is there.
            self.assertEqual(data["conversation"]["obsidian_note"], "Projets AI Hub/Logo (2).md")
            self.assertEqual(existing.read_text(encoding="utf-8"), before)
            self.assertIn("## Création", (self.vault / "Projets AI Hub" / "Logo (2).md").read_text(encoding="utf-8"))
            # Once more: another name again.
            again = self.client.post("/api/projects/import", params={"workdir": str(self.workdir)}, content=self.exported()).json()
            self.assertEqual((again["project"]["name"], again["notes"]), ("Boutique (importé 2)", 0))

        def test_a_bad_zip_creates_nothing(self):
            count = len(self.client.get("/api/projects").json()["projects"])
            r = self.client.post("/api/projects/import", params={"workdir": str(self.workdir)}, content=b"pas un zip")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(len(self.client.get("/api/projects").json()["projects"]), count)

        def test_all_or_nothing(self):
            data = self.exported()
            count = len(self.client.get("/api/projects").json()["projects"])
            # The owner's conversations work on this PC: no folder chosen, nothing made.
            r = self.client.post("/api/projects/import", content=data)
            self.assertEqual(r.status_code, 400)
            self.assertIn("dossier de travail", r.json()["detail"])
            # A failure halfway (the 2nd conversation): the project and the 1st one go too.
            from aihub import importer
            real = importer.write_messages
            calls = []
            def second_fails(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    raise importer.ImportFailed("panne au milieu")
                return real(*args, **kwargs)
            with mock.patch.object(importer, "write_messages", second_fails):
                r = self.client.post("/api/projects/import", params={"workdir": str(self.workdir)}, content=data)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(self.client.get("/api/projects").json()["projects"]), count)
            titles = [c["title"] for c in self.client.get("/api/conversations").json()["conversations"]]
            self.assertEqual(sorted(titles), ["Couleurs", "Logo"])  # only the originals


if RolesTestCase is not None:
    class ExportImportRightsTests(RolesTestCase):
        def test_the_notes_are_the_owners(self):
            from fastapi.testclient import TestClient
            from aihub import auth
            from aihub.web.app import create_app
            from tests.helpers import FAKE_COMMAND
            vault = self.make_tempdir() / "coffre"
            (vault / "Projets AI Hub").mkdir(parents=True)
            app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, obsidian_vault=str(vault),
                             auth_policy=auth.Policy(), run_scheduler=False)
            with TestClient(app, client=("127.0.0.1", 50000)) as owner:
                self.assertEqual(owner.post("/api/auth/login", json={"password": self.password}).status_code, 200)
                pid = owner.post("/api/projects", json={"name": "Notes"}, headers=H).json()["project"]["id"]
                cid = owner.post("/api/conversations", json={"title": "Notée", "workdir": str(self.workdir), "project_id": pid},
                                 headers=H).json()["conversation"]["id"]
                owner.patch(f"/api/conversations/{cid}", json={"obsidian": True}, headers=H)
                # A conversation with a message: an empty one is not imported, nor its note.
                from tests.test_sharing import wait_idle
                self.assertEqual(owner.post(f"/api/conversations/{cid}/messages", json={"prompt": "bonjour"},
                                            headers=H).status_code, 202)
                wait_idle(owner, cid)
                with_note = owner.get(f"/api/projects/{pid}/export", headers=H).content
                self.assertTrue(project_export.read(with_note)["conversations"][0]["export"]["messages"])
                for name, role in (("Ada", "admin"), ("Léa", "membre")):
                    r = owner.post("/api/users", json={"name": name, "password": "", "role": role}, headers=H)
                    self.assertEqual(r.status_code, 201, r.text)
                ada = TestClient(app, client=("127.0.0.1", 50201))
                ada.__enter__()
                self.addCleanup(ada.__exit__, None, None, None)
                ada.post("/api/auth/login", json={"name": "Ada", "password": ""})
                bundle = project_export.read(ada.get(f"/api/projects/{pid}/export", headers=H).content)
                self.assertIsNone(bundle["conversations"][0]["note"])  # an admin: not the vault's text
                self.assertIn("## Création", project_export.read(with_note)["conversations"][0]["note"])
                lea = TestClient(app, client=("127.0.0.1", 50202))
                lea.__enter__()
                self.addCleanup(lea.__exit__, None, None, None)
                lea.post("/api/auth/login", json={"name": "Léa", "password": ""})
                before = sorted(p.name for p in (vault / "Projets AI Hub").iterdir())
                r = lea.post("/api/projects/import?notes=true", content=with_note, headers=H)
                self.assertEqual(r.status_code, 201, r.text)
                self.assertEqual(r.json()["conversations"], 1)  # the conversation came: only its note did not
                self.assertEqual(r.json()["notes"], 0)  # a member writes nothing in the owner's vault
                self.assertEqual(sorted(p.name for p in (vault / "Projets AI Hub").iterdir()), before)

        def test_who_may(self):
            r = self.owner.post("/api/projects", json={"name": "Privé"}, headers=H)
            pid = r.json()["project"]["id"]
            data = self.owner.get(f"/api/projects/{pid}/export", headers=H).content
            sam, _ = self.person("Sam")  # a guest: sees nothing of the project, creates nothing
            self.assertEqual(sam.get(f"/api/projects/{pid}/export", headers=H).status_code, 404)
            self.assertEqual(sam.post("/api/projects/import", content=data, headers=H).status_code, 403)
            lea, _ = self.person("Léa", role="membre")  # a member creates projects: may import one
            r = lea.post("/api/projects/import?notes=true", content=data, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["notes"], 0)


if __name__ == "__main__":
    unittest.main()
