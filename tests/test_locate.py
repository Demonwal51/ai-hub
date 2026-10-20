"""Version 1.3.0: the programs searched for at each use, and « Chercher sur ce PC » (aihub/locate.py)."""

import itertools
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from aihub import config, locate, providers
from aihub.providers import claude_code, codex
from aihub.providers.claude_code import ClaudeCodeRun, ProviderError

from .helpers import FAKE_CLI, FAKE_CODEX, FAKE_COMMAND, TempDirMixin, fake_env

try:
    from fastapi.testclient import TestClient
    from aihub.web.app import _status_hint, create_app
except ImportError:  # web dependencies missing
    TestClient = None

DAY = 86400


def fake_which(name, path=None):
    """shutil.which as on Windows: each folder of the PATH in turn, `name` then name.exe, name.cmd."""
    for folder in (path or "").split(os.pathsep):
        for candidate in ([name] if "." in name else [name + ".exe", name + ".cmd"]):
            if folder and (Path(folder) / candidate).is_file():
                return str(Path(folder) / candidate)
    return None


def wrapper(path):
    """A one-file Claude Code (claude.cmd, or a shell script) that runs the fake CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(f'@"{sys.executable}" -B "{FAKE_CLI}" %*\r\n', encoding="utf-8")
    else:
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "{FAKE_CLI}" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return path


class PC(TempDirMixin, unittest.TestCase):
    """A PC of its own: its home, AppData, PATH and / are folders of the test."""

    system = "nt"
    windows_which = True  # shutil.which as on Windows, whatever the system running the tests

    def setUp(self):
        self.tmp = self.make_tempdir()
        self.home = self.tmp / "home"
        self.appdata = self.home / "AppData" / "Roaming"
        self.local = self.home / "AppData" / "Local"
        self.root = self.tmp / "root"
        self.env = {"PATH": "", "APPDATA": str(self.appdata), "LOCALAPPDATA": str(self.local),
                    "USERPROFILE": str(self.home), "HOME": str(self.home)}
        if self.windows_which:
            patcher = mock.patch("shutil.which", side_effect=fake_which)
            patcher.start()
            self.addCleanup(patcher.stop)

    def where(self, **extra):
        return dict(env=self.env, home=self.home, root=self.root, system=self.system, **extra)

    def file(self, path, age_days=0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
        when = time.time() - age_days * DAY
        os.utime(path, (when, when))
        return path

    def find(self, program, **kwargs):
        return locate.find(program, **self.where(**kwargs))

    def app_copy(self, name, age_days=0):
        return self.file(self.local / "OpenAI" / "Codex" / "bin" / name / "codex.exe", age_days)


class FindTests(PC):
    def test_a_native_exe_on_the_path_before_a_cmd(self):
        shim = self.file(self.tmp / "npm" / "codex.cmd")
        native = self.file(self.tmp / "outils" / "codex.exe")
        self.env["PATH"] = os.pathsep.join([str(shim.parent), str(native.parent)])  # the .cmd comes first
        found = self.find("codex")
        self.assertEqual((Path(found.path), found.origin, found.where), (native, "path", "PATH"))

    def test_the_npm_shim_starts_its_own_native_copy(self):
        prefix = self.tmp / "nodejs"  # npm's own folder, not the usual one of %APPDATA%
        shim = self.file(prefix / "codex.cmd")
        native = self.file(prefix / "node_modules" / "@openai" / "codex" / "vendor"
                           / "x86_64-pc-windows-msvc" / "codex" / "codex.exe")
        self.env["PATH"] = str(shim.parent)
        found = self.find("codex")
        self.assertEqual((Path(found.path), found.origin), (native, "npm"))
        native.unlink()
        self.assertEqual(Path(self.find("codex", refresh=True).path), shim)  # nothing native left: the shim

    def test_windowsapps_is_never_taken(self):
        store = self.file(self.local / "Microsoft" / "WindowsApps" / "codex.exe")
        self.env["PATH"] = str(store.parent)
        with self.assertRaises(locate.NotFound):
            self.find("codex")
        self.assertIn("WindowsApps", locate.refused("codex", r"C:\Program Files\WindowsApps\OpenAI.Codex_1_x64__2p\app\resources\codex.exe"))
        app = self.app_copy("2e5e00da")
        self.assertEqual(Path(self.find("codex", refresh=True).path), app)
        # A folder of the PATH after WindowsApps is still looked at.
        tools = self.file(self.tmp / "outils" / "codex.exe")
        self.env["PATH"] = os.pathsep.join([str(store.parent), str(tools.parent)])
        self.assertEqual(Path(self.find("codex").path), tools)

    def test_the_newest_copy_of_the_codex_app(self):
        old = self.app_copy("aaaa", age_days=10)
        new = self.app_copy("bbbb", age_days=1)
        left_behind = self.file(self.home / ".codex" / ".sandbox-bin" / "codex.exe", age_days=30)
        found = self.find("codex")
        self.assertEqual((Path(found.path), found.origin, found.where), (new, "codex_app", "application Codex"))
        new.unlink()
        self.assertEqual(Path(self.find("codex").path), old)  # the copy kept has gone: searched again
        old.unlink()
        self.assertEqual(Path(self.find("codex").path), left_behind)  # older apps' copy, when nothing is newer
        os.utime(left_behind, None)
        self.app_copy("cccc", age_days=2)
        self.assertEqual(Path(self.find("codex", refresh=True).path), left_behind)  # newer: taken

    def test_the_highest_version_of_the_claude_app_then_the_newest_place(self):
        root = self.appdata / "Claude" / "claude-code"
        low = self.file(root / "2.1.295" / "d2e2" / "claude.exe", age_days=1)
        high = self.file(root / "2.1.299" / "f058" / "claude.exe", age_days=2)  # older file, higher version
        self.file(root / "pas-une-version" / "abc" / "claude.exe")
        found = self.find("claude")
        self.assertEqual((Path(found.path), found.version, found.where), (high, "2.1.299", "application Claude"))
        self.file(self.home / ".local" / "bin" / "claude.exe")  # the official installer, newer
        self.assertEqual(self.find("claude", refresh=True).origin, "native")
        self.assertTrue(low.is_file())

    def test_a_native_copy_before_a_newer_cmd(self):
        app = self.app_copy("aaaa", age_days=5)
        self.file(self.appdata / "npm" / "codex.cmd")  # newer, but cmd.exe cuts its arguments
        self.assertEqual(Path(self.find("codex").path), app)

    def test_a_desktop_app_is_not_a_cli(self):
        gui = self.file(self.home / ".local" / "bin" / "claude.exe")
        self.file(gui.parent / "icudtl.dat")
        with self.assertRaises(locate.NotFound):
            self.find("claude")
        for marker in ("Update.exe", "resources/app.asar"):
            folder = self.tmp / marker.replace("/", "-")
            self.file(folder / marker)
            self.assertIn("application de bureau", locate.refused("claude", folder / "claude.exe"))

    def test_a_path_kept_that_has_gone_is_said_and_searched_around(self):
        kept = self.file(self.tmp / "ailleurs" / "codex.exe")
        found = self.find("codex", setting=str(kept))
        self.assertEqual((found.origin, found.where, found.note), ("setting", "réglage", None))
        kept.unlink()
        app = self.app_copy("aaaa")
        found = self.find("codex", setting=str(kept))
        self.assertEqual((Path(found.path), found.origin), (app, "codex_app"))
        self.assertIn("retenu dans Réglages n’existe plus", found.note)
        self.assertIn(str(kept), found.note)
        app.unlink()
        with self.assertRaises(locate.NotFound) as caught:
            self.find("codex", setting=str(kept))
        self.assertIn("retenu dans Réglages n’existe plus", str(caught.exception))
        self.assertIn("Chercher sur ce PC", str(caught.exception))

    def test_the_launch_option_comes_first_then_the_setting(self):
        option = self.file(self.tmp / "option" / "claude.exe")
        setting = self.file(self.tmp / "reglage" / "claude.exe")
        self.assertEqual(self.find("claude", option=str(option), setting=str(setting)).origin, "option")
        option.unlink()
        found = self.find("claude", option=str(option), setting=str(setting))
        self.assertEqual((Path(found.path), found.origin), (setting, "setting"))
        self.assertIn("--claude", found.note)

    def test_nothing_found_says_what_was_looked_at_and_what_to_do(self):
        with self.assertRaises(locate.NotFound) as caught:
            self.find("codex")
        text = str(caught.exception)
        for part in ("Codex introuvable sur ce PC", "application Codex", "npm i -g @openai/codex",
                     "Chercher sur ce PC"):
            self.assertIn(part, text)
        self.assertNotIn("WinError", text)
        self.assertIsInstance(caught.exception, ProviderError)

    def test_kept_a_while_while_its_file_is_there(self):
        first = self.app_copy("aaaa", age_days=3)
        self.assertEqual(Path(self.find("codex").path), first)
        self.app_copy("bbbb")  # newer, but the copy found is still there: kept
        self.assertEqual(Path(self.find("codex").path), first)
        with mock.patch.object(locate, "CACHE_S", 0):
            self.assertNotEqual(Path(self.find("codex").path), first)  # past CACHE_S: searched again
        locate.forget()
        self.assertNotEqual(Path(self.find("codex").path), first)


@unittest.skipUnless(os.name == "nt", "la recherche de Windows commence par le dossier courant")
class CurrentFolderTests(PC):
    windows_which = False  # Windows' own search (shutil.which), as the server meets it

    def test_the_current_folder_is_not_the_path(self):
        """The hub's current folder, where its agents may write: a codex.exe put there is never
        started, nor one of a PATH folder not given in full."""
        here = self.tmp / "dossier-du-serveur"
        self.file(here / "codex.exe")
        self.file(here / "relatif" / "codex.exe")
        empty = self.tmp / "vide"
        empty.mkdir()
        cwd = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, cwd)
        self.env["PATH"] = os.pathsep.join([str(empty), ".", "relatif"])
        with mock.patch.dict(os.environ):
            # Started outside a terminal that sets it, Windows looks in the current folder first.
            os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
            with self.assertRaises(locate.NotFound):
                self.find("codex")
            self.env["PATH"] = str(empty)
            with self.assertRaises(locate.NotFound):
                self.find("codex", refresh=True)
        tools = self.file(self.tmp / "outils" / "codex.exe")
        self.env["PATH"] = os.pathsep.join([".", str(tools.parent)])
        self.assertEqual(self.find("codex", refresh=True).path, str(tools))


class LinuxTests(PC):
    system = "posix"

    def test_the_places_of_linux(self):
        for program, path, origin in (
                ("claude", self.home / ".local" / "bin" / "claude", "native"),
                ("claude", self.home / ".claude" / "local" / "claude", "native"),
                ("codex", self.home / ".npm-global" / "bin" / "codex", "npm"),
                ("codex", self.home / ".nvm" / "versions" / "node" / "v22.3.0" / "bin" / "codex", "nvm"),
                ("codex", self.home / ".cargo" / "bin" / "codex", "cargo"),
                ("claude", self.root / "usr" / "local" / "bin" / "claude", "system"),
                ("codex", self.root / "opt" / "homebrew" / "bin" / "codex", "homebrew"),
                ("sbx", self.home / ".local" / "bin" / "sbx", "native")):
            with self.subTest(path=path):
                self.file(path)
                found = self.find(program, refresh=True)
                self.assertEqual((Path(found.path), found.origin), (path, origin))
                path.unlink()

    def test_no_windows_name_on_linux(self):
        self.file(self.home / ".local" / "bin" / "claude.exe")
        with self.assertRaises(locate.NotFound):
            self.find("claude")


class StartTests(PC):
    def spawn(self, *errors):
        calls = []
        errors = list(errors)

        def spawn(args, **options):
            calls.append(list(args))
            if errors:
                raise errors.pop(0)
            return "lancé"
        return spawn, calls

    def test_a_vanished_file_is_searched_again_before_the_start(self):
        old = self.app_copy("aaaa", age_days=2)
        command = locate.command("codex", **self.where())
        self.assertEqual((list(command), command.found.origin), ([str(old)], "codex_app"))
        old.unlink()
        new = self.app_copy("bbbb")  # the app updated itself: another folder
        spawn, calls = self.spawn()
        self.assertEqual(locate.start(command, ["login", "status"], spawn, text=True), "lancé")
        self.assertEqual(calls, [[str(new), "login", "status"]])

    def test_a_start_that_finds_no_file_searches_again_once(self):
        first = self.app_copy("aaaa", age_days=2)
        command = locate.command("codex", **self.where())
        second = self.app_copy("bbbb")
        spawn, calls = self.spawn(FileNotFoundError(2, "introuvable"))
        self.assertEqual(locate.start(command, ["x"], spawn), "lancé")
        self.assertEqual(calls, [[str(first), "x"], [str(second), "x"]])
        # Still nothing to start after the second search: no third try, a clear message.
        spawn, calls = self.spawn(FileNotFoundError(2, "a"), FileNotFoundError(2, "b"), FileNotFoundError(2, "c"))
        with self.assertRaisesRegex(ProviderError, "impossible de lancer Codex") as caught:
            locate.start(command, ["x"], spawn)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("WinError", str(caught.exception))

    def test_gone_and_nothing_else_is_not_found(self):
        only = self.app_copy("aaaa")
        command = locate.command("codex", **self.where())
        only.unlink()
        spawn, calls = self.spawn()
        with self.assertRaises(locate.NotFound):
            locate.start(command, [], spawn)
        self.assertEqual(calls, [])

    def test_a_search_then_a_start_without_file_is_not_retried(self):
        old = self.app_copy("aaaa", age_days=2)
        command = locate.command("codex", **self.where())
        old.unlink()
        self.app_copy("bbbb")
        spawn, calls = self.spawn(FileNotFoundError(2, "a"), FileNotFoundError(2, "b"))
        with self.assertRaises(ProviderError):
            locate.start(command, [], spawn)
        self.assertEqual(len(calls), 1)  # the search before the start was the one try

    def test_a_given_command_is_started_as_it_is(self):
        spawn, calls = self.spawn(FileNotFoundError(2, "introuvable"))
        with mock.patch.object(locate, "find", side_effect=AssertionError("pas de recherche")):
            with self.assertRaises(FileNotFoundError):
                locate.start(["C:/absent/claude.exe"], ["-p"], spawn)
        self.assertEqual(calls, [["C:/absent/claude.exe", "-p"]])

    @unittest.skipUnless(os.name == "nt", "cmd.exe, qui lance un .cmd, est celui de Windows")
    def test_a_cmd_never_gets_what_cmd_exe_would_cut_or_run(self):
        """npm's claude.cmd, alone on this PC: cmd.exe would end the line at a line break, put
        the value of %NAME%, and run what follows a & outside quotes (measured with cmd.exe)."""
        shim = self.file(self.appdata / "npm" / "claude.cmd")
        command = locate.command("claude", **self.where())
        self.assertEqual(Path(command[0]), shim)
        for text in ("ligne 1\nligne 2", "a&calc", "x|y", "%PATHEXT%", 'a"b c&calc', "a^b",
                     json.dumps({"mcpServers": {"a b": {"args": ["x & y"]}}})):
            spawn, calls = self.spawn()
            with self.subTest(text=text), self.assertRaisesRegex(ProviderError, "script .cmd"):
                locate.start(command, ["--append-system-prompt", text], spawn)
            self.assertEqual(calls, [])
        # What cmd.exe passes as it is still starts: a & between quotes, a % alone, JSON.
        spawn, calls = self.spawn()
        tail = ["-p", "--append-system-prompt", 'dit "oui" & baisse de 50 %', "--mcp-config", '{"mcpServers": {}}']
        self.assertEqual(locate.start(command, tail, spawn), "lancé")
        self.assertEqual(calls, [[str(shim), *tail]])
        # A given command too: cmd.exe never runs part of a request.
        with self.assertRaisesRegex(ProviderError, "script .cmd"):
            locate.start([str(shim)], ["a&calc"], spawn)
        self.assertEqual(len(calls), 1)



class RealRunTests(PC):
    system = os.name
    windows_which = False

    def test_a_real_run_after_its_cli_has_moved(self):
        """Claude Code found, then moved by an update: the run searches again and goes on."""
        kept = wrapper(self.tmp / "avant" / "claude")
        moved = wrapper(self.tmp / "apres" / "claude")
        # The PC of the test only (its AppData, its home): never the real Claude Code of this computer.
        command = locate.command("claude", setting=str(kept), **self.where())
        self.env["PATH"] = str(moved.parent)
        kept.unlink()
        run = ClaudeCodeRun(command, prompt="salut", workdir=str(self.make_tempdir()), permission_mode="manual",
                            env=fake_env("text", self.tmp / "log.json"))
        result = run.run()
        self.assertEqual((result.status, result.text), ("ok", "Bonjour"), result.error)


class ProvidersTests(PC):
    def test_find_cli_keeps_its_strict_explicit_path(self):
        with self.assertRaisesRegex(ProviderError, "introuvable à l'emplacement"):
            claude_code.find_cli(str(self.tmp / "absent.exe"))
        with self.assertRaisesRegex(ProviderError, "introuvable à l'emplacement"):
            codex.find_cli(str(self.tmp / "absent.exe"))

    def test_command_for_a_searched_cli_and_its_way_to_a_delegation(self):
        kept = self.file(self.tmp / "reglage" / "claude.exe")
        commands = json.loads(json.dumps({"claude_code": providers.searched(None, str(kept)), "codex": False}))
        command = providers.command_for(commands, "claude_code")
        self.assertIsInstance(command, locate.Command)
        self.assertEqual((list(command), command.found.origin), ([str(kept)], "setting"))

    def test_a_claude_command_alone_offers_no_other_ai(self):
        """A fake Claude as a list (a test's): Codex is never searched for behind it. It would
        be the real one of this PC, on its owner's account (1.3.0: a summary chose Codex)."""
        with mock.patch.object(locate, "find", side_effect=AssertionError("recherche du vrai Codex")):
            self.assertEqual(providers.command_for(FAKE_COMMAND, "claude_code"), FAKE_COMMAND)
            for provider in ("codex", "ollama"):
                with self.subTest(provider=provider), self.assertRaisesRegex(ProviderError, "pas branché"):
                    providers.command_for(FAKE_COMMAND, provider)
            with self.assertRaisesRegex(ProviderError, "pas branché"):
                providers.command_for(FAKE_COMMAND, "codex", execution=object())

    def test_a_delegation_under_a_claude_command_alone_offers_no_codex(self):
        """The delegation's process gets the commands in JSON (aihub/tools.py): the same rule there."""
        from aihub import db, store, tools
        conn = db.connect(str(self.tmp / "hub.db"))
        self.addCleanup(conn.close)
        cid = store.create_conversation(conn, title="T", workdir=str(self.tmp))
        store.set_tools(conn, cid, [tools.DELEGATE])
        settings = config.Settings(models=["sonnet", "gpt-5.6-sol"])
        with mock.patch.object(tools, "delegate_models", return_value=["sonnet", "gpt-5.6-sol"]):
            run = tools.for_run(conn, store.get_conversation(conn, cid), settings, commands=FAKE_COMMAND)
        args = run.mcp_config["mcpServers"][tools.DELEGATE]["args"]
        sent = json.loads(args[args.index("--commands") + 1])
        self.assertEqual(sent["claude_code"], FAKE_COMMAND)
        with mock.patch.object(locate, "find", side_effect=AssertionError("recherche du vrai Codex")):
            with self.assertRaisesRegex(ProviderError, "pas branché"):
                providers.command_for(sent, "codex")

    def test_an_isolated_space_never_searches_the_pc(self):
        commands = {"claude_code": providers.searched("C:/x/claude.exe", "C:/y/claude.exe"),
                    "codex": providers.searched(None, None)}
        with mock.patch.object(locate, "find", side_effect=AssertionError("pas de recherche sur le PC")):
            self.assertEqual(providers.command_for(commands, "claude_code", execution=object()), ["claude"])
            self.assertEqual(providers.command_for(commands, "codex", execution=object()), ["codex"])

            class Space:
                def spawn(self, args, **options):
                    self.args = args
                    raise OSError("arrêt de l'essai")
            space = Space()
            with self.assertRaisesRegex(ProviderError, "arrêt de l'essai"):
                codex._Server(["C:/disparu/codex.exe"], str(self.tmp), {}, execution=space)
            self.assertEqual(space.args[:2], ["codex", "app-server"])

    def test_the_isolated_claude_is_started_as_it_is(self):
        class Space:
            workdir = "/work"

            def spawn(self, args, **options):
                self.args = args
                raise OSError("arrêt de l'essai")

            def stop(self):
                pass

            def finish(self):
                pass
        space = Space()
        run = ClaudeCodeRun(["claude"], prompt="x", workdir=str(self.tmp), permission_mode="manual", execution=space)
        with mock.patch.object(locate, "find", side_effect=AssertionError("pas de recherche sur le PC")):
            with self.assertRaises(ProviderError):
                run.run()
        self.assertEqual(space.args[0], "claude")


class ScanTests(PC):
    def scan(self, roots, **kwargs):
        kwargs.setdefault("versions", False)
        return locate.scan(roots=roots, **self.where(), **kwargs)

    def copies(self, result, program):
        return [Path(c["path"]) for c in next(p for p in result["programs"] if p["program"] == program)["copies"]]

    def test_folders_simulated_bounded_in_depth(self):
        disk = self.tmp / "disque"
        near = self.file(disk / "outils" / "codex.exe")
        far = self.file(disk / "a" / "b" / "c" / "d" / "codex.exe")
        script = self.file(disk / "outils" / "claude.cmd")
        result = self.scan([(disk, 2)])
        self.assertTrue(result["complete"])
        self.assertEqual(self.copies(result, "codex"), [near])
        self.assertNotIn(far, self.copies(result, "codex"))
        self.assertEqual(self.copies(result, "claude"), [script])
        copy = next(p for p in result["programs"] if p["program"] == "codex")["copies"][0]
        self.assertEqual((copy["origin"], copy["where"], copy["in_use"]), ("scan", "recherche sur le PC", False))
        self.assertEqual(len(self.copies(self.scan([(disk, 4)]), "codex")), 2)

    def test_bounded_in_time(self):
        disk = self.tmp / "disque"
        for n in range(30):
            self.file(disk / f"dossier{n:02}" / "codex.exe")
        ticks = itertools.count()
        result = self.scan([(disk, 1)], max_seconds=5, clock=lambda: next(ticks))
        self.assertFalse(result["complete"])
        self.assertLess(len(self.copies(result, "codex")), 30)
        self.assertFalse(self.scan([(disk, 1)], max_dirs=3)["complete"])

    def test_unreadable_folders_are_passed_over(self):
        disk = self.tmp / "disque"
        self.file(disk / "interdit" / "codex.exe")
        allowed = self.file(disk / "permis" / "codex.exe")
        real = os.scandir

        def scandir(path):
            if Path(path).name == "interdit":
                raise PermissionError(13, "Accès refusé")
            return real(path)
        with mock.patch("os.scandir", side_effect=scandir):
            result = self.scan([(disk, 2), (self.tmp / "absent", 2)])
        self.assertEqual(self.copies(result, "codex"), [allowed])
        self.assertTrue(result["complete"])

    def test_links_are_not_walked_into(self):
        disk = self.tmp / "disque"
        target = self.tmp / "ailleurs"
        self.file(target / "codex.exe")
        disk.mkdir()
        try:
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(target), str(disk / "lien"))
            else:
                os.symlink(target, disk / "lien", target_is_directory=True)
        except (OSError, AttributeError) as exc:
            self.skipTest(f"lien impossible ici : {exc}")
        self.assertEqual(self.copies(self.scan([(disk, 3)]), "codex"), [])

    def test_left_out_windowsapps_desktop_apps_and_an_older_sandbox_bin(self):
        disk = self.tmp / "disque"
        self.file(disk / "WindowsApps" / "OpenAI.Codex_1_x64" / "codex.exe")
        gui = self.file(disk / "AnthropicClaude" / "app-2.4" / "claude.exe")
        self.file(gui.parent / "resources" / "app.asar")
        old = self.file(self.home / ".codex" / ".sandbox-bin" / "codex.exe", age_days=60)
        new = self.app_copy("aaaa", age_days=1)
        result = self.scan([(disk, 4)])
        self.assertEqual(self.copies(result, "codex"), [new])
        self.assertEqual(self.copies(result, "claude"), [])
        new.unlink()
        self.assertEqual(self.copies(self.scan([(disk, 4)]), "codex"), [old])  # nothing newer: shown

    def test_the_copy_in_use_first_and_only_trusted_copies_are_run(self):
        app = self.app_copy("aaaa", age_days=1)
        met = self.file(self.tmp / "disque" / "Telechargements" / "codex.exe")
        asked = []
        with mock.patch.object(locate, "_version_of", side_effect=lambda path: asked.append(Path(path)) or "0.163.0"):
            result = self.scan([(self.tmp / "disque", 3)], versions=True)
        codex_entry = next(p for p in result["programs"] if p["program"] == "codex")
        self.assertEqual(self.copies(result, "codex"), [app, met])
        self.assertEqual([c["in_use"] for c in codex_entry["copies"]], [True, False])
        self.assertEqual(codex_entry["in_use"]["where"], "application Codex")
        self.assertEqual(asked, [app])  # a file met elsewhere is never run
        self.assertEqual(codex_entry["copies"][0]["version"], "0.163.0")

    def test_the_copy_kept_is_in_use_and_asked_its_version(self):
        kept = self.file(self.tmp / "disque" / "outils" / "codex.exe")  # met by the walk only
        asked = []
        with mock.patch.object(locate, "_version_of", side_effect=lambda path: asked.append(Path(path)) or "0.150.0"):
            result = self.scan([(self.tmp / "disque", 3)], versions=True, where={"codex": {"setting": str(kept)}})
        entry = next(p for p in result["programs"] if p["program"] == "codex")
        self.assertEqual((entry["in_use"]["where"], entry["copies"][0]["in_use"], entry["copies"][0]["version"]),
                         ("réglage", True, "0.150.0"))
        self.assertEqual(asked, [kept])  # the hub starts it anyway

    def test_the_roots_are_local_disks(self):
        env = dict(self.env, ProgramFiles=r"C:\Program Files", LOCALAPPDATA=r"\\serveur\profil\AppData\Local")
        with mock.patch("aihub.vaults._fixed_drives", return_value=["C:\\", "G:\\"]):
            roots = locate.scan_roots(system="nt", env=env, home=Path(r"\\serveur\profil"))
        self.assertEqual([str(folder) for folder, _ in roots], [r"C:\Program Files", "C:\\", "G:\\"])
        roots = dict(locate.scan_roots(system="nt", env=dict(env, LOCALAPPDATA=r"D:\Profils\p\AppData\Local"),
                                       home=Path(r"D:\Profils\p")))
        self.assertEqual(roots[Path(r"D:\Profils\p")], locate.HOME_DEPTH)


