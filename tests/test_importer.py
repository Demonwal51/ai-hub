"""Conversations imported from ChatGPT, Claude, Claude Code and Codex, alone or merged."""

import io
import json
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from aihub import db, importer, store
from aihub.web.app import create_app

from .helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log
from .test_sharing import SharingTestCase

H = {"X-AIHub": "1"}


def chatgpt_export():
    """The shape of chatgpt.com's conversations.json: a tree of messages, the shown branch ends at current_node."""
    def node(nid, parent, role, text, kind="text", hidden=False):
        message = {"id": nid, "author": {"role": role}, "create_time": 1760000000 + len(nid),
                   "content": {"content_type": kind, "parts": [text] if isinstance(text, str) else text},
                   "metadata": {"is_visually_hidden_from_conversation": hidden}}
        return nid, {"id": nid, "message": message, "parent": parent, "children": []}
    mapping = dict([
        ("root", {"id": "root", "message": None, "parent": None, "children": ["s"]}),
        node("s", "root", "user", "instructions personnalisées, cachées", hidden=True),
        node("u1", "s", "user", "Quel budget pour le logo ?"),
        node("a1", "u1", "assistant", "Environ 900 euros."),
        node("a1b", "u1", "assistant", "Une autre branche, pas affichée."),
        node("t1", "a1", "tool", "résultat d'un outil"),
        node("u2", "t1", "user", ["Et pour le site ?", {"asset_pointer": "image"}], kind="multimodal_text"),
        node("c2", "u2", "assistant", "print(1)", kind="code"),
        node("a2", "c2", "assistant", "Compte 3 000 euros."),
    ])
    return [{"title": "Budget du logo", "create_time": 1760000000.5, "mapping": mapping, "current_node": "a2"},
            {"title": "Vide", "create_time": 1750000000, "mapping": {}, "current_node": None}]


def claude_export():
    return [{"uuid": "c1", "name": "Couleurs de la boutique", "created_at": "2026-09-01T10:00:00.000000Z",
             "chat_messages": [
                 {"sender": "human", "text": "Quelles couleurs ?", "content": [{"type": "text", "text": "Quelles couleurs ?"}],
                  "created_at": "2026-09-01T10:00:01Z"},
                 {"sender": "assistant", "text": "", "created_at": "2026-09-01T10:00:05Z",
                  "content": [{"type": "text", "text": "Bleu nuit"}, {"type": "tool_use", "name": "x"},
                              {"type": "text", "text": "et sable."}]}]}]


def zipped(name, items):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(name, json.dumps(items))
        archive.writestr("user.json", "{}")
    return data.getvalue()


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def local_home(root, workdir):
    """A home folder with one Claude Code session and two Codex sessions (one is a sub-agent's)."""
    write_jsonl(root / ".claude" / "projects" / "D--site" / "s1.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
         "timestamp": "2026-10-01T08:00:00Z", "cwd": str(workdir)},
        {"type": "user", "message": {"role": "user", "content": "Corrige le menu du site"},
         "timestamp": "2026-10-01T08:00:01Z", "cwd": str(workdir)},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "…"},
                                                                          {"type": "text", "text": "Je regarde."}]},
         "timestamp": "2026-10-01T08:00:02Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read"}]},
         "timestamp": "2026-10-01T08:00:03Z"},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "fichier"}]},
         "timestamp": "2026-10-01T08:00:04Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "C'est corrigé."}]},
         "timestamp": "2026-10-01T08:00:05Z"},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "tâche d'un sous-agent"}},
        {"type": "ai-title", "aiTitle": "Menu du site"},
    ])
    write_jsonl(root / ".codex" / "sessions" / "2026" / "10" / "02" / "rollout-a.jsonl", [
        {"type": "session_meta", "payload": {"timestamp": "2026-10-02T09:00:00Z", "cwd": str(workdir), "source": "cli"}},
        {"type": "response_item", "timestamp": "2026-10-02T09:00:01Z",
         "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "règles"}]}},
        {"type": "response_item", "timestamp": "2026-10-02T09:00:01Z",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
             {"type": "input_text", "text": "Ajoute un test"}]}},
        {"type": "response_item", "timestamp": "2026-10-02T09:00:02Z",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
        {"type": "response_item", "timestamp": "2026-10-02T09:00:03Z",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Test ajouté."}]}},
    ])
    write_jsonl(root / ".codex" / "sessions" / "2026" / "10" / "02" / "rollout-b.jsonl", [
        {"type": "session_meta", "payload": {"source": {"subagent": {"kind": "review"}}}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "sous-agent"}]}},
    ])


