"""1.2.3: one rule for every file and folder of the PC (aihub/places.py), and the app's explorer
for each thing done with files: memory, attachment, import, saving a conversation, vaults."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from aihub import places
from aihub.store import StoreError
from tests.helpers import TempDirMixin

try:
    from fastapi.testclient import TestClient
    from aihub import auth, db
    from aihub.web.app import create_app
    from tests.helpers import FAKE_COMMAND, fake_env
    from tests.test_auth import Clock, new_password
    from tests.test_importer import claude_export
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    TestClient = None


class RuleTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        self.inside = self.root / "dedans"
        self.inside.mkdir()
        (self.inside / "notes.md").write_text("# Notes\n", encoding="utf-8")
        self.outside = self.make_tempdir()
        (self.outside / "secret.md").write_text("secret\n", encoding="utf-8")
        self.rule = places.Places([self.root])

    def test_inside_the_allowed_folders_only(self):
        self.assertEqual(self.rule.folder(str(self.inside)), self.inside.resolve())
        for path in (self.outside, self.outside / "secret.md"):
            with self.subTest(path=path), self.assertRaisesRegex(StoreError, "non autorisé"):
                self.rule.file(str(path)) if path.suffix else self.rule.folder(str(path))
        # No allowed folder: every local disk.
        self.assertEqual(places.Places().folder(str(self.outside)), self.outside.resolve())

    def test_what_it_must_be(self):
        with self.assertRaisesRegex(StoreError, "n'existe pas"):
            self.rule.folder(str(self.inside / "notes.md"))  # a file is not a folder
        with self.assertRaisesRegex(StoreError, "n'existe pas"):
            self.rule.file(str(self.inside))
        with self.assertRaisesRegex(StoreError, "genre accepté"):
            self.rule.file(str(self.inside / "notes.md"), extensions={".txt"})
        with self.assertRaisesRegex(StoreError, "trop gros"):
            self.rule.file(str(self.inside / "notes.md"), max_size=3)
        self.assertEqual(self.rule.file(str(self.inside / "notes.md"), extensions={".md"}).name, "notes.md")
        for bad in ("", "relatif", "x" * 1001, "a\x00b"):
            with self.subTest(bad=bad[:20]), self.assertRaisesRegex(StoreError, "chemin complet"):
                self.rule.folder(bad)

    def test_a_new_name_is_a_name_never_a_path(self):
        self.assertEqual(self.rule.child(str(self.inside), "Nouveau"), self.inside.resolve() / "Nouveau")
        self.assertEqual(self.rule.child(str(self.inside), " Nouveau ").name, "Nouveau")  # spaces around: trimmed
        for bad in ("../dehors", "a/b", "a\\b", "C:x", "CON", "nom.", "..", "", "x" * 121, "a*b"):
            with self.subTest(bad=bad), self.assertRaisesRegex(StoreError, "nom simple"):
                self.rule.child(str(self.inside), bad)
        with self.assertRaisesRegex(StoreError, "non autorisé"):
            self.rule.child(str(self.outside), "Nouveau")

    def test_a_free_name_takes_the_first_number(self):
        target = self.inside / "notes.md"
        self.assertEqual(places.free(target).name, "notes (2).md")
        (self.inside / "notes (2).md").write_text("x", encoding="utf-8")
        self.assertEqual(places.free(target).name, "notes (3).md")
        self.assertEqual(places.free(self.inside / "neuf.md").name, "neuf.md")

    @unittest.skipUnless(os.name == "nt", "liens de dossier de Windows")
    def test_a_link_judged_where_it_leads(self):
        import _winapi
        link = self.inside / "vers-dehors"
        _winapi.CreateJunction(str(self.outside), str(link))
        with self.assertRaisesRegex(StoreError, "non autorisé"):
            self.rule.folder(str(link))
        with self.assertRaisesRegex(StoreError, "non autorisé"):
            self.rule.file(str(link / "secret.md"))

    @unittest.skipUnless(os.name == "nt", "chemins réseau de Windows")
    def test_a_network_path_is_never_resolved(self):
        touched = []
        real = Path.resolve

        def spy(self_path, *args, **kwargs):
            touched.append(str(self_path))
            return real(self_path, *args, **kwargs)
        with mock.patch.object(Path, "resolve", spy):
            for bad in (r"\\serveur\partage\x", r"\\?\UNC\serveur\partage", "//serveur/partage", r"\dossier", "C:dossier"):
                with self.subTest(bad=bad), self.assertRaisesRegex(StoreError, "disque local"):
                    places.Places().folder(bad)
        self.assertEqual([p for p in touched if "serveur" in p or "dossier" in p], [])
        # The settings' own folders too (the allowed folders, the vault, the backups).
        from aihub import config
        with mock.patch.object(Path, "resolve", spy), mock.patch.object(Path, "is_dir", lambda p: touched.append(str(p)) or True):
            with self.assertRaisesRegex(config.ConfigError, "disque local"):
                config._folder(r"\\serveur\partage", "dossier autorisé")
        self.assertEqual([p for p in touched if "serveur" in p], [])


if TestClient is not None:
    class ExplorerRoutesTests(TempDirMixin, unittest.TestCase):
        """A server whose settings name an allowed folder: everything done with files keeps inside it."""

        def setUp(self):
            tmp = self.make_tempdir()
            self.db_path = str(tmp / "hub.db")
            self.allowed = self.make_tempdir()
            self.outside = self.make_tempdir()
            self.password = new_password()
            conn = db.connect(self.db_path)
            auth.set_password(conn, self.password)
            conn.close()
            self.env = fake_env("text", tmp / "log.json")
            self.env["FAKE_CLAUDE_LOGGED_IN"] = "1"
            self.app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, auth_policy=auth.Policy(),
                                  clock=Clock(), sse_ping_s=0.1, run_scheduler=False, allowed_roots=[self.allowed])
            self.owner = self.client()
            self.assertEqual(self.owner.post("/api/auth/login", json={"password": self.password}).status_code, 200)
            r = self.owner.post("/api/conversations", json={"title": "Site", "workdir": str(self.allowed)}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            self.cid = r.json()["conversation"]["id"]

        def client(self, address=("127.0.0.1", 50000)):
            client = TestClient(self.app, client=address)
            client.__enter__()
            self.addCleanup(client.__exit__, None, None, None)
            return client

        def member(self):
            r = self.owner.post("/api/users", json={"name": "Léa", "password": "", "role": "membre", "grants": []}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            lea = self.client(("127.0.0.1", 50200))
            self.assertEqual(lea.post("/api/auth/login", json={"name": "Léa", "password": ""}).status_code, 200)
            return lea

        def put(self, folder, name, text):
            path = folder / name
            path.write_text(text, encoding="utf-8")
            return str(path)

        def test_the_explorer_shows_files_of_the_kinds_asked(self):
            (self.allowed / "sous-dossier").mkdir()
            self.put(self.allowed, "a.md", "x")
            self.put(self.allowed, "b.exe", "x")
            r = self.owner.get("/api/filesystem/folders", params={"path": str(self.allowed), "files": "md,txt"}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([(e["name"], e["kind"]) for e in r.json()["entries"]], [("sous-dossier", "dir"), ("a.md", "file")])
            r = self.owner.get("/api/filesystem/folders", params={"path": str(self.allowed)}, headers=H)
            self.assertEqual([e["kind"] for e in r.json()["entries"]], ["dir"])  # folders only, unless asked
            r = self.owner.get("/api/filesystem/folders", params={"path": str(self.outside), "files": "*"}, headers=H)
            self.assertEqual(r.status_code, 400)
            r = self.owner.get("/api/filesystem/folders", params={"scope": "settings", "files": "md"}, headers=H)
            self.assertEqual(r.status_code, 400)  # the settings choose folders only

        def test_a_file_of_the_pc_into_the_projects_memory(self):
            pid = self.owner.post("/api/projects", json={"name": "Boutique"}, headers=H).json()["project"]["id"]
            path = self.put(self.allowed, "Décisions.md", "Le bleu nuit est retenu.\n")
            r = self.owner.post(f"/api/projects/{pid}/memory/file", json={"path": path}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["document"]["name"], "Décisions.md")
            for bad, said in ((self.put(self.outside, "x.md", "x"), "non autorisé"),
                              (self.put(self.allowed, "x.exe", "x"), "genre accepté")):
                with self.subTest(said=said):
                    r = self.owner.post(f"/api/projects/{pid}/memory/file", json={"path": bad}, headers=H)
                    self.assertEqual(r.status_code, 400)
                    self.assertIn(said, r.json()["detail"])
            latin = self.allowed / "ancien.txt"
            latin.write_bytes("é".encode("latin-1"))
            r = self.owner.post(f"/api/projects/{pid}/memory/file", json={"path": str(latin)}, headers=H)
            self.assertIn("UTF-8", r.json()["detail"])
            lea = self.member()
            own = lea.post("/api/projects", json={"name": "À Léa"}, headers=H).json()["project"]["id"]
            r = lea.post(f"/api/projects/{own}/memory/file", json={"path": path}, headers=H)
            self.assertEqual(r.status_code, 403)  # a member never reads the PC's files

        def test_a_file_of_the_pc_attached_to_the_next_request(self):
            path = self.put(self.allowed, "plan.txt", "étape 1\n")
            r = self.owner.post(f"/api/conversations/{self.cid}/attachments/pc", json={"path": path}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["name"], "plan.txt")
            r = self.owner.get(f"/api/attachments/{r.json()['id']}", headers=H)
            self.assertEqual(r.content, Path(path).read_bytes())
            r = self.owner.post(f"/api/conversations/{self.cid}/attachments/pc",
                                json={"path": self.put(self.outside, "y.txt", "y")}, headers=H)
            self.assertEqual(r.status_code, 400)

        def test_an_export_of_the_pc_imported(self):
            path = self.allowed / "conversations.json"
            path.write_text(json.dumps(claude_export()), encoding="utf-8")
            r = self.owner.post("/api/import/pc", json={"path": str(path)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([c["title"] for c in r.json()["conversations"]], ["Couleurs de la boutique"])
            other = self.outside / "conversations.json"
            other.write_text(json.dumps(claude_export()), encoding="utf-8")
            self.assertEqual(self.owner.post("/api/import/pc", json={"path": str(other)}, headers=H).status_code, 400)
            lea = self.member()
            self.assertEqual(lea.post("/api/import/pc", json={"path": str(path)}, headers=H).status_code, 403)

        def test_a_project_of_the_pc_imported(self):
            pid = self.owner.post("/api/projects", json={"name": "Boutique"}, headers=H).json()["project"]["id"]
            self.owner.patch(f"/api/conversations/{self.cid}", json={"project_id": pid}, headers=H)
            self.owner.post(f"/api/conversations/{self.cid}/messages", json={"prompt": "dis bonjour"}, headers=H)
            from tests.test_sharing import wait_idle
            wait_idle(self.owner, self.cid)
            data = self.owner.get(f"/api/projects/{pid}/export", headers=H).content
            path = self.allowed / "projet.zip"
            path.write_bytes(data)
            r = self.owner.post("/api/projects/import/pc", json={"path": str(path)}, params={"workdir": str(self.allowed)},
                                headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            self.assertEqual(r.json()["conversations"], 1)

        def test_a_conversation_saved_into_a_folder_of_the_pc(self):
            r = self.owner.post(f"/api/conversations/{self.cid}/save",
                                json={"folder": str(self.allowed), "password": self.password}, headers=H)
            self.assertEqual(r.status_code, 201, r.text)
            first = Path(r.json()["path"])
            self.assertEqual((first.parent, first.name), (self.allowed.resolve(), "Site.md"))
            self.assertIn("# Site", first.read_text(encoding="utf-8"))
            r = self.owner.post(f"/api/conversations/{self.cid}/save",
                                json={"folder": str(self.allowed), "format": "json", "password": self.password}, headers=H)
            self.assertEqual(Path(r.json()["path"]).name, "Site.json")
            r = self.owner.post(f"/api/conversations/{self.cid}/save",
                                json={"folder": str(self.allowed), "password": self.password}, headers=H)
            self.assertEqual(Path(r.json()["path"]).name, "Site (2).md")  # never over another file
            self.assertIn("# Site", first.read_text(encoding="utf-8"))
            # Outside the allowed folders, a wrong password, another format: nothing written.
            for body, code in (({"folder": str(self.outside), "password": self.password}, 400),
                               ({"folder": str(self.allowed), "password": "faux mot de passe"}, 401),
                               ({"folder": str(self.allowed), "format": "html", "password": self.password}, 400)):
                with self.subTest(body=body.get("format") or body["folder"][-12:]):
                    r = self.owner.post(f"/api/conversations/{self.cid}/save", json=body, headers=H)
                    self.assertEqual(r.status_code, code, r.text)
            self.assertEqual(sorted(p.name for p in self.allowed.iterdir()), ["Site (2).md", "Site.json", "Site.md"])
            self.assertEqual(list(self.outside.iterdir()), [])
            # A member never writes on the PC, not even their own conversation.
            lea = self.member()
            own = lea.post("/api/conversations", json={"title": "À Léa"}, headers=H).json()["conversation"]["id"]
            r = lea.post(f"/api/conversations/{own}/save", json={"folder": str(self.allowed)}, headers=H)
            self.assertEqual(r.status_code, 403)
            self.assertIn("réservé au propriétaire", r.json()["detail"])  # said for what it is

        def test_a_huge_folder_is_read_in_part(self):
            from aihub import browse
            for n in range(6):
                (self.allowed / f"dossier {n}").mkdir()
            with mock.patch.object(browse, "MAX_SCAN", 3):
                data = self.owner.get("/api/filesystem/folders", params={"path": str(self.allowed)}, headers=H).json()
            self.assertEqual((len(data["entries"]), data["complete"]), (3, False))

        def test_vaults_keep_inside_the_allowed_folders(self):
            outside_vault = self.outside / "coffre"
            (outside_vault / ".obsidian").mkdir(parents=True)
            inside_vault = self.allowed / "coffre"
            (inside_vault / ".obsidian").mkdir(parents=True)
            r = self.owner.patch(f"/api/conversations/{self.cid}", json={"obsidian_vault": str(outside_vault)}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("non autorisé", r.json()["detail"])
            r = self.owner.post("/api/projects", json={"name": "P", "obsidian_vault": str(outside_vault)}, headers=H)
            self.assertEqual(r.status_code, 400)
            r = self.owner.patch(f"/api/conversations/{self.cid}", json={"obsidian_vault": str(inside_vault)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            # Reading a vault: inside the allowed folders, or one AI Hub writes to.
            r = self.owner.get("/api/obsidian/vault/notes", params={"vault": str(outside_vault)}, headers=H)
            self.assertEqual(r.status_code, 400)
            r = self.owner.get("/api/obsidian/vault/notes", params={"vault": str(inside_vault)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            self.app.state.vault_hooks = {"lists": [], "roots": [(self.outside, 2), (self.allowed, 2)]}  # the disks walked
            found = {Path(v["path"]).resolve(): v["allowed"]
                     for v in self.owner.get("/api/obsidian/vaults?refresh=true", headers=H).json()["vaults"]}
            self.assertEqual((found.get(outside_vault.resolve()), found.get(inside_vault.resolve())), (False, True), found)

        @unittest.skipUnless(os.name == "nt", "chemins réseau de Windows")
        def test_a_network_working_folder_is_refused_before_it_is_resolved(self):
            touched = []
            real = Path.resolve

            def spy(self_path, *args, **kwargs):
                touched.append(str(self_path))
                return real(self_path, *args, **kwargs)
            with mock.patch.object(Path, "resolve", spy):
                r = self.owner.post("/api/conversations", json={"title": "Réseau", "workdir": r"\\serveur\partage"}, headers=H)
            self.assertEqual(r.status_code, 400)
            self.assertIn("disque local", r.json()["detail"])
            self.assertEqual([p for p in touched if "serveur" in p], [])


if __name__ == "__main__":
    unittest.main()