class ConfigTests(PC):
    def test_a_program_kept_must_be_there_and_be_the_program(self):
        exe = self.file(self.tmp / "outils" / "codex.exe")
        self.assertEqual(config.validate({"codex_cli": str(exe)}).codex_cli, str(exe.resolve()))
        for value, fragment in ((str(self.tmp / "absent" / "codex.exe"), "introuvable"),
                                ("relatif\\codex.exe", "chemin complet"),
                                (str(self.file(self.tmp / "notepad.exe")), "programme codex"),
                                (str(self.file(self.tmp / "WindowsApps" / "codex.exe")), "WindowsApps")):
            with self.subTest(value=value), self.assertRaisesRegex(config.ConfigError, fragment):
                config.validate({"codex_cli": value})
        self.assertIsNone(config.validate({"claude_cli": ""}).claude_cli)

    @unittest.skipUnless(os.name == "nt", "chemins Windows")
    def test_a_network_path_is_refused_unread(self):
        with mock.patch("pathlib.Path.is_file", side_effect=AssertionError("lu")):
            with self.assertRaisesRegex(config.ConfigError, "disque local"):
                config.validate({"claude_cli": r"\\serveur\partage\claude.exe"})

    def test_a_program_kept_that_has_gone_does_not_stop_the_server(self):
        path = self.tmp / "config.json"
        gone = str(self.tmp / "parti" / "codex.exe")
        path.write_text(json.dumps({"codex_cli": gone, "sandbox_cli": str(self.tmp / "parti" / "sbx.exe")}),
                        encoding="utf-8")
        settings = config.load(path)
        self.assertEqual(settings.codex_cli, gone)  # kept: the search says it has gone and goes on
        self.assertEqual(config.validate({"quota_warning": 70}, base=settings).codex_cli, gone)


