"""No test may start a real Claude Code, Codex or Docker Sandboxes (sbx) of this PC.

Installed by tests/__init__.py, before any test module is imported (unittest discover,
« python -m unittest tests.test_x », tools/mutations.py). In 1.3.0, tests started the
real Codex of the PC (requests on its owner's account): a summary chose a Codex model
behind a fake Claude, and aihub/locate.py found the copy of the Codex app.

1. Nothing of this PC to find. The folders aihub/locate.py looks at (APPDATA,
   LOCALAPPDATA, the user's folder, so ~/.codex too) are those of an empty PC in the
   temporary folder, and the PATH loses each folder holding a claude*, codex* or sbx*
   program. The processes the tests start (a fake CLI, a delegation's MCP server)
   inherit this environment. `locate.find` and `locate.scan` get an empty / (Linux) and
   no disk to walk, unless the test gives its own.
2. A tripwire. Any start of a program named claude*, codex* or sbx* (subprocess.Popen,
   run, asyncio's subprocesses, os.system, os.startfile, os.spawn*, os.exec*), directly
   or through a shell or node, that is not in the temporary folder where tests make
   their own fakes, raises RealProgramBlocked before anything starts. The test fails even
   when the code under test catches the error.
3. After each test, the routing table and locate's cache are put back: one test's
   « Codex connected » never reaches the next one.

Nothing of this is in aihub/: the application has no switch for tests.
"""

import atexit
import contextlib
import functools
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

NAMES = ("claude", "codex", "sbx")
# Programs that start the one named in their arguments (cmd /c codex, node …\codex.js).
LAUNCHERS = {"cmd", "powershell", "pwsh", "sh", "bash", "dash", "zsh", "env", "wsl", "node", "npx", "npm",
             "bun", "deno", "start", "conhost", "wt", "busybox"}
PROGRAM_EXTENSIONS = {".exe", ".cmd", ".bat", ".com", ".ps1", ".js", ".mjs", ".cjs"}

TEMP = Path(tempfile.gettempdir()).resolve()   # where the tests make their own fake programs
BLOCKED = []                                   # what the tripwire stopped, for the test that ran it
REAL = {}                                      # the real functions, as they were before install()
EMPTY_PC = None                                # the empty PC every test runs on


class RealProgramBlocked(AssertionError):
    """A test tried to start a real program of the PC."""


def _inside_temp(path):
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return resolved == TEMP or TEMP in resolved.parents


def _name(token):
    return re.split(r"[\\/]", token)[-1].lower()


def _named(token):
    return _name(token).startswith(NAMES)


def _pathlike(token):
    return "/" in token or "\\" in token


def _program_refused(token):
    """What would really start if `token` is the program, or None (not one of them, or a test's own)."""
    if not token or not _named(token):
        return None
    if _pathlike(token):
        return None if _inside_temp(token) else token
    hit = shutil.which(token)  # a bare name: what the system would start
    return None if hit and _inside_temp(hit) else (hit or token)


def _argument_refused(token):
    """What would really start if `token` is given to a shell or to node, or None."""
    if not token or not _named(token):
        return None
    if _pathlike(token):
        return None if _inside_temp(token) or os.path.isdir(token) else token
    stem, ext = os.path.splitext(_name(token))
    return token if (stem in NAMES and not ext) or ext in PROGRAM_EXTENSIONS else None


def _split(line):
    """The words of a command line, quotes taken off (Windows' rules, near enough)."""
    return [word.strip('"').strip("'") for word in re.findall(r'"[^"]*"|\S+', line)]


def _launcher(token):
    return os.path.splitext(_name(token))[0] in LAUNCHERS


def check(args, *, executable=None, shell=False):
    """Raise RealProgramBlocked when this start would run a real claude, codex or sbx."""
    if isinstance(args, (str, bytes, os.PathLike)):
        words = _split(os.fsdecode(args)) if (shell or os.name == "nt") else [os.fsdecode(args)]
    else:
        words = [os.fsdecode(a) for a in args]
        if shell and words:
            words = _split(words[0]) + words[1:]
    programs = ([os.fsdecode(executable)] if executable is not None else []) + words[:1]
    found = [p for p in map(_program_refused, programs) if p]
    if shell or any(_launcher(p) for p in programs):
        # cmd /c "codex login": a shell's argument is a command line too.
        found += [w for w in map(_argument_refused, [part for word in words[1:] for part in [word, *_split(word)]]) if w]
    if found:
        what = ", ".join(dict.fromkeys(found))
        BLOCKED.append(what)
        raise RealProgramBlocked(
            f"garde-fou des tests : lancement d'un vrai programme du PC bloqué avant son départ ({what}). "
            "Un test ne lance que ses propres fausses CLI, créées dans un dossier temporaire.")


@contextlib.contextmanager
def expected():
    """For the guard's own tests: what the tripwire stops inside is not held against the test."""
    before = len(BLOCKED)
    try:
        yield
    finally:
        del BLOCKED[before:]


