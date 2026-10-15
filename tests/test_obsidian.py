import shutil
import unittest
from pathlib import Path

from aihub import obsidian
from tests.helpers import TempDirMixin, wait_for_log_key

try:
    from tests.test_web import WebTestCase
except unittest.SkipTest:  # web dependencies missing
    WebTestCase = None


class NoteFileTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.vault = self.make_tempdir() / "coffre"  # see ObsidianWebTests.setUp
        self.vault.mkdir()

    def test_paths_stay_in_the_vault(self):
        for bad, fragment in (("../dehors.md", "rester dans le coffre"),
                              ("Projets/../../dehors.md", "rester dans le coffre"),
                              (str(self.vault / "absolu.md"), "relatif"),
                              ("/racine.md", "relatif"),
                              ("note.txt", ".md"),
                              ("  ", "vide")):
            with self.subTest(bad=bad), self.assertRaisesRegex(obsidian.ObsidianError, fragment):
                obsidian.resolve(self.vault, bad)
        self.assertEqual(obsidian.resolve(self.vault, "Projets AI Hub/a.md"),
                         (self.vault / "Projets AI Hub" / "a.md").resolve())
        with self.assertRaisesRegex(obsidian.ObsidianError, "introuvable"):
            obsidian.resolve(self.vault / "absent", "a.md")
        with self.assertRaisesRegex(obsidian.ObsidianError, "aucun coffre"):
            obsidian.resolve(None, "a.md")

    def test_default_note_path(self):
        self.assertEqual(obsidian.default_note_path('Site : v2/final?'), "Projets AI Hub/Site - v2-final-.md")
        self.assertEqual(obsidian.default_note_path(" . "), "Projets AI Hub/Projet.md")

    def test_append_keeps_what_the_user_wrote(self):
        path = obsidian.append(self.vault, "Projets AI Hub/p.md", "Mon projet", "## Création\n\nici")
        self.assertTrue(path.read_text(encoding="utf-8").startswith("# Mon projet\n\nNote de projet tenue par AI Hub"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\nMa remarque à moi.\n")
        obsidian.append(self.vault, "Projets AI Hub/p.md", "Mon projet", "## Mise à jour\n\nlà")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("# Mon projet"), 1)
        self.assertLess(text.index("## Création"), text.index("Ma remarque à moi."))
        self.assertLess(text.index("Ma remarque à moi."), text.index("## Mise à jour"))
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))  # no BOM for Obsidian

    def test_rewrite_backs_up_then_replaces(self):
        backups = self.make_tempdir()
        path, backup = obsidian.rewrite(self.vault, "Projets AI Hub/p.md", "P", ["## Création"], backups)
        self.assertIsNone(backup)  # nothing to back up the first time
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\nRemarque perso\n")
        path, backup = obsidian.rewrite(self.vault, "Projets AI Hub/p.md", "P",
                                        ["## Création", "## Mise à jour — 1"], backups)
        self.assertIn("Remarque perso", backup.read_text(encoding="utf-8"))
        self.assertEqual(backup.parent, backups)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("Remarque perso", text)
        self.assertIn("## Mise à jour — 1", text)
        self.assertEqual(list(path.parent.glob("*.aihub-tmp")), [])

    def test_plan_section(self):
        plan = {"status": "error", "summary": "deux étapes", "subtasks": [
            {"position": 1, "title": "A | B", "description": "d", "agent_id": 1, "model": "opus",
             "status": "ok", "files": ["a.py"], "error": None},
            {"position": 2, "title": None, "description": "deuxième", "agent_id": None, "model": None,
             "status": "error", "files": [], "error": "quota atteint"}]}
        text = obsidian.plan_section(plan, "fais-le", "Tout va bien sauf B.", {1: "Claude Code (opus)"})
        self.assertIn("plan terminé avec des échecs", text)
        self.assertIn("| 1 | A / B | Claude Code (opus) | réussie | `a.py` |", text)
        plan["subtasks"][0]["model"] = "sonnet"  # the user chose another model for this subtask
        self.assertIn("| Claude Code (opus), IA sonnet |", obsidian.plan_section(plan, "x", None, {1: "Claude Code (opus)"}))
        self.assertIn("| 2 | deuxième | — | ÉCHEC | — |", text)
        self.assertIn("- Sous-tâche 2 : quota atteint", text)
        self.assertIn("**Synthèse du chef :**\n\nTout va bien sauf B.", text)


