"""Shared helpers for tests that run the fake Claude Code CLI."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

FAKE_CLI = Path(__file__).with_name("fake_claude.py")
FAKE_COMMAND = [sys.executable, "-B", str(FAKE_CLI)]
FAKE_CODEX = [sys.executable, "-B", str(Path(__file__).with_name("fake_codex.py"))]
# Both fakes, by provider: what the hub is given when Codex is connected.
FAKE_COMMANDS = {"claude_code": FAKE_COMMAND, "codex": FAKE_CODEX}


def fake_env(scenario, log_path, *, codex_scenario="hello", codex_log=None):
    env = dict(os.environ)
    env["FAKE_CLAUDE_SCENARIO"] = scenario
    env["FAKE_CLAUDE_LOG"] = str(log_path)
    env["FAKE_CODEX_SCENARIO"] = codex_scenario
    if codex_log is not None:
        env["FAKE_CODEX_LOG"] = str(codex_log)
    return env


def read_log(log_path):
    with open(log_path, encoding="utf-8") as fh:
        return json.load(fh)


def wait_for_log_key(log_path, key, timeout=20):
    """Wait until the fake CLI has written `key` to its log; return the log."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = read_log(log_path)
            if key in data:
                return data
        except (OSError, ValueError):
            pass  # not written yet, or being written
        time.sleep(0.05)
    raise AssertionError(f"la fausse CLI n'a pas écrit « {key} » en {timeout} s")


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    # Container PID 1 may retain a killed orphan as a zombie. It no longer
    # executes or holds files, although kill(pid, 0) still finds its PID.
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                return False
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TempDirMixin:
    def make_tempdir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)
