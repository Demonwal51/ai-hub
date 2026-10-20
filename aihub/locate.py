"""Finding the programs the hub starts (1.3.0): Claude Code, Codex and the sbx of Docker Sandboxes.

A program is searched for when it is needed, never kept from the launch: the Codex app
copies its CLI to %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<id>\\, a folder whose name changes
with each update, and a path kept for the life of the server ends up pointing at nothing
(« [WinError 2] Le fichier spécifié est introuvable »).

`find`, in this order: the path given at launch (--claude, --codex), the one kept in
Réglages (config.json), the PATH (a native .exe before a .cmd), then the places the
installers use, the newest copy first. A path given or kept that has gone is said
(`Found.note`) and passed over. What is found is kept a few minutes; `start` checks the
file before each start: gone, the program is searched again, and a start that still
finds no file searches once more, no further.

`scan` looks further, for « Chercher sur ce PC » (Réglages): the user's folder, the
programs' folders and the other local disks, bounded in time and depth, never a network
disk nor a link.

An isolated space runs the VM's own CLI (`claude`, `codex`): nothing here applies to it.
"""

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .providers.claude_code import NO_WINDOW, ProviderError, clean_env

CACHE_S = 300        # a program found is searched again after this long (a new install shows)
SCAN_SECONDS = 45    # the walk of « Chercher sur ce PC », at most; the versions are asked after
SCAN_DIRS = 300000   # folders looked at by one scan, at most
VERSION_S = 5        # « --version » of one copy, at most
HOME_DEPTH = 6       # AppData\Local\OpenAI\Codex\bin\<id> is 6 folders down the user's folder
OTHER_DEPTH = 4      # Program Files and the root of each local disk
SCAN_VERSIONS = 24   # copies asked their version, at most

_LOCAL = re.compile(r"^[A-Za-z]:[\\/]")
_VERSION = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+(?:\.\d+)?)")
_SCRIPTS = (".cmd", ".bat")
# Never walked into: heavy, rebuilt, or not the user's programs.
_SKIP = {"node_modules", ".git", "__pycache__", "site-packages", ".venv", "venv", "windows", "windowsapps",
         "system volume information", "recovery", "perflogs", "temp", "tmp", "inetcache", ".cache", ".npm",
         ".nuget", ".gradle", ".m2", ".rustup", "proc", "sys", "dev"}
_ENV_NAMES = ("PATH", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "CODEX_HOME")
_EDITORS = (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".antigravity")


@dataclass(frozen=True)
class Program:
    key: str
    label: str
    setting: str    # its path in config.json
    searched: str   # what the message says was looked at
    install: str    # what to do when there is none


PROGRAMS = {
    "claude": Program("claude", "Claude Code", "claude_cli",
                      "réglage, PATH, application Claude, installeur officiel, npm, éditeurs",
                      "Installez Claude Code (application Claude, ou « npm i -g @anthropic-ai/claude-code »)"),
    "codex": Program("codex", "Codex", "codex_cli", "réglage, PATH, application Codex, npm, éditeurs",
                     "Installez l’application Codex ou « npm i -g @openai/codex »"),
    "sbx": Program("sbx", "Docker Sandboxes", "sandbox_cli", "réglage, PATH, Docker Sandboxes",
                   "Installez Docker Sandboxes"),
}
PROVIDERS = {"claude_code": "claude", "codex": "codex"}  # the program of each AI provider

# Where a copy comes from, as the app says it.
ORIGINS = {
    "option": "option de lancement",
    "setting": "réglage",
    "given": "commande donnée au serveur",
    "path": "PATH",
    "claude_app": "application Claude",
    "codex_app": "application Codex",
    "native": "installeur officiel",
    "npm": "npm",
    "nvm": "nvm",
    "winget": "winget",
    "scoop": "Scoop",
    "cargo": "Cargo",
    "homebrew": "Homebrew",
    "system": "dossier du système",
    "editor": "extension d’éditeur",
    "docker": "Docker Sandboxes",
    "scan": "recherche sur le PC",
}


@dataclass
class Found:
    program: str
    path: str
    origin: str
    note: str | None = None      # a path given or kept that has gone
    version: str | None = None

    @property
    def where(self):
        return ORIGINS.get(self.origin, self.origin)

    def view(self):
        return {"program": self.program, "path": self.path, "origin": self.origin, "where": self.where,
                "version": self.version, "note": self.note}