class ParseTest(TempDirMixin, unittest.TestCase):
    def test_chatgpt_keeps_the_shown_branch_and_the_text_only(self):
        found = importer.parse_export(zipped("conversations.json", chatgpt_export()))
        self.assertEqual([c.title for c in found], ["Budget du logo"])  # the empty one is left out
        self.assertEqual([(r, t) for r, t, _ in found[0].messages], [
            ("user", "Quel budget pour le logo ?"), ("assistant", "Environ 900 euros."),
            ("user", "Et pour le site ?"), ("assistant", "Compte 3 000 euros.")])
        self.assertEqual(found[0].source, "chatgpt")
        self.assertTrue(found[0].created_at.startswith("2025-10-09"))

    def test_claude_and_a_bare_json(self):
        found = importer.parse_export(json.dumps(claude_export()).encode())
        self.assertEqual([(r, t) for r, t, _ in found[0].messages],
                         [("user", "Quelles couleurs ?"), ("assistant", "Bleu nuit\n\net sable.")])
        self.assertEqual((found[0].title, found[0].source), ("Couleurs de la boutique", "claude"))

    def test_what_is_not_an_export_is_said_so(self):
        for data, said in ((b"", "vide"), (b"PK\x03\x04pas un zip", "illisible"),
                           (zipped("autre.json", []), "pas de conversations.json"), (b"{pas du json", "illisible"),
                           (json.dumps([{"x": 1}]).encode(), "aucune conversation")):
            with self.subTest(said=said), self.assertRaisesRegex(importer.ImportFailed, said):
                importer.parse_export(data)

    def test_local_sessions_without_tools_nor_injected_context(self):
        home, workdir = self.make_tempdir(), self.make_tempdir()
        local_home(home, workdir)
        found = {c.source: c for c in importer.local_sessions(home)}
        self.assertEqual(sorted(found), ["claude_code", "codex"])  # the sub-agent's session is left out
        cc = found["claude_code"]
        self.assertEqual((cc.title, cc.workdir), ("Menu du site", str(workdir)))
        self.assertEqual([(r, t) for r, t, _ in cc.messages],
                         [("user", "Corrige le menu du site"), ("assistant", "Je regarde.\n\nC'est corrigé.")])
        self.assertEqual([(r, t) for r, t, _ in found["codex"].messages],
                         [("user", "Ajoute un test"), ("assistant", "Test ajouté.")])
        self.assertEqual(found["codex"].title, "Ajoute un test")


class WebImportTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        tmp = self.make_tempdir()
        self.db_path = str(tmp / "hub.db")
        self.workdir = self.make_tempdir()
        self.home = self.make_tempdir()
        local_home(self.home, self.workdir)
        self.log = tmp / "log.json"
        self.env = fake_env("text", self.log)
        self.app = create_app(self.db_path, command=FAKE_COMMAND, env=self.env, sse_ping_s=0.1,
                              run_scheduler=False, local_home=self.home)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def read(self, data):
        r = self.client.post("/api/import/file", content=data, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def messages(self, cid):
        return self.client.get(f"/api/conversations/{cid}", headers=H).json()["messages"]

    def test_one_conversation_imported_as_it_was_then_continued_from_memory(self):
        reading = self.read(zipped("conversations.json", chatgpt_export()))
        listed = reading["conversations"]
        self.assertEqual((listed[0]["title"], listed[0]["count"], listed[0]["source_name"]), ("Budget du logo", 4, "ChatGPT"))
        self.assertNotIn("messages", listed[0])  # the page gets the list, not the texts
        r = self.client.post("/api/import", json={"reading": reading["reading"], "keys": [listed[0]["key"]],
                                                  "workdir": str(self.workdir)}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        cid = r.json()["conversations"][0]
        msgs = self.messages(cid)
        self.assertEqual([(m["role"], m["content"]) for m in msgs][:2],
                         [("user", "Quel budget pour le logo ?"), ("assistant", "Environ 900 euros.")])
        self.assertEqual(msgs[1]["model"], "ChatGPT (importé)")
        self.assertEqual(self.client.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]["title"],
                         "Budget du logo")
        # Searchable like any other conversation.
        found = self.client.get("/api/search", params={"q": "logo"}, headers=H).json()
        self.assertIn(cid, {f["conversation_id"] for f in found.get("results", found.get("found", []))})
        # The first request carries the imported memory (no session to resume).
        self.client.post(f"/api/conversations/{cid}/messages", json={"prompt": "Et au total ?"}, headers=H)
        self._wait(cid)
        sent = read_log(self.log)
        self.assertIn("900 euros", sent.get("memory", ""))
        self.assertNotIn("--resume", sent["args"])

    def test_merged_into_one_new_conversation(self):
        export = self.read(zipped("conversations.json", chatgpt_export()))
        local = self.client.get("/api/import/local", headers=H).json()
        keys = [c["key"] for c in local["conversations"]]
        r = self.client.post("/api/import", json={"reading": local["reading"], "keys": keys, "merge": True,
                                                  "title": "Tout le site"}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        [cid] = r.json()["conversations"]
        conv = self.client.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]
        self.assertEqual((conv["title"], conv["workdir"]), ("Tout le site", str(self.workdir.resolve())))
        contents = [m["content"] for m in self.messages(cid)]
        self.assertEqual(len(contents), 4)
        # Each part headed by where it comes from, in the order they began.
        self.assertTrue(contents[0].startswith("[Importé de Claude Code — « Menu du site » — 2026-10-01]"))
        self.assertTrue(contents[2].startswith("[Importé de Codex — « Ajoute un test » — 2026-10-02]"))
        # A reading of another kind is its own: its keys do not mix.
        r = self.client.post("/api/import", json={"reading": export["reading"], "keys": keys, "merge": True}, headers=H)
        self.assertIn("cochez", r.json()["detail"])

    def test_refusals(self):
        r = self.client.post("/api/import", json={"reading": "inconnue", "keys": ["x"]}, headers=H)
        self.assertIn("expirée", r.json()["detail"])
        r = self.client.post("/api/import/file", content=b"pas un export", headers=H)
        self.assertEqual(r.status_code, 400)
        reading = self.read(json.dumps(claude_export()).encode())
        r = self.client.post("/api/import", json={"reading": reading["reading"], "keys": [reading["conversations"][0]["key"]]},
                             headers=H)
        self.assertIn("dossier de travail", r.json()["detail"])  # an export has no folder of its own

    def test_never_the_mode_without_permission(self):
        reading = self.read(json.dumps(claude_export()).encode())
        r = self.client.post("/api/import", json={"reading": reading["reading"], "keys": [reading["conversations"][0]["key"]],
                                                  "workdir": str(self.workdir), "permission_mode": "bypass"}, headers=H)
        cid = r.json()["conversations"][0]
        conv = self.client.get(f"/api/conversations/{cid}", headers=H).json()["conversation"]
        self.assertEqual(conv["permission_mode"], "manual")  # confirmed in the conversation, never by an import


    def _wait(self, cid, timeout=20):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.client.get(f"/api/conversations/{cid}", headers=H).json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("toujours en cours")


class GuestImportTest(SharingTestCase):
    """An import is the owner's: the sessions of this PC are theirs, and a guest creates no conversation."""

    def test_a_guest_reads_nothing_and_imports_nothing(self):
        guest, _ = self.guest()
        export = json.dumps(claude_export()).encode()
        self.assertEqual(guest.get("/api/import/local", headers=H).status_code, 403)
        self.assertEqual(guest.post("/api/import/file", content=export, headers=H).status_code, 403)
        reading = self.owner.post("/api/import/file", content=export, headers=H).json()
        r = guest.post("/api/import", json={"reading": reading["reading"], "keys": [reading["conversations"][0]["key"]],
                                            "workdir": str(self.workdir)}, headers=H)
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