@unittest.skipIf(TestClient is None, "web dependencies missing")
class WebTests(PC):
    windows_which = False

    def setUp(self):
        super().setUp()
        from aihub import auth, db, routing
        from .test_auth import new_password
        # The state read here says « Codex connected » to the routing table: put back after, or a
        # later test's summary would choose a Codex model, and then the real Codex of this PC.
        self.addCleanup(routing.set_available, {"claude_code": None})
        self.db_path = str(self.tmp / "hub.db")
        self.config_path = self.tmp / "config.json"
        self.password = new_password()
        conn = db.connect(self.db_path)
        auth.set_password(conn, self.password)
        conn.close()
        self.claude_env = fake_env("text", self.tmp / "log.json")
        self.claude_env["FAKE_CLAUDE_LOGGED_IN"] = "1"
        # The Claude Code of the test (never the real one of this computer), kept in Réglages.
        self.before = wrapper(self.tmp / "avant" / "claude")
        config.save(self.config_path, config.validate({"claude_cli": str(self.before)}))
        self.app = create_app(self.db_path, env=self.claude_env, auth_policy=auth.Policy(),
                              config_path=self.config_path, settings=config.load(self.config_path),
                              codex_command=FAKE_CODEX, ollama_url="http://127.0.0.1:9", run_scheduler=False)
        self.owner = self.client(("127.0.0.1", 50000))
        self.assertEqual(self.owner.post("/api/auth/login", json={"password": self.password}).status_code, 200)

    def client(self, address):
        client = TestClient(self.app, client=address, headers={"X-AIHub": "1"})
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def person(self, name, role):
        r = self.owner.post("/api/users", json={"name": name, "password": "", "role": role, "grants": []})
        self.assertEqual(r.status_code, 201, r.text)
        other = self.client(("127.0.0.1", 50100 + r.json()["user"]["id"]))
        self.assertEqual(other.post("/api/auth/login", json={"name": name, "password": ""}).status_code, 200)
        return other

    def test_the_scan_is_the_owners(self):
        found = {"programs": [{"program": program, "in_use": {"found": True, "where": "PATH"},
                               "copies": [{"path": "x", "in_use": True}]} for program in locate.PROGRAMS],
                 "complete": True, "seconds": 1.0}
        with mock.patch.object(locate, "scan", return_value=found) as scan:
            r = self.owner.post("/api/programs/scan")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["complete"], [p["program"] for p in r.json()["programs_state"]]),
                             (True, ["claude", "codex", "sbx"]))
            claude_entry, codex_entry, _ = r.json()["programs"]
            self.assertTrue(claude_entry["copies"][0]["in_use"])  # searched: as the scan says
            # The Codex the server was given is the one in use, whatever the scan met.
            self.assertEqual((codex_entry["in_use"]["where"], codex_entry["copies"][0]["in_use"]),
                             ("commande donnée au serveur", False))
            for role in ("membre", "invite", "admin"):
                with self.subTest(role=role):
                    self.assertEqual(self.person(f"P-{role}", role).post("/api/programs/scan").status_code, 403)
        self.assertEqual(scan.call_count, 1)
        self.assertIn("claude", scan.call_args.kwargs["where"])

    def test_a_copy_kept_is_saved_and_tried_at_once(self):
        self.assertEqual(Path(self.owner.get("/api/status").json()["cli"]), self.before.resolve())
        cli = wrapper(self.tmp / "outils" / "claude")
        r = self.owner.patch("/api/config", json={"changes": {"claude_cli": str(cli)}, "password": self.password})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["claude_cli"], str(cli.resolve()))
        claude_state = next(p for p in r.json()["programs"] if p["program"] == "claude")
        self.assertEqual((claude_state["where"], Path(claude_state["path"])), ("réglage", cli.resolve()))
        status = self.owner.get("/api/status").json()
        self.assertEqual((status["logged_in"], status["found"], status["where"]), (True, True, "réglage"))
        self.assertEqual(Path(status["cli"]), cli.resolve())  # at once: not the state read before
        refused = self.owner.patch("/api/config", json={"changes": {"codex_cli": str(cli)}, "password": "faux"})
        self.assertEqual(refused.status_code, 401)  # the password, as for the other settings
        member = self.person("Sam", "membre")
        self.assertEqual(member.patch("/api/config", json={"changes": {"claude_cli": None}}).status_code, 403)

    def test_status_and_diagnostics_say_it_in_words(self):
        with mock.patch.object(locate, "find", side_effect=locate.NotFound("claude")):
            status = self.owner.get("/api/status").json()
            checks = self.owner.get("/api/diagnostics").json()["checks"]
        self.assertEqual((status["found"], status["logged_in"]), (False, None))
        self.assertIn("Chercher sur ce PC", status["hint"])
        self.assertNotIn("WinError", status["hint"])
        claude_check = next(c for c in checks if c.get("id") == "claude_code")
        self.assertEqual((claude_check["ok"], claude_check["detail"]), (False, "Claude introuvable sur le PC du serveur."))
        self.assertIn("Chercher sur ce PC", claude_check["action"])
        self.assertEqual(next(c for c in checks if c.get("id") == "codex")["detail"],
                         "Codex trouvé (commande donnée au serveur).")

    def test_diagnostics_name_where_it_comes_from_never_its_path(self):
        secret_place = self.file(self.tmp / "dossier-prive" / "claude.exe")
        found = locate.Found("claude", str(secret_place), "claude_app")
        with mock.patch.object(locate, "find", return_value=found):
            response = self.owner.get("/api/diagnostics")
        check = next(c for c in response.json()["checks"] if c.get("id") == "claude_code")
        self.assertEqual((check["ok"], check["detail"]), (True, "Claude trouvé (application Claude)."))
        self.assertNotIn("dossier-prive", response.text)

    def test_hints(self):
        state = {"found": True, "where": "PATH", "note": None, "logged_in": False, "error": None}
        self.assertIn("claude auth login", _status_hint("Claude Code", state, "lancez « claude auth login »"))
        state.update(error="[WinError 5] Accès refusé")
        hint = _status_hint("Claude Code", state, "x")
        self.assertIn("trouvé (PATH) mais ne répond pas", hint)
        self.assertNotIn("WinError", hint)
        state.update(logged_in=True, note="Le chemin retenu n’existe plus.")
        self.assertEqual(_status_hint("Claude Code", state, "x"), "Le chemin retenu n’existe plus.")


