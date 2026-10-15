"""The app's own folder picker: the server's folders and a working folder's files, from any device."""

import os
import unittest
from pathlib import Path
from unittest import mock

from aihub import browse
from aihub.store import StoreError
from tests.helpers import TempDirMixin

try:
    from tests.test_sharing import H, SharingTestCase
except unittest.SkipTest:  # web dependencies missing
    SharingTestCase = None


def link_folder(target, link):
    """A link to a folder: a symbolic one, or a junction where Windows refuses those without rights."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        import os
        if os.name != "nt":
            raise unittest.SkipTest("liens symboliques non permis ici")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


class FoldersTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        for name in ("Projets", "b-notes", ".cache", "$Recycle"):
            (self.root / name).mkdir()
        (self.root / "b-notes" / ".obsidian").mkdir()
        (self.root / "fichier.txt").write_text("x", encoding="utf-8")

    def test_folders_only_hidden_ones_left_out_vaults_marked(self):
        data = browse.folders(str(self.root))
        self.assertEqual([(e["name"], e["vault"]) for e in data["entries"]], [("b-notes", True), ("Projets", False)])
        self.assertEqual(Path(data["path"]), self.root.resolve())
        self.assertEqual(Path(data["parent"]), self.root.resolve().parent)
        self.assertFalse(data["vault"])
        self.assertTrue(browse.folders(str(self.root / "b-notes"))["vault"])

    def test_the_starting_places(self):
        data = browse.folders(None)
        self.assertIsNone(data["path"])
        self.assertTrue(data["starts"])  # the user's folder at least
        self.assertTrue(all(Path(s["path"]).is_dir() for s in data["starts"]))
        limited = browse.folders(None, roots=[str(self.root / "Projets")])
        self.assertEqual([Path(s["path"]) for s in limited["starts"]], [(self.root / "Projets").resolve()])
        self.assertTrue(limited["limited"])

    def test_the_allowed_folders_are_a_limit(self):
        roots = [str(self.root / "Projets")]
        (self.root / "Projets" / "site").mkdir()
        data = browse.folders(str(self.root / "Projets"), roots=roots)
        self.assertEqual([e["name"] for e in data["entries"]], ["site"])
        self.assertIsNone(data["parent"])  # no way up out of the allowed folder
        self.assertEqual(Path(browse.folders(str(self.root / "Projets" / "site"), roots=roots)["parent"]),
                         (self.root / "Projets").resolve())
        for outside in (str(self.root), str(self.root / "b-notes"), str(self.root / "Projets" / ".." / "b-notes")):
            with self.subTest(outside=outside), self.assertRaises(StoreError):
                browse.folders(outside, roots=roots)

    def test_bad_paths(self):
        for bad in ("Projets", ".", str(self.root / "absent"), str(self.root / "fichier.txt"), "x" * 1001, "a\x00b"):
            with self.subTest(bad=bad[:30]), self.assertRaises(StoreError):
                browse.folders(bad)
        # A clear word for the person, not the system's error.
        for missing in (str(self.root / "absent"), str(self.root / "fichier.txt")):
            with self.subTest(missing=missing), self.assertRaisesRegex(StoreError, "n'existe pas"):
                browse.folders(missing)

    @unittest.skipUnless(os.name == "nt", "chemins réseau de Windows")
    def test_a_network_path_is_never_looked_at(self):
        # Resolving \\serveur\partage would make Windows connect to that machine.
        touched = []
        real_resolve, real_is_dir, real_scandir = Path.resolve, Path.is_dir, os.scandir
        def spy(name, real):
            def wrapper(*args, **kwargs):
                touched.append((name, str(args[0])))
                return real(*args, **kwargs)
            return wrapper
        bad = [r"\\serveur\partage", "//serveur/partage", r"\\?\UNC\serveur\partage", r"\dossier", "C:dossier"]
        with mock.patch.object(Path, "resolve", spy("resolve", real_resolve)), \
             mock.patch.object(Path, "is_dir", spy("is_dir", real_is_dir)), \
             mock.patch.object(os, "scandir", spy("scandir", real_scandir)):
            for path in bad:
                with self.subTest(path=path), self.assertRaisesRegex(StoreError, "disque local"):
                    browse.folders(path)
                with self.subTest(base=path), self.assertRaisesRegex(StoreError, "disque local"):
                    browse.entries(path)
        self.assertEqual([t for t in touched if "serveur" in t[1] or "dossier" in t[1]], [])

    def test_a_crowded_folder_says_it_is_cut(self):
        with mock.patch.object(browse, "MAX_ENTRIES", 1):
            data = browse.folders(str(self.root))
        self.assertEqual(len(data["entries"]), 1)
        self.assertFalse(data["complete"])

    def test_a_link_out_of_the_allowed_folders_is_not_shown(self):
        allowed = self.root / "Projets"
        link_folder(self.root / "b-notes", allowed / "raccourci")
        self.assertTrue((allowed / "raccourci" / ".obsidian").is_dir())  # the link works: only the guard hides it
        self.assertEqual(browse.folders(str(allowed), roots=[str(allowed)])["entries"], [])


class EntriesTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.base = self.make_tempdir()
        (self.base / "src" / "lib").mkdir(parents=True)
        (self.base / "src" / "app.py").write_text("print(1)", encoding="utf-8")
        (self.base / "README.md").write_text("# Lisez-moi", encoding="utf-8")

    def test_files_and_folders_relative_to_the_working_folder(self):
        top = browse.entries(str(self.base))
        self.assertEqual([(e["path"], e["kind"]) for e in top["entries"]], [("src", "dir"), ("README.md", "file")])
        self.assertEqual(top["entries"][1]["size"], len("# Lisez-moi"))
        self.assertEqual((top["path"], top["parent"]), ("", None))
        inner = browse.entries(str(self.base), "src")
        self.assertEqual([e["path"] for e in inner["entries"]], ["src/lib", "src/app.py"])
        self.assertEqual((inner["path"], inner["parent"]), ("src", ""))
        self.assertEqual(browse.entries(str(self.base), "src/lib")["parent"], "src")

    def test_never_out_of_the_working_folder(self):
        outside = self.base.parent
        for bad in ("..", "src/../..", str(outside), "C:/Windows", "absent"):
            with self.subTest(bad=bad), self.assertRaises(StoreError):
                browse.entries(str(self.base), bad)

    def test_a_way_out_is_refused_before_it_is_looked_at(self):
        # "..", an absolute or a drive path: refused on its words, before any resolve of it.
        resolved = []
        real = Path.resolve
        def spy(self_path, *args, **kwargs):
            resolved.append(str(self_path))
            return real(self_path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", spy):
            for bad in ("..", "src/../../x", str(self.base.parent), r"\\serveur\partage", "C:x"):
                with self.subTest(bad=bad), self.assertRaisesRegex(StoreError, "relatif"):
                    browse.entries(str(self.base), bad)
        self.assertTrue(resolved)
        self.assertTrue(all(Path(p) == self.base for p in resolved), resolved)

    def test_a_folder_link_out_of_the_working_folder_is_neither_shown_nor_entered(self):
        outside = self.make_tempdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        link_folder(outside, self.base / "ailleurs")
        self.assertTrue((self.base / "ailleurs" / "secret.txt").is_file())
        self.assertNotIn("ailleurs", [e["path"] for e in browse.entries(str(self.base))["entries"]])
        with self.assertRaises(StoreError):
            browse.entries(str(self.base), "ailleurs")

    def test_a_link_out_of_the_working_folder_is_not_shown(self):
        secret = self.make_tempdir() / "secret.txt"
        secret.write_text("x", encoding="utf-8")
        try:
            (self.base / "lien.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("liens symboliques non permis ici")
        self.assertNotIn("lien.txt", [e["path"] for e in browse.entries(str(self.base))["entries"]])


if SharingTestCase is not None:
    class BrowseWebTests(SharingTestCase):
        def folders(self, client, path=None, **params):
            return client.get("/api/filesystem/folders", params={**({"path": str(path)} if path else {}), **params}, headers=H)

        def test_the_owner_browses_and_a_guest_does_not(self):
            (self.workdir / "site").mkdir()
            r = self.folders(self.owner, self.workdir)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([e["name"] for e in r.json()["entries"]], ["site"])
            guest, uid = self.guest("Lou")
            for scope in ("workdir", "settings"):
                with self.subTest(scope=scope):
                    self.assertEqual(self.folders(guest, self.workdir, scope=scope).status_code, 403)
            self.owner.patch(f"/api/users/{uid}", json={"role": "admin"}, headers=H)
            self.assertEqual(self.folders(guest, self.workdir).status_code, 200)
            # The settings are the owner's: an admin does not choose them.
            self.assertEqual(self.folders(guest, self.workdir, scope="settings").status_code, 403)

        def test_allowed_folders_limit_the_working_folder_not_the_owners_settings(self):
            from fastapi.testclient import TestClient
            from aihub import auth
            from aihub.web.app import create_app
            from tests.helpers import FAKE_COMMAND
            limited = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, allowed_roots=[self.workdir],
                                 auth_policy=auth.Policy(), run_scheduler=False)
            outside = self.make_tempdir()
            with TestClient(limited, client=("127.0.0.1", 50000)) as client:
                self.assertEqual(client.post("/api/auth/login", json={"password": self.password}).status_code, 200)
                self.assertEqual(self.folders(client, outside).status_code, 400)
                self.assertEqual(self.folders(client, self.workdir).status_code, 200)
                starts = self.folders(client).json()["starts"]
                self.assertEqual([Path(s["path"]) for s in starts], [self.workdir.resolve()])
                self.assertEqual(self.folders(client, outside, scope="settings").status_code, 200)
                # Picking an isolated space's files from a folder typed by hand: the same limit.
                r = client.get("/api/filesystem/entries", params={"base": str(outside)}, headers=H)
                self.assertEqual(r.status_code, 400)
                self.assertIn("non autorisé", r.json()["detail"])
                r = client.get("/api/filesystem/entries", params={"base": str(self.workdir)}, headers=H)
                self.assertEqual(r.status_code, 200)

        @unittest.skipUnless(os.name == "nt", "chemins réseau de Windows")
        def test_a_network_working_folder_is_refused_before_it_is_resolved(self):
            touched = []
            real = Path.resolve
            def spy(self_path, *args, **kwargs):
                touched.append(str(self_path))
                return real(self_path, *args, **kwargs)
            with mock.patch.object(Path, "resolve", spy):
                r = self.owner.get("/api/filesystem/entries", params={"base": r"\\serveur\partage"}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("disque local", r.json()["detail"])
            self.assertEqual([p for p in touched if "serveur" in p], [])

        def test_a_conversations_files_for_its_owner(self):
            (self.workdir / "notes.md").write_text("x", encoding="utf-8")
            cid = self.conversation()
            r = self.owner.get("/api/filesystem/entries", params={"conversation_id": cid}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("notes.md", [e["path"] for e in r.json()["entries"]])
            r = self.owner.get("/api/filesystem/entries", params={"base": str(self.workdir)}, headers=H)
            self.assertEqual(r.status_code, 200)
            guest, uid = self.guest("Lou")
            self.share(uid, "write", conversation_id=cid)
            # Writing in a shared conversation is not owning it: its files stay hidden.
            self.assertEqual(guest.get("/api/filesystem/entries", params={"conversation_id": cid}, headers=H).status_code, 403)
            self.assertEqual(guest.get("/api/filesystem/entries", params={"base": str(self.workdir)}, headers=H).status_code, 403)
            self.assertEqual(self.owner.get("/api/filesystem/entries", headers=H).status_code, 400)


if __name__ == "__main__":
    unittest.main()
