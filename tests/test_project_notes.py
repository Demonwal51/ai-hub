"""1.1: a vault per project, and a note per conversation (an existing note is never taken without asking)."""

import unittest

from tests.test_obsidian import WebTestCase

try:
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    RolesTestCase = None


if WebTestCase is not None:
    class ProjectVaultTests(WebTestCase):
        scenario = "auto"

        def setUp(self):
            root = self.make_tempdir()
            self.vault = root / "coffre-des-reglages"
            self.own = root / "coffre-du-projet"
            self.vault.mkdir()
            self.own.mkdir()
            self.app_kwargs = {"obsidian_vault": str(self.vault)}
            super().setUp()
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())

        def project(self, **body):
            r = self.client.post("/api/projects", json={"name": "Boutique", **body})
            self.assertEqual(r.status_code, 201, r.text)
            return r.json()["project"]

        def orchestration(self, project_id=None, title="Mon site"):
            return self.new_conversation(title=title, mode="orchestration", conductor_model="opus",
                                         assignment_mode="auto", project_id=project_id)

        def test_a_projects_vault_takes_its_notes(self):
            project = self.project(obsidian_vault=str(self.own))
            self.assertEqual(project["obsidian_vault"], str(self.own.resolve()))
            cid = self.orchestration(project["id"])
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.own.resolve()))
            self.assertEqual(self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True}).status_code, 200)
            self.send(cid, "fais le site")
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            self.wait_idle(cid)
            note = self.own / "Projets AI Hub" / "Mon site.md"
            self.assertIn("— plan terminé", note.read_text(encoding="utf-8"))
            self.assertEqual(list(self.vault.rglob("*.md")), [])  # nothing in the settings' vault
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 200)
            self.assertIn("— plan terminé", note.read_text(encoding="utf-8"))
            self.assertEqual(list(self.vault.rglob("*.md")), [])

        def test_a_reply_noted_by_hand_goes_to_the_projects_vault(self):
            project = self.project(obsidian_vault=str(self.own))
            cid = self.new_conversation(title="Simple", project_id=project["id"])
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.send(cid, "dis bonjour")
            reply = self.wait_idle(cid)["messages"][-1]
            self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/obsidian").status_code, 200)
            self.assertIn("Bonjour", (self.own / "Projets AI Hub" / "Simple.md").read_text(encoding="utf-8"))

        def test_without_its_own_the_settings_vault_and_back(self):
            project = self.project()
            self.assertIsNone(project["obsidian_vault"])
            cid = self.orchestration(project["id"])
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.vault))
            r = self.client.patch(f"/api/projects/{project['id']}", json={"obsidian_vault": str(self.own)})
            self.assertEqual(r.json()["project"]["obsidian_vault"], str(self.own.resolve()))
            r = self.client.patch(f"/api/projects/{project['id']}", json={"obsidian_vault": ""})
            self.assertIsNone(r.json()["project"]["obsidian_vault"])
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.vault))

        def test_a_bad_vault_is_refused(self):
            for bad in ("coffre-relatif", str(self.own / "absent"), "x" * 1001):
                with self.subTest(bad=bad[:30]):
                    r = self.client.post("/api/projects", json={"name": f"P{len(bad)}", "obsidian_vault": bad})
                    self.assertEqual(r.status_code, 400, r.text)

        def test_the_vaults_window_says_whose(self):
            self.project(obsidian_vault=str(self.own))
            self.app.state.vault_hooks = {"lists": [], "roots": []}
            found = {v["name"]: v for v in self.client.get("/api/obsidian/vaults?refresh=true").json()["vaults"]}
            self.assertEqual(found["coffre-du-projet"]["projects"], ["Boutique"])
            self.assertIn("projet", found["coffre-du-projet"]["sources"])
            self.assertTrue(found["coffre-du-projet"]["exists"])  # a notes folder, even without .obsidian
            notes = self.client.get("/api/obsidian/vault/notes", params={"vault": str(self.own)})
            self.assertEqual(notes.status_code, 200, notes.text)

    class NotePerConversationTests(WebTestCase):
        scenario = "auto"

        def setUp(self):
            self.vault = self.make_tempdir() / "coffre"
            (self.vault / "Projets AI Hub").mkdir(parents=True)
            self.app_kwargs = {"obsidian_vault": str(self.vault)}
            super().setUp()

        def test_an_existing_note_is_never_taken_without_asking(self):
            old = self.vault / "Projets AI Hub" / "Mon site.md"
            old.write_text("# Mon site\n\nL'ancienne note.\n", encoding="utf-8")
            cid = self.new_conversation(title="Mon site")
            found = self.client.get(f"/api/conversations/{cid}/obsidian/check").json()
            self.assertEqual((found["exists"], found["free"]), (True, "Projets AI Hub/Mon site (2).md"))
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("existe déjà", r.json()["detail"])
            self.assertIsNone(self.conversation(cid)["conversation"]["obsidian_note"])
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True, "obsidian_existing": "new"})
            self.assertEqual(r.json()["conversation"]["obsidian_note"], "Projets AI Hub/Mon site (2).md")
            self.assertEqual(old.read_text(encoding="utf-8"), "# Mon site\n\nL'ancienne note.\n")  # untouched
            self.assertIn("## Création", (self.vault / "Projets AI Hub" / "Mon site (2).md").read_text(encoding="utf-8"))

        def test_the_new_note_takes_the_first_free_number(self):
            for name in ("Mon site.md", "Mon site (2).md"):
                (self.vault / "Projets AI Hub" / name).write_text("déjà là\n", encoding="utf-8")
            cid = self.new_conversation(title="Mon site")
            self.assertEqual(self.client.get(f"/api/conversations/{cid}/obsidian/check").json()["free"],
                             "Projets AI Hub/Mon site (3).md")
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True, "obsidian_existing": "new"})
            self.assertEqual(r.json()["conversation"]["obsidian_note"], "Projets AI Hub/Mon site (3).md")
            self.assertEqual((self.vault / "Projets AI Hub" / "Mon site (2).md").read_text(encoding="utf-8"), "déjà là\n")

        def test_continuing_an_existing_note_and_who_writes_there(self):
            first = self.new_conversation(title="Site")
            self.client.patch(f"/api/conversations/{first}", json={"obsidian": True})
            second = self.new_conversation(title="Site")
            found = self.client.get(f"/api/conversations/{second}/obsidian/check").json()
            self.assertEqual((found["exists"], found["used_by"]), (True, ["Site"]))
            r = self.client.patch(f"/api/conversations/{second}", json={"obsidian": True, "obsidian_existing": "continue"})
            self.assertEqual(r.json()["conversation"]["obsidian_note"], "Projets AI Hub/Site.md")
            self.assertEqual((self.vault / "Projets AI Hub" / "Site.md").read_text(encoding="utf-8").count("## Création"), 2)

        def test_a_free_note_needs_no_question(self):
            cid = self.new_conversation(title="Neuve")
            found = self.client.get(f"/api/conversations/{cid}/obsidian/check").json()
            self.assertEqual((found["exists"], found["used_by"], found["free"]), (False, [], "Projets AI Hub/Neuve.md"))
            self.assertEqual(self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True}).status_code, 200)


if RolesTestCase is not None:
    class ProjectVaultRightsTests(RolesTestCase):
        def test_only_the_owner_chooses_a_projects_vault(self):
            folder = self.make_tempdir()
            lea, uid = self.person("Léa", role="membre")
            r = lea.post("/api/projects", json={"name": "À Léa", "obsidian_vault": str(folder)}, headers=H)
            self.assertEqual(r.status_code, 403)
            r = lea.post("/api/projects", json={"name": "À Léa"}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            pid = r.json()["project"]["id"]
            self.assertEqual(lea.patch(f"/api/projects/{pid}", json={"obsidian_vault": str(folder)}, headers=H).status_code, 403)
            r = self.owner.patch(f"/api/projects/{pid}", json={"obsidian_vault": str(folder)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