@unittest.skipIf(TestClient is None, "web dependencies missing")
class ServeTests(TempDirMixin, unittest.TestCase):
    def test_a_launch_path_is_tried_first_and_never_fixed(self):
        from aihub import cli
        tmp = self.make_tempdir()
        output = []
        gone = str(tmp / "absent" / "codex.exe")
        with mock.patch("uvicorn.run") as run:
            code = cli.main(["--db", str(tmp / "hub.db"), "--codex", gone, "serve"],
                            out=lambda text="", end="\n": output.append(text + end),
                            command=[sys.executable, "-B", str(FAKE_CLI)])
        self.assertEqual(code, 0)
        self.assertIn("--codex", "".join(output))
        self.assertIn("recherche automatique", "".join(output))
        commands = run.call_args[0][0].state.jobs.command
        self.assertEqual(commands["codex"], {"option": gone, "setting": None})

    def test_ask_with_a_claude_path_still_offers_codex(self):
        """« aihub ask --claude … » gives Claude Code's path only: a Codex conversation still finds Codex."""
        from aihub import cli
        tmp = self.make_tempdir()
        exe = tmp / "claude.exe"
        exe.write_text("", encoding="utf-8")
        with mock.patch.object(cli, "cmd_ask", return_value=0) as ask:
            code = cli.main(["--db", str(tmp / "hub.db"), "--claude", str(exe), "ask", "--workdir", str(tmp), "salut"],
                            out=lambda text="", end="\n": None)
        self.assertEqual(code, 0)
        command = ask.call_args[0][4]
        self.assertEqual(providers.command_for(command, "claude_code"), [str(exe)])
        found = locate.Found("codex", str(tmp / "codex.exe"), "path")
        with mock.patch.object(locate, "find", return_value=found):
            self.assertEqual(list(providers.command_for(command, "codex")), [found.path])

    @unittest.skipUnless(shutil.which("node"), "Node.js nécessaire pour vérifier app.js")
    def test_each_keep_button_says_which_copy(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "program_scan_checks.js"),
                                 str(root / "aihub" / "web" / "static" / "app.js")], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
