"""Tools chosen per conversation: the catalog, the vault server, what each run gets."""

import json
import unittest

from aihub import config, db, store, tools, vault_mcp
from aihub.providers import claude_code, codex
from aihub.runner import Request

from .helpers import FAKE_COMMAND, TempDirMixin, fake_env, read_log


class CatalogTest(unittest.TestCase):
    def test_valid_and_refused(self):
        ok = tools.check_catalog([{"name": "fichiers", "command": " npx ", "args": ["-y", "srv"],
                                   "read_only": True}])
        self.assertEqual(ok, [{"name": "fichiers", "command": "npx", "args": ["-y", "srv"], "description": "",
                               "read_only": True}])
        for bad in ([{"name": "Web", "command": "x"}], [{"name": "web", "command": "x"}],
                    [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}],
                    [{"name": "a", "command": ""}], [{"name": "a", "command": "x", "env": {"KEY": "s"}}],
                    [{"name": "a", "command": "x", "args": "-y"}], [{"name": "a", "command": "x", "read_only": "oui"}],
                    {"name": "a"}):
            with self.subTest(bad=bad), self.assertRaises(tools.ToolError):
                tools.check_catalog(bad)
        with self.assertRaisesRegex(config.ConfigError, "variables"):
            config.validate({"tool_servers": [{"name": "a", "command": "x", "env": {}}]})


class VaultTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.root = self.make_tempdir()
        (self.root / "Projets").mkdir()
        (self.root / "Projets" / "Site.md").write_text("# Site\nLe logo est vert sapin.", encoding="utf-8")
        (self.root / "Idées.md").write_text("Une décision prise.", encoding="utf-8")
        (self.root / ".obsidian").mkdir()
        (self.root / ".obsidian" / "secret.md").write_text("caché", encoding="utf-8")
        (self.root / "image.png").write_bytes(b"\x89PNG")
        self.vault = vault_mcp.Vault(self.root)

    def test_list_read_search(self):
        listed = self.vault.list_notes()
        self.assertIn("Projets/Site.md", listed)
        self.assertNotIn("secret", listed)
        self.assertIn("vert sapin", self.vault.read_note("Projets/Site.md"))
        self.assertIn("Projets/Site.md", self.vault.search_notes("logo sapin"))
        self.assertIn("Idées.md", self.vault.search_notes("decision"))  # accents folded
        self.assertIn("Rien trouvé", self.vault.search_notes("girafe"))

    def test_nothing_outside_the_vault(self):
        outside = self.root.parent / "dehors.md"
        for path in ("../dehors.md", str(outside), ".obsidian/secret.md", "image.png"):
            with self.subTest(path=path), self.assertRaises(vault_mcp.Refused):
                self.vault.read_note(path)
        with self.assertRaises(vault_mcp.Refused):
            self.vault.list_notes("..")

    def test_protocol(self):
        reply = vault_mcp.handle(self.vault, {"id": 1, "method": "tools/call",
                                              "params": {"name": "read_note", "arguments": {"path": "../x.md"}}})
        self.assertTrue(reply["result"]["isError"])
        names = [t["name"] for t in vault_mcp.handle(self.vault, {"id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, vault_mcp.TOOL_NAMES)


class RunToolsTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(self.conn.close)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "c", agent_id=self.agent, workdir=str(self.tmp))
        self.settings = config.validate({"obsidian_vault": str(self.tmp), "tool_servers": [
            {"name": "lecteur", "command": "lire", "read_only": True}, {"name": "ecrivain", "command": "ecrire"}]})

    def conv(self):
        return store.get_conversation(self.conn, self.cid)

    def test_default_is_the_web_alone(self):
        given = tools.for_run(self.conn, self.conv(), self.settings)
        self.assertEqual((given.mcp_config, given.auto_allow, given.web), (None, [], True))

    def test_chosen_tools(self):
        store.set_tools(self.conn, self.cid, tools.check_choice(["ecrivain", "coffre", "lecteur"], self.settings))
        self.assertEqual(tools.chosen(self.conv()), ["coffre", "lecteur", "ecrivain"])  # the catalog's order
        given = tools.for_run(self.conn, self.conv(), self.settings)
        self.assertEqual(sorted(given.mcp_config["mcpServers"]), ["coffre", "ecrivain", "lecteur"])
        self.assertIn("mcp__coffre__read_note", given.auto_allow)
        self.assertIn("mcp__lecteur", given.auto_allow)       # read only: the whole server
        self.assertNotIn("mcp__ecrivain", given.auto_allow)   # the others ask
        self.assertFalse(given.web)
        with self.assertRaisesRegex(tools.ToolError, "non proposé"):
            tools.check_choice(["inconnu"], self.settings)

    def test_a_server_taken_out_of_the_settings_is_not_given(self):
        store.set_tools(self.conn, self.cid, ["lecteur"])
        given = tools.for_run(self.conn, self.conv(), config.Settings())
        self.assertIsNone(given.mcp_config)

    def test_allowed_without_asking(self):
        self.assertTrue(claude_code._allowed("mcp__lecteur__lire", ["mcp__lecteur"]))
        self.assertFalse(claude_code._allowed("mcp__lecteur2__lire", ["mcp__lecteur"]))
        self.assertFalse(claude_code._allowed("Bash", ["mcp__lecteur"]))

    def test_web_off_for_both_providers(self):
        run = claude_code.ClaudeCodeRun(["c"], prompt="x", workdir=str(self.tmp), permission_mode="plan",
                                        disallowed=claude_code.WEB_TOOLS)
        args = run.arguments()
        self.assertEqual(args[args.index("--disallowedTools") + 1], "WebSearch,WebFetch")
        params = codex.CodexRun(["c"], prompt="x", workdir=str(self.tmp), permission_mode="plan", web=False).thread_params()
        self.assertEqual(params["config"]["web_search"], "disabled")
        self.assertNotIn("config", codex.CodexRun(["c"], prompt="x", workdir=str(self.tmp),
                                                  permission_mode="plan").thread_params())

    def test_a_request_gets_them(self):
        store.set_tools(self.conn, self.cid, ["coffre"])
        log = self.tmp / "log.json"
        Request(self.conn, self.cid, self.agent, "bonjour", command=FAKE_COMMAND, env=fake_env("text", log),
                settings=self.settings).run()
        args = read_log(log)["args"]
        self.assertIn("coffre", json.loads(args[args.index("--mcp-config") + 1])["mcpServers"])
        self.assertEqual(args[args.index("--disallowedTools") + 1], "WebSearch,WebFetch")
        self.assertIn("mcp__coffre__search_notes", args[args.index("--allowedTools") + 1])


if __name__ == "__main__":
    unittest.main()
