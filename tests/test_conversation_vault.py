"""1.2.2: a conversation linked to Obsidian keeps its own vault, chosen when it is linked
(a new vault, or one already there). Its own first, then its project's, then the settings'."""

import os
import sqlite3
import unittest

from aihub import db
from tests.helpers import TempDirMixin
from tests.test_obsidian import WebTestCase

try:
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    RolesTestCase = None


class SchemaTests(TempDirMixin, unittest.TestCase):
    def test_a_base_gets_the_column(self):
        path = self.make_tempdir() / "base.db"
        conn = db.connect(str(path))
        try:
            columns = [r["name"] for r in conn.execute("PRAGMA table_info(conversations)")]
            self.assertIn("obsidian_vault", columns)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        finally:
            conn.close()
        # A base of the previous schema (28) is brought up to date, its conversations kept, without a vault of their own.
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES ('Gardée', 'x', 'x')")
        conn.execute("ALTER TABLE conversations DROP COLUMN obsidian_vault")
        conn.execute("PRAGMA user_version = 28")
        conn.commit()
        conn.close()
        conn = db.connect(str(path))
        try:
            rows = conn.execute("SELECT title, obsidian_vault FROM conversations").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("Gardée", None)])
        finally:
            conn.close()


if WebTestCase is not None:
    class ConversationVaultTests(WebTestCase):
        scenario = "auto"

        def setUp(self):
            root = self.make_tempdir()
            self.settings_vault, self.project_vault = root / "coffre-des-reglages", root / "coffre-du-projet"
            self.own, self.other = root / "mon-coffre", root / "un-autre-coffre"
            for folder in (self.settings_vault, self.project_vault, self.own, self.other):
                folder.mkdir()
            self.app_kwargs = {"obsidian_vault": str(self.settings_vault)}
            super().setUp()

        def link(self, cid, vault, **more):
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(vault), "obsidian": True, **more})
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()["conversation"]

        def notes(self, folder):
            return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.md"))

        def test_its_own_vault_comes_first(self):
            pid = self.client.post("/api/projects", json={"name": "Boutique", "obsidian_vault": str(self.project_vault)}).json()["project"]["id"]
            cid = self.new_conversation(title="Simple", project_id=pid)
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.project_vault.resolve()))
            conv = self.link(cid, self.own)
            self.assertEqual((conv["obsidian_vault"], conv["obsidian_note"]), (str(self.own.resolve()), "Projets AI Hub/Simple.md"))
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.own.resolve()))
            self.send(cid, "dis bonjour")
            reply = self.wait_idle(cid)["messages"][-1]
            self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/obsidian").status_code, 200)
            self.assertIn("Bonjour", (self.own / "Projets AI Hub" / "Simple.md").read_text(encoding="utf-8"))
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 200)
            self.assertEqual((self.notes(self.project_vault), self.notes(self.settings_vault)), ([], []))

        def test_another_vault_makes_the_note_again_there(self):
            cid = self.new_conversation(title="Site")
            self.link(cid, self.own)
            first = self.own / "Projets AI Hub" / "Site.md"
            self.assertIn("## Création", first.read_text(encoding="utf-8"))
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(self.other)})
            self.assertIsNone(r.json()["conversation"]["obsidian_note"])  # linked again, in the new vault
            self.assertTrue(first.is_file())  # the former note stays where it is
            conv = self.link(cid, self.other)
            self.assertEqual(conv["obsidian_note"], "Projets AI Hub/Site.md")
            self.assertIn("## Création", (self.other / "Projets AI Hub" / "Site.md").read_text(encoding="utf-8"))
            # The same vault again: nothing undone.
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(self.other)})
            self.assertEqual(r.json()["conversation"]["obsidian_note"], "Projets AI Hub/Site.md")
            # Empty: back to the settings' vault.
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": ""})
            self.assertIsNone(r.json()["conversation"]["obsidian_vault"])
            self.assertEqual(self.conversation(cid)["conversation"]["notes_vault"], str(self.settings_vault))

        def test_a_bad_vault_is_refused_and_nothing_changes(self):
            cid = self.new_conversation(title="Site")
            bad = ["coffre-relatif", str(self.own / "absent")]
            if os.name == "nt":
                bad.append(r"\\serveur\partage\coffre")
            for path in bad:
                with self.subTest(path=path):
                    r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": path, "obsidian": True})
                    self.assertEqual(r.status_code, 400, r.text)
                    conv = self.conversation(cid)["conversation"]
                    self.assertEqual((conv["obsidian_vault"], conv["obsidian_note"]), (None, None))

        def test_an_existing_note_is_asked_about_in_the_chosen_vault(self):
            (self.own / "Projets AI Hub").mkdir()
            (self.own / "Projets AI Hub" / "Site.md").write_text("déjà là\n", encoding="utf-8")
            cid = self.new_conversation(title="Site")
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(self.own)})
            self.assertEqual(r.status_code, 200, r.text)
            found = self.client.get(f"/api/conversations/{cid}/obsidian/check").json()
            self.assertEqual((found["vault"], found["exists"]), (str(self.own.resolve()), True))
            conv = self.link(cid, self.own, obsidian_existing="new")
            self.assertEqual(conv["obsidian_note"], "Projets AI Hub/Site (2).md")
            self.assertEqual((self.own / "Projets AI Hub" / "Site.md").read_text(encoding="utf-8"), "déjà là\n")

        def test_who_else_writes_there_counts_their_own_vaults(self):
            first = self.new_conversation(title="Site")
            self.link(first, self.own)
            second = self.new_conversation(title="Site")
            self.client.patch(f"/api/conversations/{second}", json={"obsidian_vault": str(self.own)})
            found = self.client.get(f"/api/conversations/{second}/obsidian/check").json()
            self.assertEqual((found["exists"], found["used_by"]), (True, ["Site"]))
            third = self.new_conversation(title="Site")
            self.client.patch(f"/api/conversations/{third}", json={"obsidian_vault": str(self.other)})
            found = self.client.get(f"/api/conversations/{third}/obsidian/check").json()
            self.assertEqual((found["exists"], found["used_by"]), (False, []))

        def test_the_vault_reader_opens_a_conversations_vault(self):
            # A plain folder (no .obsidian): read only once AI Hub writes notes there.
            r = self.client.get("/api/obsidian/vault/notes", params={"vault": str(self.own)})
            self.assertEqual(r.status_code, 400)
            cid = self.new_conversation(title="Site")
            self.link(cid, self.own)
            r = self.client.get("/api/obsidian/vault/notes", params={"vault": str(self.own)})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("Projets AI Hub/Site.md", [n["path"] for n in r.json()["notes"]])


if RolesTestCase is not None:
    class ConversationVaultRightsTests(RolesTestCase):
        def test_only_the_owner_chooses_a_conversations_vault(self):
            folder = self.make_tempdir()
            for role in ("membre", "admin"):
                with self.subTest(role=role):
                    client, _ = self.person(f"Personne {role}", role=role)
                    r = client.post("/api/conversations", json={"title": f"À {role}"}, headers=H)
                    self.assertEqual(r.status_code, 201, r.text)
                    cid = r.json()["conversation"]["id"]
                    r = client.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(folder)}, headers=H)
                    self.assertEqual(r.status_code, 403, r.text)
            cid = self.conversation("Au propriétaire")
            r = self.owner.patch(f"/api/conversations/{cid}", json={"obsidian_vault": str(folder)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["conversation"]["obsidian_vault"], str(folder.resolve()))


if __name__ == "__main__":
    unittest.main()
