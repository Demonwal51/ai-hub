"""The tests' own guard (tests/guard.py): no test may start a real Claude Code, Codex or sbx."""

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from aihub import locate

from . import guard
from .helpers import TempDirMixin

# Where a real program could be: never created by these tests, never started.
ELSEWHERE = Path(os.environ.get("SystemDrive", "C:") + os.sep) / "Programmes-AIHub-essai" / "OpenAI" / "codex.exe"


class GuardTests(TempDirMixin, unittest.TestCase):
    def nothing_started(self):
        """A patch of what really starts a process: called, it would say so (and start nothing)."""
        target = "_winapi.CreateProcess" if os.name == "nt" else "subprocess._fork_exec"
        return mock.patch(target, side_effect=AssertionError("un processus a été créé"))

    def assert_blocked(self, start):
        with guard.expected(), self.nothing_started() as created:
            with self.assertRaises(guard.RealProgramBlocked) as caught:
                start()
        created.assert_not_called()
        self.assertIn("garde-fou des tests", str(caught.exception))

    def test_a_program_of_the_pc_is_stopped_before_it_starts(self):
        for start in (lambda: subprocess.run([str(ELSEWHERE), "--version"]),
                      lambda: subprocess.Popen([str(ELSEWHERE.with_name("claude.exe")), "-p"]),
                      lambda: subprocess.run(str(ELSEWHERE.with_name("sbx.exe")) + " version"),
                      lambda: subprocess.run(["codex", "login", "status"]),
                      lambda: subprocess.run("codex --version", shell=True),
                      lambda: subprocess.run(["cmd", "/c", "claude", "-p"]),
                      lambda: subprocess.run(["cmd", "/c", "codex login status"]),
                      lambda: subprocess.run(["node", str(ELSEWHERE.parent / "node_modules" / "codex" / "bin" / "codex.js")]),
                      lambda: subprocess.run([sys.executable], executable=str(ELSEWHERE)),
                      lambda: os.system("claude --version")):
            with self.subTest(start=start):
                self.assert_blocked(start)

    def test_asyncio_too(self):
        async def start():
            await asyncio.create_subprocess_exec(str(ELSEWHERE), "app-server")
        self.assert_blocked(lambda: asyncio.run(start()))

    def test_a_test_s_own_fake_starts(self):
        tmp = self.make_tempdir()
        if os.name == "nt":
            fake = tmp / "codex.cmd"
            fake.write_text("@echo faux codex %*\r\n", encoding="utf-8")
        else:
            fake = tmp / "codex"
            fake.write_text("#!/bin/sh\necho faux codex \"$@\"\n", encoding="utf-8")
            fake.chmod(0o755)
        result = subprocess.run([str(fake), "--version"], capture_output=True, text=True, timeout=30)
        self.assertIn("faux codex --version", result.stdout)
        # Other programs are not looked at: a model's name is not a program.
        result = subprocess.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "claude-opus"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.strip(), "claude-opus")

    def test_a_blocked_start_fails_its_test_even_when_caught(self):
        class Swallows(unittest.TestCase):
            def test_it(self):
                try:
                    subprocess.run([str(ELSEWHERE)])
                except Exception:
                    pass  # code under test that hides the error
        result = unittest.TestResult()
        with self.nothing_started() as created:
            Swallows("test_it").run(result)
        created.assert_not_called()
        self.assertEqual(len(result.failures), 1)
        self.assertIn("ce test a tenté de lancer un vrai programme", result.failures[0][1])
        del guard.BLOCKED[-1:]

    def test_the_search_sees_an_empty_pc(self):
        for name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
            self.assertIn(guard.EMPTY_PC, Path(os.environ[name]).parents)
        self.assertNotIn("CODEX_HOME", os.environ)
        for program in locate.PROGRAMS:
            with self.subTest(program=program), self.assertRaises(locate.NotFound):
                locate.find(program, refresh=True)
        result = locate.scan(versions=False)
        self.assertEqual([p["copies"] for p in result["programs"]], [[], [], []])


if __name__ == "__main__":
    unittest.main()
