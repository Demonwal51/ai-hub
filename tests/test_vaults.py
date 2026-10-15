"""The Obsidian vaults of the PC: found three ways, opened to read, the owner's only."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from aihub import obsidian, vaults
from tests.helpers import TempDirMixin

try:
    from tests.test_sharing import H, SharingTestCase
except unittest.SkipTest:  # web dependencies missing
    SharingTestCase = None


def make_vault(path, notes=()):
    path = Path(path)
    (path / ".obsidian").mkdir(parents=True)
    for name, text in notes:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    return path


def obsidian_list(path, *vault_paths):
    """An Obsidian app list, as the app writes it."""
    path.write_text(json.dumps({"vaults": {f"id{i}": {"path": str(p), "ts": 1, "open": i == 0}
                                           for i, p in enumerate(vault_paths)}}), encoding="utf-8")
    return path


class WalkTests(TempDirMixin, unittest.TestCase):
    def test_finds_vaults_within_its_depth_only(self):
        root = self.make_tempdir()
        near = make_vault(root / "notes" / "travail")
        make_vault(root / "a" / "b" / "c" / "trop-loin")
        make_vault(root / ".cache" / "cache")           # hidden folder
        make_vault(root / "node_modules" / "paquet")    # a system folder
        make_vault(near / "dans-un-coffre")             # a vault is not looked into
        found, complete = vaults.walk([(root, 2)])
        self.assertTrue(complete)
        self.assertEqual([Path(p) for p in found], [near])

    def test_a_bounded_walk_says_it_stopped(self):
        root = self.make_tempdir()
        for i in range(5):
            (root / f"d{i}").mkdir()
        make_vault(root / "zz")
        found, complete = vaults.walk([(root, 1)], max_dirs=3)
        self.assertFalse(complete)
        self.assertEqual(found, [])


class SourcesTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        self.record = self.root / "hub-coffres-crees.json"

    def test_obsidians_list_and_a_broken_one(self):
        good = obsidian_list(self.root / "obsidian.json", "C:/un", "D:/deux")
        broken = self.root / "casse.json"
        broken.write_text("{pas du json", encoding="utf-8")
        self.assertEqual(vaults.registered([good, broken, self.root / "absent.json"]), ["C:/un", "D:/deux"])

    def test_the_list_of_created_vaults_keeps_one_entry_each(self):
        one = make_vault(self.root / "un")
        vaults.record_created(self.record, one)
        vaults.record_created(self.record, one)
        self.assertEqual([Path(v["path"]) for v in vaults.created(self.record)], [one.resolve()])
        self.assertEqual(vaults.created(self.root / "absent.json"), [])

    def test_sources_are_merged_by_folder(self):
        everywhere = make_vault(self.root / "disques" / "partout", [("a.md", "x"), (".obsidian/x.md", "y"),
                                                                    (".trash/b.md", "z"), ("dossier/c.md", "w"),
                                                                    ("image.png", "")])
        by_hand = make_vault(self.root / "disques" / "a-la-main")
        configured = make_vault(self.root / "ailleurs" / "notes-aihub")
        vaults.record_created(self.record, everywhere)
        lists = [obsidian_list(self.root / "obsidian.json", everywhere, self.root / "efface")]
        found = vaults.discover(configured=str(configured), record=self.record, lists=lists,
                                roots=[(self.root / "disques", 1)], refresh=True)
        by_name = {v["name"]: v for v in found["vaults"]}
        self.assertEqual(set(by_name), {"partout", "a-la-main", "notes-aihub", "efface"})
        # Also on the walked disk, but "found on the disk" is said of the vaults known by nothing else.
        self.assertEqual(by_name["partout"]["sources"], ["aihub", "obsidian"])
        self.assertEqual(by_name["partout"]["notes"], 2)  # not .obsidian, .trash nor the image
        self.assertEqual(by_name["a-la-main"]["sources"], ["disque"])
        self.assertEqual(Path(by_name["a-la-main"]["path"]), by_hand.resolve())
        self.assertEqual(found["vaults"][0]["name"], "notes-aihub")  # the active one first
        self.assertTrue(by_name["notes-aihub"]["active"])
        self.assertEqual(by_name["notes-aihub"]["sources"], ["reglages"])
        self.assertFalse(by_name["efface"]["exists"])           # still in Obsidian's list, gone from the disk
        self.assertEqual(found["vaults"][-1]["name"], "efface")
        self.assertEqual(sum(v["active"] for v in found["vaults"]), 1)

    def test_a_known_vaults_brothers_are_found(self):
        known = make_vault(self.root / "loin" / "connu")
        brother = make_vault(self.root / "loin" / "frere")
        lists = [obsidian_list(self.root / "obsidian.json", known)]
        found = vaults.discover(lists=lists, roots=[], refresh=True)
        self.assertIn(str(brother.resolve()), [v["path"] for v in found["vaults"]])

    def test_obsidians_list_is_looked_for_where_the_app_keeps_it(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.root)}):
            places = vaults.obsidian_lists()
        if os.name == "nt":
            self.assertEqual(places, [self.root / "obsidian" / "obsidian.json"])
        else:
            self.assertTrue(all(p.name == "obsidian.json" for p in places))


class ReadingTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.vault = make_vault(self.make_tempdir() / "coffre", [
            ("Accueil.md", "# Accueil\n\nVoir [[Projet]]."), ("Projets/Projet.md", "Le projet"),
            (".obsidian/workspace.md", "caché"), ("notes.txt", "pas une note")])

    def test_notes_outside_hidden_folders(self):
        data = vaults.list_notes(str(self.vault))
        self.assertEqual([n["path"] for n in data["notes"]], ["Accueil.md", "Projets/Projet.md"])
        self.assertTrue(data["complete"])
        with mock.patch.object(vaults, "MAX_NOTES", 1):
            data = vaults.list_notes(str(self.vault))
        self.assertEqual(len(data["notes"]), 1)
        self.assertFalse(data["complete"])

    def test_a_note_is_read_inside_its_vault_only(self):
        self.assertEqual(vaults.read_note(str(self.vault), "Projets/Projet.md")["content"], "Le projet")
        outside = self.vault.parent / "dehors.md"
        outside.write_text("secret", encoding="utf-8")
        for bad in ("../dehors.md", str(outside), ".obsidian/workspace.md", "notes.txt", "absente.md", ""):
            with self.subTest(bad=bad), self.assertRaises(obsidian.ObsidianError):
                vaults.read_note(str(self.vault), bad)

    def test_only_a_vault_opens(self):
        plain = self.vault.parent / "simple-dossier"
        plain.mkdir()
        for bad in (str(plain), "coffre", str(self.vault.parent / "absent"), None):
            with self.subTest(bad=bad), self.assertRaises(obsidian.ObsidianError):
                vaults.list_notes(bad)

    def test_the_settings_folder_opens_even_without_obsidian_in_it(self):
        # AI Hub writes its notes into any folder: the trial server's vault has no .obsidian.
        plain = self.vault.parent / "notes-aihub"
        (plain / "Projets AI Hub").mkdir(parents=True)
        (plain / "Projets AI Hub" / "Site.md").write_text("note", encoding="utf-8")
        found = vaults.discover(configured=str(plain), lists=[], roots=[], refresh=True)["vaults"]
        mine = next(v for v in found if v["active"])
        self.assertEqual((mine["exists"], mine["notes"]), (True, 1))
        self.assertEqual([n["path"] for n in vaults.list_notes(str(plain), configured=str(plain))["notes"]],
                         ["Projets AI Hub/Site.md"])
        self.assertEqual(vaults.read_note(str(plain), "Projets AI Hub/Site.md", configured=str(plain))["content"], "note")
        with self.assertRaises(obsidian.ObsidianError):
            vaults.list_notes(str(plain))  # not the settings' folder: an ordinary folder

    @unittest.skipUnless(os.name == "nt", "chemins réseau de Windows")
    def test_a_network_vault_is_never_looked_at(self):
        touched = []
        real = Path.is_dir
        def spy(self_path):
            touched.append(str(self_path))
            return real(self_path)
        with mock.patch.object(Path, "is_dir", spy):
            for path in (r"\\serveur\partage\coffre", "//serveur/partage/coffre"):
                with self.subTest(path=path), self.assertRaisesRegex(obsidian.ObsidianError, "disque local"):
                    vaults.list_notes(path)
                with self.subTest(note=path), self.assertRaisesRegex(obsidian.ObsidianError, "disque local"):
                    vaults.read_note(path, "Note.md")
        self.assertEqual([p for p in touched if "serveur" in p], [])

    def test_a_long_note_is_cut(self):
        with mock.patch.object(vaults, "MAX_NOTE_BYTES", 4):
            note = vaults.read_note(str(self.vault), "Accueil.md")
        self.assertEqual((note["content"], note["cut"]), ("# Ac", True))

    def test_a_link_out_of_the_vault_is_not_listed(self):
        outside = self.vault.parent / "ailleurs.md"
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.vault / "lien.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("liens symboliques non permis ici")
        self.assertNotIn("lien.md", [n["path"] for n in vaults.list_notes(str(self.vault))["notes"]])
        with self.assertRaises(obsidian.ObsidianError):
            vaults.read_note(str(self.vault), "lien.md")


if SharingTestCase is not None:
    class VaultsWebTests(SharingTestCase):
        def setUp(self):
            super().setUp()
            self.disk = self.make_tempdir()
            self.vault = make_vault(self.disk / "Mon coffre", [("Note.md", "Bonjour [[Autre]]")])
            # Never the real disk nor the real Obsidian list in a test.
            self.app.state.vault_hooks = {"lists": [obsidian_list(self.disk / "obsidian.json")],
                                          "roots": [(self.disk, 1)]}

        def test_the_owner_finds_opens_and_reads(self):
            found = self.owner.get("/api/obsidian/vaults", headers=H).json()
            self.assertEqual([(v["name"], v["sources"]) for v in found["vaults"]], [("Mon coffre", ["disque"])])
            notes = self.owner.get("/api/obsidian/vault/notes", params={"vault": str(self.vault)}, headers=H).json()
            self.assertEqual([n["path"] for n in notes["notes"]], ["Note.md"])
            note = self.owner.get("/api/obsidian/vault/note", params={"vault": str(self.vault), "path": "Note.md"},
                                  headers=H).json()
            self.assertEqual(note["content"], "Bonjour [[Autre]]")
            r = self.owner.get("/api/obsidian/vault/note", params={"vault": str(self.vault), "path": "../x.md"}, headers=H)
            self.assertEqual(r.status_code, 400)

        def test_a_vault_created_here_is_remembered(self):
            folder = self.disk / "loin" / "plus-loin" / "Créé ici"  # deeper than the walk goes
            r = self.owner.post("/api/obsidian/vaults", json={"path": str(folder), "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            found = self.owner.get("/api/obsidian/vaults", params={"refresh": "true"}, headers=H).json()["vaults"]
            created = next(v for v in found if v["name"] == "Créé ici")
            self.assertEqual(created["sources"], ["aihub"])
            self.assertEqual(created["notes"], 0)

        def test_only_the_owner(self):
            guest, uid = self.guest("Admin")
            self.owner.patch(f"/api/users/{uid}", json={"role": "admin"}, headers=H)
            for path, params in (("/api/obsidian/vaults", {}), ("/api/obsidian/vault/notes", {"vault": str(self.vault)}),
                                 ("/api/obsidian/vault/note", {"vault": str(self.vault), "path": "Note.md"})):
                with self.subTest(path=path):
                    self.assertEqual(guest.get(path, params=params, headers=H).status_code, 403)


if __name__ == "__main__":
    unittest.main()