if WebTestCase is not None:
    class ObsidianWebTests(WebTestCase):
        scenario = "auto"

        def setUp(self):
            # The vault sits inside a folder the test owns: a note that escapes it
            # (a broken guard) lands there and is cleaned, never in the shared %TEMP%.
            self.vault = self.make_tempdir() / "coffre"
            self.vault.mkdir()
            self.app_kwargs = {"obsidian_vault": str(self.vault)}
            super().setUp()
            self.env["FAKE_CLAUDE_LOG_DIR"] = str(self.make_tempdir())

        def note(self, cid):
            path = self.vault / self.conversation(cid)["conversation"]["obsidian_note"]
            return path.read_text(encoding="utf-8")

        def orchestration(self, **kwargs):
            return self.new_conversation(title="Mon site", mode="orchestration", conductor_model="opus",
                                         assignment_mode="auto", **kwargs)

        def run_plan(self, cid, prompt="fais le site"):
            self.send(cid, prompt)
            plan = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{plan['id']}/launch")
            return self.wait_idle(cid)

        def test_options(self):
            self.assertEqual(self.client.get("/api/options").json()["obsidian"],
                             {"enabled": True, "vault": str(self.vault.resolve())})

        def test_turning_the_note_on_writes_the_creation(self):
            cid = self.orchestration()
            r = self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.assertEqual(r.json()["conversation"]["obsidian_note"], "Projets AI Hub/Mon site.md")
            text = self.note(cid)
            self.assertIn("## Création — ", text)
            self.assertIn("- Choix des IA : automatique, selon les benchmarks", text)
            self.assertIn("- Chef d'orchestre : opus", text)
            self.assertIn("- Mode de permission : manuel", text)
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.assertEqual(self.note(cid).count("## Création"), 1)  # not twice

        def test_note_outside_the_vault_is_refused(self):
            cid = self.orchestration()
            r = self.client.patch(f"/api/conversations/{cid}",
                                  json={"obsidian": True, "obsidian_note": "../dehors.md"})
            self.assertEqual(r.status_code, 400)
            self.assertIsNone(self.conversation(cid)["conversation"]["obsidian_note"])
            self.assertFalse((self.vault.parent / "dehors.md").exists())

        def test_executed_plan_is_noted_but_not_the_conversation(self):
            cid = self.orchestration()
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.client.post(f"/api/conversations/{cid}/messages",
                             json={"prompt": "question secrète au chef", "kind": "discuss"})
            self.wait_idle(cid)
            data = self.run_plan(cid)
            self.assertEqual(data["plans"][-1]["status"], "done")
            text = self.note(cid)
            self.assertIn("## Mise à jour — ", text)
            self.assertIn("— plan terminé", text)
            self.assertIn("**Demande :** fais le site", text)
            self.assertIn("| 1 | Partie A | Claude Code (opus) | réussie | `a.txt` |", text)
            self.assertIn("Synthèse : 3 réussie(s), 0 en échec.", text)
            # The discussion is a conversation: it stays out of the note.
            self.assertNotIn("question secrète", text)
            self.assertNotIn("Le chef répond", text)

        def test_note_off_means_nothing_written(self):
            cid = self.orchestration()
            self.run_plan(cid)
            self.assertEqual(list(self.vault.rglob("*.md")), [])
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": False})
            self.run_plan(cid, "deuxième")
            note = next(self.vault.rglob("*.md")).read_text(encoding="utf-8")
            self.assertNotIn("## Mise à jour", note)

        def test_a_reply_noted_by_hand(self):
            cid = self.new_conversation(title="Simple")
            self.send(cid, "dis bonjour")
            reply = self.wait_idle(cid)["messages"][-1]
            r = self.client.post(f"/api/messages/{reply['id']}/obsidian")
            self.assertEqual(r.status_code, 400)
            self.assertIn("n'est pas activée", r.json()["detail"])  # the note is off
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/obsidian").status_code, 200)
            self.assertIn("**Demande :** dis bonjour\n\nBonjour", self.note(cid))
            user = self.conversation(cid)["messages"][0]
            self.assertEqual(self.client.post(f"/api/messages/{user['id']}/obsidian").status_code, 400)
            # The hub remembers the reply was noted: a rewrite puts it back.
            self.assertIsNotNone(self.conversation(cid)["messages"][-1]["noted_at"])
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 200)
            self.assertIn("**Demande :** dis bonjour\n\nBonjour", self.note(cid))

        def test_a_reply_goes_to_the_note_once(self):
            cid = self.new_conversation(title="Simple")
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.send(cid, "dis bonjour")
            reply = self.wait_idle(cid)["messages"][-1]
            self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/obsidian").status_code, 200)
            r = self.client.post(f"/api/messages/{reply['id']}/obsidian")
            self.assertEqual(r.status_code, 400)
            self.assertIn("déjà dans la note", r.json()["detail"])
            self.assertEqual(self.note(cid).count("**Demande :** dis bonjour"), 1)

        def test_a_plans_synthesis_is_in_the_note_once(self):
            # 13/10/2026 : the synthesis, already written with its plan, was noted
            # again by hand, and a rewrite would have copied it twice as well.
            cid = self.orchestration()
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            plan = self.run_plan(cid)["plans"][-1]
            synthesis = next(m for m in self.conversation(cid)["messages"] if m["id"] == plan["synthesis_message_id"])
            self.assertIsNotNone(synthesis["noted_at"])  # its button says it is in the note
            r = self.client.post(f"/api/messages/{synthesis['id']}/obsidian")
            self.assertEqual(r.status_code, 400)
            self.assertEqual(self.note(cid).count("Synthèse : 3 réussie(s)"), 1)
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 200)
            self.assertEqual(self.note(cid).count("Synthèse : 3 réussie(s)"), 1)

        def test_a_synthesis_from_before_the_note_is_refused_by_hand(self):
            cid = self.orchestration()
            plan = self.run_plan(cid)["plans"][-1]  # the note was off: no plan section
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            r = self.client.post(f"/api/messages/{plan['synthesis_message_id']}/obsidian")
            self.assertEqual(r.status_code, 400)
            self.assertIn("avec son plan", r.json()["detail"])
            # A rewrite is the way to bring it in: once, with its plan.
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 200)
            text = self.note(cid)
            self.assertEqual(text.count("Synthèse : 3 réussie(s)"), 1)
            self.assertIn("— plan terminé", text)

        def test_no_rewrite_while_a_request_runs(self):
            cid = self.new_conversation(title="Long")
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.send(cid, "attends")  # the fake CLI hangs on this word
            wait_for_log_key(self.log, "child_pid")  # stopping it earlier races its start
            self.assertTrue(self.conversation(cid)["running"])
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 409)
            self.client.post(f"/api/conversations/{cid}/stop")
            self.wait_idle(cid)

        def test_rewrite_on_request(self):
            cid = self.orchestration()
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.run_plan(cid, "premier plan")
            # A cancelled plan never ran: it does not belong in the note.
            self.send(cid, "plan annulé")
            cancelled = self.wait_idle(cid)["plans"][-1]
            self.client.post(f"/api/plans/{cancelled['id']}/cancel")
            path = self.vault / self.conversation(cid)["conversation"]["obsidian_note"]
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\nMes notes à moi\n")
            r = self.client.post(f"/api/conversations/{cid}/obsidian/rewrite")
            self.assertEqual(r.status_code, 200)
            backup = r.json()["backup"]
            self.assertIn("Mes notes à moi", Path(backup).read_text(encoding="utf-8"))
            self.assertIn("obsidian-sauvegardes", backup)
            # Outside the vault, so Obsidian does not show the old copies as notes.
            self.assertNotIn(self.vault.resolve(), Path(backup).resolve().parents)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Mes notes à moi", text)
            self.assertEqual(text.count("## Création"), 1)
            self.assertEqual(text.count("## Mise à jour"), 1)
            self.assertIn("**Demande :** premier plan", text)
            self.assertNotIn("plan annulé", text)
            self.assertLess(text.index("## Création"), text.index("## Mise à jour"))

        def test_rewrite_needs_the_note_on(self):
            cid = self.orchestration()
            self.assertEqual(self.client.post(f"/api/conversations/{cid}/obsidian/rewrite").status_code, 400)

        def test_unwritable_note_never_fails_the_plan(self):
            cid = self.orchestration()
            self.client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            shutil.rmtree(self.vault)
            data = self.run_plan(cid)
            self.assertEqual(data["plans"][-1]["status"], "done")
            error = self.app.state.jobs.note_plan(data["plans"][-1]["id"])
            self.assertIn("coffre Obsidian introuvable", error)


class NoVaultTests(TempDirMixin, unittest.TestCase):
    def test_without_a_vault_notes_are_not_offered(self):
        if WebTestCase is None:
            self.skipTest("web dependencies missing")
        from fastapi.testclient import TestClient
        from aihub.web.app import create_app
        from tests.helpers import FAKE_COMMAND
        app = create_app(str(self.make_tempdir() / "hub.db"), command=FAKE_COMMAND)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/options").json()["obsidian"], {"enabled": False, "vault": None})
            cid = client.post("/api/conversations", json={"title": "t", "workdir": str(self.make_tempdir())}
                              ).json()["conversation"]["id"]
            r = client.patch(f"/api/conversations/{cid}", json={"obsidian": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("aucun coffre", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