class NotFound(ProviderError):
    """Nothing to start: the message says what was looked at and what to do."""

    def __init__(self, program, notes=()):
        self.program = program
        spec = PROGRAMS[program]
        super().__init__(" ".join([*notes, f"{spec.label} introuvable sur ce PC (cherché : {spec.searched}…). "
                                   f"{spec.install}, ou lancez « Chercher sur ce PC » dans Réglages › "
                                   "IA proposées et sauvegardes."]))


class Command(list):
    """A program's command line as the search found it: a list like any command, which
    also says where it comes from and can search again (its file gone)."""

    def __init__(self, found, search):
        super().__init__([found.path])
        self.found = found
        self._search = search

    def again(self):
        """The command of a new search, the cache passed over; NotFound when nothing is left."""
        return Command(self._search(), self._search)


def keep(command):
    """What a run keeps of its command: a Command as it is (it can search again), any other a list."""
    return command if isinstance(command, Command) else list(command)


# --- the search -------------------------------------------------------------------------

def _dir(value):
    return Path(value) if value else None


def _home(env, home, system):
    if home is not None:
        return Path(home)
    value = env.get("USERPROFILE") if system == "nt" else env.get("HOME")
    return Path(value) if value else Path.home()


def _codex_home(env, home, codex_home):
    if codex_home is not None:
        return Path(codex_home)
    return Path(env.get("CODEX_HOME") or home / ".codex")


def _script(path):
    return Path(path).suffix.lower() in _SCRIPTS


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0