def _guard_execute_child(real):
    """Popen's own start (run, call, check_output and asyncio's subprocesses go through it)."""
    shell_at = list(inspect.signature(real).parameters).index("shell") - 3  # after self, args, executable

    @functools.wraps(real)
    def _execute_child(self, args, executable, *rest, **kwargs):
        shell = rest[shell_at] if len(rest) > shell_at else kwargs.get("shell", False)
        check(args, executable=executable, shell=bool(shell))
        return real(self, args, executable, *rest, **kwargs)
    return _execute_child


def _guard_os(name, how):
    real = getattr(os, name, None)
    if real is None:
        return
    REAL[name] = real

    @functools.wraps(real)
    def guarded(*args, **kwargs):
        how(*args, **kwargs)
        return real(*args, **kwargs)
    setattr(os, name, guarded)


def _check_path_argv(path, argv=(), *rest, **kwargs):
    """os.spawnv(e), os.exec*, os.posix_spawn(p): the program, then its argv."""
    check(list(argv), executable=path)


def _empty_pc():
    """An empty PC in the temporary folder: its user's folder and AppData, for every test."""
    global EMPTY_PC
    EMPTY_PC = Path(tempfile.mkdtemp(prefix="aihub-tests-pc-"))
    atexit.register(shutil.rmtree, EMPTY_PC, True)
    (EMPTY_PC / "root").mkdir()
    home = EMPTY_PC / "home"
    places = {"USERPROFILE": home, "HOME": home, "APPDATA": home / "AppData" / "Roaming",
              "LOCALAPPDATA": home / "AppData" / "Local"}
    for folder in places.values():
        folder.mkdir(parents=True, exist_ok=True)
    gitconfig = Path.home() / ".gitconfig"
    if "GIT_CONFIG_GLOBAL" not in os.environ and gitconfig.is_file():
        os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)  # git keeps its own settings, whatever HOME is
    for name, folder in places.items():
        os.environ[name] = str(folder)
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):  # a real account's folder
        os.environ.pop(name, None)
    kept = []
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        try:
            with os.scandir(folder or ".") as entries:
                if any(e.name.lower().startswith(NAMES) and os.path.splitext(e.name.lower())[1] in
                       PROGRAM_EXTENSIONS | {""} and not e.is_dir() for e in entries):
                    continue  # a real claude, codex or sbx is there
        except OSError:
            pass
        kept.append(folder)
    os.environ["PATH"] = os.pathsep.join(kept)


def _guard_locate():
    """`locate.find` and `locate.scan` look at nothing of this PC unless the test says where;
    returns what puts the shared state back after a test."""
    try:
        from aihub import locate, routing
    except ImportError:  # the application cannot be imported: the tests will say why
        return None
    real_find, real_scan = locate.find, locate.scan
    REAL.update(find=real_find, scan=real_scan)

    @functools.wraps(real_find)
    def find(program, *, root=None, **where):
        return real_find(program, root=EMPTY_PC / "root" if root is None else root, **where)

    @functools.wraps(real_scan)
    def scan(*, roots=None, root=None, **where):
        return real_scan(roots=[] if roots is None else roots, root=EMPTY_PC / "root" if root is None else root,
                         **where)
    locate.find, locate.scan = find, scan
    available = routing.available()

    def reset():
        routing.set_available(available)
        locate.forget()
    return reset


def _guard_tests(reset):
    """Each test: put the shared state back after it, and fail it when it tried a real program."""
    real_run = unittest.TestCase.run
    REAL["run"] = real_run

    @functools.wraps(real_run)
    def run(self, result=None):
        before = len(BLOCKED)
        try:
            outcome = real_run(self, result)
        finally:
            if reset is not None:
                reset()
        reported = result if result is not None else outcome
        if len(BLOCKED) > before and reported is not None:
            try:
                raise RealProgramBlocked("garde-fou des tests : ce test a tenté de lancer un vrai programme du PC ("
                                         + " ; ".join(BLOCKED[before:]) + ")")
            except RealProgramBlocked:
                reported.addFailure(self, sys.exc_info())
        return outcome
    unittest.TestCase.run = run


def install():
    if REAL:
        return
    _empty_pc()
    REAL["_execute_child"] = subprocess.Popen._execute_child
    subprocess.Popen._execute_child = _guard_execute_child(subprocess.Popen._execute_child)
    _guard_os("system", lambda command: check(command, shell=True))
    _guard_os("startfile", lambda path, *args, **kwargs: check([path]))
    for name in ("spawnv", "spawnve"):
        _guard_os(name, lambda mode, path, argv=(), *rest, **kwargs: _check_path_argv(path, argv))
    for name in ("execv", "execve", "posix_spawn", "posix_spawnp"):
        _guard_os(name, _check_path_argv)
    _guard_tests(_guard_locate())
