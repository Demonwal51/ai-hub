"""1.1: searching the text of a vault, and a vault note added to a project's memory that follows its file."""

import os
import unittest
from unittest import mock

from aihub import obsidian, vaults
from tests.helpers import TempDirMixin
from tests.test_vaults import make_vault

try:
    from tests.test_web import WebTestCase
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = RolesTestCase = None


class SearchTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.vault = make_vault(self.make_tempdir() / "coffre", [
            ("Recettes/Tarte aux pommes.md", "Des pommes, du sucre, de la cannelle. Pommes encore."),
            ("Jardin.md", "Le pommier fleurit en avril, une pomme viendra ; la cannelle n'y pousse pas."),
            ("Vide.md", ""),
            (".trash/Pommes.md", "pommes cachées dans la corbeille")])

    def test_every_word_in_the_text_or_the_name_best_first(self):
        data = vaults.search(str(self.vault), "Cannelle POMMES")
        self.assertEqual([r["path"] for r in data["results"]], ["Recettes/Tarte aux pommes.md"])
        self.assertIn("cannelle", data["results"][0]["excerpt"])
        two = vaults.search(str(self.vault), "cannelle")["results"]
        self.assertEqual({r["path"] for r in two}, {"Recettes/Tarte aux pommes.md", "Jardin.md"})
        by_name = vaults.search(str(self.vault), "jardin avril")["results"]
        self.assertEqual([r["path"] for r in by_name], ["Jardin.md"])
        # The vault's hidden folders are not searched.
        self.assertNotIn(".trash/Pommes.md", [r["path"] for r in vaults.search(str(self.vault), "corbeille")["results"]])

    def test_the_most_hits_first(self):
        results = vaults.search(str(self.vault), "pomme")["results"]
        self.assertEqual(results[0]["path"], "Recettes/Tarte aux pommes.md")
        self.assertGreater(results[0]["hits"], results[1]["hits"])

    def test_bounded_and_said(self):
        with mock.patch.object(vaults, "MAX_SEARCH_NOTES", 1):
            data = vaults.search(str(self.vault), "cannelle")
        self.assertFalse(data["complete"])
        self.assertEqual(data["looked"], 1)
        for bad in ("", "   ", "x" * 201):
            with self.subTest(bad=bad[:10]), self.assertRaises(obsidian.ObsidianError):
                vaults.search(str(self.vault), bad)

    def test_only_the_start_of_a_long_note_is_read(self):
        (self.vault / "Long.md").write_text("début " + "x" * 200 + " cachée", encoding="utf-8")
        with mock.patch.object(vaults, "MAX_NOTE_BYTES", 100):
            self.assertEqual(vaults.search(str(self.vault), "cachée")["results"], [])
            self.assertEqual([r["path"] for r in vaults.search(str(self.vault), "début")["results"]], ["Long.md"])

    def test_a_long_excerpt_is_cut_around_the_word(self):
        (self.vault / "Long.md").write_text("début " + "mot " * 300 + "trouvaille " + "fin " * 300, encoding="utf-8")
        excerpt = vaults.search(str(self.vault), "trouvaille")["results"][0]["excerpt"]
        self.assertIn("trouvaille", excerpt)
        self.assertTrue(excerpt.startswith("…") and excerpt.endswith("…"))
        self.assertLess(len(excerpt), 200)


if WebTestCase is not None:
    class LinkedNoteTests(WebTestCase):
        def setUp(self):
            super().setUp()
            self.vault = make_vault(self.make_tempdir() / "coffre", [("Décisions.md", "Nous choisissons PostgreSQL pour la base.")])
            r = self.client.post("/api/projects", json={"name": "Boutique"})
            self.pid = r.json()["project"]["id"]
            self.cid = self.new_conversation(title="Site", project_id=self.pid)

        def link(self, path="Décisions.md"):
            return self.client.post(f"/api/projects/{self.pid}/memory/notes", json={"vault": str(self.vault), "path": path})

        def passages(self, words):
            return self.client.get(f"/api/conversations/{self.cid}/memory", params={"q": words}).json()["passages"]

        def test_a_note_added_follows_its_file(self):
            r = self.link()
            self.assertEqual(r.status_code, 201, r.text)
            document = r.json()["document"]
            self.assertEqual((document["name"], document["source_missing"]), ("Décisions.md", 0))
            self.assertTrue(document["source_path"].endswith("Décisions.md"))
            self.assertTrue(self.passages("PostgreSQL"))
            note = self.vault / "Décisions.md"
            note.write_text("Finalement : SQLite, plus simple.", encoding="utf-8")
            stat = note.stat()
            os.utime(note, (stat.st_atime, stat.st_mtime + 10))  # a change Obsidian would make
            self.assertTrue(self.passages("SQLite"))
            self.assertEqual(self.passages("PostgreSQL"), [])

        def test_the_search_an_ai_request_makes_reads_the_note_as_it_is(self):
            # rag.search alone (what a request to an AI runs), without listing the documents first.
            from aihub import db, rag
            self.link()
            note = self.vault / "Décisions.md"
            note.write_text("Finalement : SQLite, plus simple.", encoding="utf-8")
            stat = note.stat()
            os.utime(note, (stat.st_atime, stat.st_mtime + 10))
            conn = db.connect(self.db_path)
            try:
                self.assertTrue(rag.search(conn, self.pid, "SQLite"))
                self.assertEqual(rag.search(conn, self.pid, "PostgreSQL"), [])
            finally:
                conn.close()

        def test_a_note_gone_keeps_its_last_copy(self):
            self.link()
            (self.vault / "Décisions.md").unlink()
            memory = self.client.get(f"/api/conversations/{self.cid}/memory", params={"q": "PostgreSQL"}).json()
            self.assertTrue(memory["passages"])
            self.assertEqual(memory["documents"][0]["source_missing"], 1)

        def test_refusals(self):
            self.assertEqual(self.link().status_code, 201)
            again = self.link()
            self.assertEqual(again.status_code, 400)
            self.assertIn("porte déjà ce nom", again.json()["detail"])
            self.assertEqual(self.link("../dehors.md").status_code, 400)
            with mock.patch.object(vaults, "MAX_NOTE_BYTES", 5):
                (self.vault / "Long.md").write_text("une longue note", encoding="utf-8")
                r = self.link("Long.md")
            self.assertEqual(r.status_code, 400)
            self.assertIn("trop longue", r.json()["detail"])

        def test_the_vault_search_route(self):
            r = self.client.get("/api/obsidian/vault/search", params={"vault": str(self.vault), "q": "postgresql"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([x["path"] for x in r.json()["results"]], ["Décisions.md"])


if RolesTestCase is not None:
    class LinkedNoteRightsTests(RolesTestCase):
        def test_only_the_owner_reads_a_vault_into_a_project(self):
            vault = make_vault(self.make_tempdir() / "coffre", [("N.md", "texte")])
            admin, uid = self.person("Ada", role="admin")
            r = admin.post("/api/projects", json={"name": "À Ada"}, headers=H)
            pid = r.json()["project"]["id"]
            r = admin.post(f"/api/projects/{pid}/memory/notes", json={"vault": str(vault), "path": "N.md"}, headers=H)
            self.assertEqual(r.status_code, 403)
            self.assertEqual(admin.get("/api/obsidian/vault/search", params={"vault": str(vault), "q": "texte"},
                                       headers=H).status_code, 403)
            r = self.owner.post(f"/api/projects/{pid}/memory/notes", json={"vault": str(vault), "path": "N.md"}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)


if __name__ == "__main__":
    unittest.main()