def _version_key(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except (AttributeError, ValueError):
        return ()


def _version_in(path):
    """The version a folder of the path names (claude-code\\2.1.299, an editor's extension), or None."""
    for part in reversed(Path(path).parts[:-1]):
        match = _VERSION.search(part)
        if match:
            return match.group(1)
    return None


def refused(program, path):
    """Why this file cannot be the program to start, or None."""
    path = Path(path)
    if any(part.lower() == "windowsapps" for part in path.parts):
        # The Store app's own copies: another program is refused access to them.
        return "une copie de WindowsApps ne peut pas être lancée par un autre programme"
    if not path.name.lower().startswith(program):
        return f"choisissez le programme {program} lui-même"
    folder = path.parent
    if ((folder / "Update.exe").exists() or (folder / "icudtl.dat").exists()
            or (folder / "resources" / "app.asar").exists()):
        # Claude.exe of the Claude app, Codex.exe of the Codex app: their window, not their CLI.
        return "c’est l’application de bureau, pas sa ligne de commande"
    return None


def _usable(program, path, system):
    path = Path(path)
    if not path.is_file() or refused(program, path) is not None:
        return False
    return system == "nt" or os.access(path, os.X_OK)


def _glob(folder, pattern):
    if folder is None:
        return []
    try:
        return list(Path(folder).glob(pattern))
    except OSError:
        return []


def _places(program, *, system, env, home, codex_home, root):
    """(origin, folder, pattern, versioned) of the places the installers use; `versioned`:
    only a copy in a folder named by its version counts."""
    out = []
    editors = [home / name / "extensions" for name in _EDITORS]
    if system == "nt":
        appdata, local = _dir(env.get("APPDATA")), _dir(env.get("LOCALAPPDATA"))
        npm = appdata / "npm" if appdata else None
        links = local / "Microsoft" / "WinGet" / "Links" if local else None
        if program == "claude":
            out += [("claude_app", appdata / "Claude" / "claude-code" if appdata else None, "*/*/claude.exe", True),
                    ("native", home / ".local" / "bin", "claude.exe", False),
                    ("winget", links, "claude.exe", False),
                    ("scoop", home / "scoop" / "shims", "claude.exe", False),
                    ("npm", npm, "claude.cmd", False)]
            out += [("editor", e, "anthropic.claude-code-*/resources/native-binary/claude.exe", False) for e in editors]
        elif program == "codex":
            package = npm / "node_modules" / "@openai" / "codex" if npm else None
            out += [("codex_app", local / "OpenAI" / "Codex" / "bin" if local else None, "*/codex.exe", False),
                    ("codex_app", codex_home / "plugins" / ".plugin-appserver", "codex.exe", False),
                    # Left behind by older apps (0.144): taken only when nothing is newer.
                    ("codex_app", codex_home / ".sandbox-bin", "codex.exe", False),
                    ("npm", package, "vendor/*/codex/codex.exe", False),
                    ("npm", package, "node_modules/@openai/codex-*/vendor/*/codex/codex.exe", False),
                    ("npm", npm, "codex.cmd", False),
                    ("winget", links, "codex.exe", False),
                    ("scoop", home / "scoop" / "shims", "codex.exe", False),
                    ("cargo", home / ".cargo" / "bin", "codex.exe", False)]
            out += [("editor", e, "openai.chatgpt-*/bin/*/codex.exe", False) for e in editors]
        else:
            out += [("docker", local / "DockerSandboxes" / "bin" if local else None, "sbx.exe", False)]
        return out
    root = Path(root or "/")
    common = [("native", home / ".local" / "bin", program, False),
              ("npm", home / ".npm-global" / "bin", program, False),
              ("nvm", home / ".nvm" / "versions" / "node", f"*/bin/{program}", False),
              ("system", root / "usr" / "local" / "bin", program, False),
              ("homebrew", root / "opt" / "homebrew" / "bin", program, False)]
    if program == "claude":
        out += common + [("native", home / ".claude" / "local", "claude", False)]
        out += [("editor", e, "anthropic.claude-code-*/resources/native-binary/claude", False) for e in editors]
    elif program == "codex":
        out += common + [("cargo", home / ".cargo" / "bin", "codex", False),
                         ("codex_app", codex_home / ".sandbox-bin", "codex", False)]
        out += [("editor", e, "openai.chatgpt-*/bin/*/codex", False) for e in editors]
    else:
        out += [c for c in common if c[0] in ("native", "system", "homebrew")]
    return out


@dataclass
class _Copy:
    index: int      # its place in _places: the first wins a tie
    origin: str
    path: Path
    version: str | None
    mtime: float


def _known(program, *, system, env, home, codex_home, root, every=False):
    """The copies in the installers' places, each place's best one (see `_best`); `every`: all of them."""
    best, every_copy = {}, []
    for index, (origin, folder, pattern, versioned) in enumerate(
            _places(program, system=system, env=env, home=home, codex_home=codex_home, root=root)):
        for path in _glob(folder, pattern):
            version = _version_in(path) if origin in ("claude_app", "editor") else None
            if (versioned and version is None) or not _usable(program, path, system):
                continue
            copy = _Copy(index, origin, path, version, _mtime(path))
            every_copy.append(copy)
            kept = best.get(index)
            # Within one place, the highest version (claude-code\2.1.299 over 2.1.295), then the newest file.
            if kept is None or (_version_key(copy.version), copy.mtime) > (_version_key(kept.version), kept.mtime):
                best[index] = copy
    return every_copy if every else list(best.values())


def _best(copies):
    """Between places: a native copy before a script (.cmd passes its arguments through
    cmd.exe, which cuts them at a line break), then the newest file, then the first place."""
    return max(copies, key=lambda c: (not _script(c.path), c.mtime, -c.index))


def _on_path(program, *, system, env):
    """The program on the PATH, WindowsApps left out (`refused`): a native .exe anywhere on it before a .cmd.

    Only the PATH's folders given in full: never the current folder, where the hub runs and
    its agents may write, which Windows' search (shutil.which too) looks at first."""
    folders = [d for d in (env.get("PATH") or "").split(os.pathsep) if d and os.path.isabs(d)]
    if system != "nt":
        hit = shutil.which(program, path=os.pathsep.join(folders)) if folders else None
        return hit if hit and refused(program, hit) is None else None
    # What Windows starts by itself (npm's codex.cmd; never its codex.ps1 or its sh script).
    for names in ([f"{program}.exe"], [f"{program}.cmd", f"{program}.bat", f"{program}.com"]):
        for folder in folders:
            for name in names:
                hit = os.path.join(folder, name)
                if os.path.isfile(hit) and refused(program, hit) is None:
                    return hit
    return None


# npm's codex.cmd starts this native copy, installed next to it.
_NPM_NATIVE = {"codex": ("node_modules/@openai/codex/vendor/*/codex/codex.exe",
                         "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/codex/codex.exe")}


def _native_of(program, script, system):
    copies = [p for pattern in _NPM_NATIVE.get(program, ()) for p in _glob(Path(script).parent, pattern)
              if _usable(program, p, system)]
    return max(copies, key=_mtime) if copies else None


def _gone(program, origin, path):
    where = f"donné au lancement (--{program})" if origin == "option" else "retenu dans Réglages"
    return f"Le chemin de {PROGRAMS[program].label} {where} n’existe plus ({path}) : recherche automatique."


def _search(program, option, setting, *, system, env, home, codex_home, root):
    notes = []
    for origin, given in (("option", option), ("setting", setting)):
        if not given:
            continue
        if Path(given).is_file():
            return Found(program, str(given), origin, " ".join(notes) or None)
        notes.append(_gone(program, origin, given))
    note = " ".join(notes) or None
    hit = _on_path(program, system=system, env=env)
    if hit and not _script(hit):
        return Found(program, hit, "path", note)
    copies = _known(program, system=system, env=env, home=home, codex_home=codex_home, root=root)
    if hit:
        # A shim on the PATH (npm's codex.cmd): its own native copy, else a native one of the
        # installers, else the shim itself.
        native = _native_of(program, hit, system)
        if native is not None:
            return Found(program, str(native), "npm", note)
        copies = [c for c in copies if not _script(c.path)]
        if not copies:
            return Found(program, hit, "path", note)
    if copies:
        best = _best(copies)
        return Found(program, str(best.path), best.origin, note, best.version)
    raise NotFound(program, notes)


_cache = {}
_lock = threading.Lock()


def find(program, *, option=None, setting=None, env=None, home=None, codex_home=None, root=None, system=None,
         refresh=False):
    """The copy of `program` (claude, codex, sbx) to start: a Found, else NotFound.

    `option`: the path given at launch; `setting`: the one kept in Réglages. `env`, `home`,
    `codex_home`, `root` (the / of Linux) and `system` (os.name) are this PC's by default
    (tests give their own). Kept CACHE_S, while its file is there; `refresh` searches again."""
    system = system or os.name
    env = os.environ if env is None else env
    home = _home(env, home, system)
    codex_home = _codex_home(env, home, codex_home)
    key = (program, option, setting, system, str(home), str(codex_home), str(root),
           tuple(env.get(name) for name in _ENV_NAMES))
    now = time.monotonic()
    with _lock:
        kept = _cache.get(key)
    if kept is not None and not refresh and now - kept[0] < CACHE_S and Path(kept[1].path).is_file():
        return kept[1]
    found = _search(program, option, setting, system=system, env=env, home=home, codex_home=codex_home, root=root)
    with _lock:
        _cache[key] = (now, found)
    return found


def forget():
    """Every program is searched again at its next use (after « Chercher sur ce PC »)."""
    with _lock:
        _cache.clear()


def command(program, **where):
    """The program's command line (a Command), searched as `find` does with `where`."""
    def search():
        return find(program, refresh=True, **where)
    return Command(find(program, **where), search)


def _no_start(command):
    found = command.found
    if Path(found.path).is_file():
        return ProviderError(f"impossible de lancer {PROGRAMS[found.program].label} ({found.path}) : "
                             "un fichier dont il a besoin est introuvable")
    return NotFound(found.program)


def _cmd_changes(args):
    """True when cmd.exe, which runs a .cmd or a .bat, would not pass this command line as it
    is: it ends it at a line break, puts a variable's value for %NAME%, and runs what follows
    & | < > ^ outside double quotes as other commands (an instruction's text included)."""
    line = subprocess.list2cmdline(args)
    if "\n" in line or "\r" in line or re.search(r"%[^%\s]+%", line):
        return True
    quoted = False
    for char in line:
        if char == '"':
            quoted = not quoted  # cmd.exe knows no \" : each quote opens or closes
        elif char in "&|<>^" and not quoted:
            return True
    return False


def _spawn(spawn, command, tail, options):
    if os.name == "nt" and _script(command[0]) and _cmd_changes([*command, *tail]):
        label = PROGRAMS[command.found.program].label if isinstance(command, Command) else Path(command[0]).name
        raise ProviderError(f"{label} n’est ici qu’un script {Path(command[0]).suffix} : cmd.exe couperait cette "
                            "demande ou en exécuterait une partie (saut de ligne, %, &, |, <, >), elle n’a donc pas "
                            "été lancée. Installez sa version .exe, ou retenez-en une dans Réglages › IA proposées "
                            "et sauvegardes.")
    return spawn([*command, *tail], **options)


def start(command, tail, spawn, **options):
    """`spawn(command + tail, **options)`: subprocess.Popen, subprocess.run or an isolated space's.

    A command the search found is checked first: its file gone (an update of the Codex app),
    the program is searched again. A start that still finds no file searches once more and
    starts once more, no further. Any other command (a test's, an isolated space's) is
    started as it is. A .cmd or .bat (npm's shims) is not started with arguments that
    cmd.exe would cut or run (`_cmd_changes`)."""
    if not isinstance(command, Command):
        return _spawn(spawn, command, tail, options)
    searched = not Path(command[0]).is_file()
    if searched:
        command = command.again()
    try:
        return _spawn(spawn, command, tail, options)
    except FileNotFoundError:
        if searched:
            raise _no_start(command) from None
    command = command.again()
    try:
        return _spawn(spawn, command, tail, options)
    except FileNotFoundError:
        raise _no_start(command) from None


def origin(command):
    """Where a command comes from, for the state of the AIs: {"found", "where", "note"}, no path."""
    if isinstance(command, Command):
        return {"found": True, "where": command.found.where, "note": command.found.note}
    return {"found": True, "where": ORIGINS["given"], "note": None}


def state(program, **where):
    """The copy in use and where it comes from, or why there is none (Réglages)."""
    base = {"program": program, "label": PROGRAMS[program].label, "offered": True}
    try:
        return {**base, "found": True, **find(program, **where).view(), "error": None}
    except NotFound as exc:
        return {**base, "found": False, "path": None, "where": None, "version": None, "note": None,
                "error": str(exc)}


# --- « Chercher sur ce PC » ---------------------------------------------------------------

def _names(program, system):
    return {f"{program}.exe", f"{program}.cmd", f"{program}.bat"} if system == "nt" else {program}


def _key(path):
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _linked(entry):
    """A link, a junction or another reparse point (a OneDrive folder): not walked into."""
    if entry.is_symlink() or entry.is_junction():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def scan_roots(*, system=None, env=None, home=None, root=None):
    """(folder, depth) walked by the scan: the user's folder, the programs' folders, then each
    local disk (Windows: the fixed ones only, never a network, removable or optical one)."""
    system = system or os.name
    env = os.environ if env is None else env
    home = _home(env, home, system)
    if system == "nt":
        from .vaults import _fixed_drives
        local = _dir(env.get("LOCALAPPDATA"))
        roots = [(home, HOME_DEPTH), (local / "Programs" if local else None, OTHER_DEPTH)]
        # A copy of os.environ (a dict) has its names in capitals on Windows.
        roots += [(_dir(env.get(name) or env.get(name.upper())), OTHER_DEPTH)
                  for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
        roots += [(Path(drive), OTHER_DEPTH) for drive in _fixed_drives()]
        # A network folder (\\serveur\partage, a profile kept on a server) is never looked at.
        return [(folder, depth) for folder, depth in roots if folder is not None and _LOCAL.match(str(folder))]
    root = Path(root or "/")
    return [(home, HOME_DEPTH), (root / "opt", OTHER_DEPTH), (root / "usr" / "local", 3), (root / "snap", 2)]


def _walk(roots, names, *, system, deadline, max_dirs, clock):
    """The files named one of `names` under `roots` [(folder, depth)], breadth first.

    Returns (paths, complete): incomplete past `deadline` or `max_dirs` folders. Unreadable
    folders, links and (Linux) another disk mounted inside are passed over."""
    found, seen, looked = [], set(), 0
    queue = deque((Path(folder), int(depth), None) for folder, depth in roots)
    while queue:
        if looked >= max_dirs or clock() > deadline:
            return found, False
        folder, depth, device = queue.popleft()
        key = os.path.normcase(str(folder))
        if key in seen:
            continue
        seen.add(key)
        looked += 1
        children = []
        try:
            if device is None and system != "nt":
                device = folder.stat().st_dev
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        name = entry.name.lower() if system == "nt" else entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if depth > 0 and name.lower() not in _SKIP and not name.startswith("$") \
                                    and not _linked(entry) \
                                    and (system == "nt" or entry.stat(follow_symlinks=False).st_dev == device):
                                children.append(Path(entry.path))
                        elif name in names:
                            found.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue  # unreadable (access denied, gone): passed over
        queue.extend((child, depth - 1, device) for child in sorted(children, key=lambda p: p.name.lower()))
    return found, True


def _version_of(path):
    """The version a copy says (« --version », VERSION_S at most); a .cmd is not asked."""
    if _script(path):
        return None
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=VERSION_S, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                              env=clean_env(os.environ))
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION.search(proc.stdout or "") or _VERSION.search(proc.stderr or "")
    return match.group(1) if match else None


def scan(*, where=None, env=None, home=None, codex_home=None, root=None, system=None, roots=None,
         max_seconds=SCAN_SECONDS, max_dirs=SCAN_DIRS, versions=True, clock=time.monotonic):
    """Every copy of the programs on this PC, for « Chercher sur ce PC » (Réglages).

    The PATH and the installers' places first, then a walk of `roots` [(folder, depth)]
    (None: `scan_roots()`), bounded in depth, in folders and in time. A copy of WindowsApps
    or a desktop app is left out, and the Codex of .sandbox-bin when another is newer. The
    version is asked (« --version ») only of the copies the search could start by itself
    (the PATH, the installers' places): a file met elsewhere is never run. `where`:
    {program: {"option", "setting"}} of the hub, for the copy in use.

    Returns {"programs": [{program, label, setting, in_use, copies}], "complete", "seconds"}."""
    system = system or os.name
    env = os.environ if env is None else env
    where = where or {}
    started = clock()
    home_dir = _home(env, home, system)
    codex_dir = _codex_home(env, home_dir, codex_home)
    copies = {}   # key -> (Found, trusted)

    def add(program, path, origin, *, trusted, version=None):
        key = _key(path)
        if key in copies or not _usable(program, path, system):
            return
        copies[key] = (Found(program, str(path), origin, version=version), trusted)

    for program in PROGRAMS:
        hit = _on_path(program, system=system, env=env)
        if hit:
            add(program, hit, "path", trusted=True)
        for copy in _known(program, system=system, env=env, home=home_dir, codex_home=codex_dir, root=root,
                           every=True):
            add(program, copy.path, copy.origin, trusted=True, version=copy.version)
    owner = {name: program for program in PROGRAMS for name in _names(program, system)}
    walked, complete = _walk(scan_roots(system=system, env=env, home=home_dir, root=root) if roots is None else roots,
                             set(owner), system=system, deadline=started + max_seconds, max_dirs=max_dirs, clock=clock)
    for path in walked:
        add(owner[path.name.lower() if system == "nt" else path.name], path, "scan", trusted=False)
    # The copy each program would start now: the hub runs it anyway, its version may be asked.
    states = {program: state(program, **(where.get(program) or {}), env=env, home=home, codex_home=codex_home,
                             root=root, system=system, refresh=True) for program in PROGRAMS}
    in_use = {program: _key(current["path"]) for program, current in states.items() if current["found"]}
    for program, key in in_use.items():
        if key in copies:
            copies[key] = (copies[key][0], True)
        else:
            add(program, states[program]["path"], states[program]["origin"], trusted=True)
    # The Codex of .sandbox-bin is left behind by older apps: shown only when no copy is newer.
    codexes = [found for found, _ in copies.values() if found.program == "codex"]
    for key, (found, _) in list(copies.items()):
        if found.program == "codex" and ".sandbox-bin" in Path(found.path).parts and key != in_use.get("codex") \
                and any(_mtime(other.path) > _mtime(found.path) for other in codexes if other is not found):
            del copies[key]
    if versions:
        asked = [found for found, trusted in copies.values() if trusted and found.version is None
                 and not _script(found.path)][:SCAN_VERSIONS]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for found, version in zip(asked, pool.map(lambda f: _version_of(f.path), asked)):
                found.version = version
    programs = []
    for program, spec in PROGRAMS.items():
        mine = [found for found, _ in copies.values() if found.program == program]
        # The copy in use first, then the highest versions, then the newest files.
        mine.sort(key=lambda f: (_key(f.path) != in_use.get(program), f.version is None,
                                 [-n for n in _version_key(f.version)], -_mtime(f.path)))
        programs.append({"program": program, "label": spec.label, "setting": spec.setting, "in_use": states[program],
                         "copies": [dict(f.view(), in_use=_key(f.path) == in_use.get(program)) for f in mine]})
    return {"programs": programs, "complete": complete, "seconds": round(clock() - started, 1)}
