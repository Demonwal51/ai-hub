"""Obsidian vaults on this PC: those AI Hub created, those Obsidian knows, those found on the disk.

A vault is a folder holding `.obsidian`. Three sources, merged by path:

- AI Hub's own list of the vaults it created (`<base>-coffres-crees.json`,
  beside the base), the one thing this module writes;
- Obsidian's list (`obsidian.json` in its settings folder), vaults opened or
  created in the Obsidian app;
- a bounded walk of the disk (the user's folders, the drives' first levels,
  and around the vaults already known), for vaults made by hand or another tool.

The owner opens a vault to read its notes in the app: reading only, and
nothing of it goes to an AI.
"""

import json
import os
import string
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import obsidian

SOURCES = ("reglages", "aihub", "obsidian", "disque")
SKIP = {"node_modules", "__pycache__", "site-packages", "venv", "AppData", "Application Data", "Windows",
        "Program Files", "Program Files (x86)", "ProgramData", "System Volume Information", "Library",
        "Temp", "tmp", "proc", "sys", "dev", "usr", "bin", "lib", "snap", "var"}
MAX_DIRS = 20000         # folders looked at in one walk, at most
MAX_SECONDS = 3.0        # and for how long
MAX_NOTES = 5000         # notes listed in one vault
MAX_COUNT = 20000        # files counted for the number of notes
MAX_NOTE_BYTES = 512 * 1024
CACHE_S = 60
_cache = {}
_lock = threading.Lock()


def is_vault(path):
    try:
        return Path(path).is_dir() and (Path(path) / ".obsidian").is_dir()
    except OSError:
        return False


def _key(path):
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


# --- AI Hub's own list --------------------------------------------------------------

def record_file(db_path):
    base = Path(db_path).resolve()
    return base.with_name(f"{base.stem}-coffres-crees.json")


def created(path_of_record):
    try:
        data = json.loads(Path(path_of_record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [v for v in data.get("vaults", []) if isinstance(v, dict) and isinstance(v.get("path"), str)]


def record_created(path_of_record, vault_path):
    """Add a vault AI Hub just created to its list (written aside, then swapped)."""
    with _lock:
        known = [v for v in created(path_of_record) if _key(v["path"]) != _key(vault_path)]
        known.append({"path": str(Path(vault_path).resolve()),
                      "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        target = Path(path_of_record)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps({"vaults": known}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, target)
    _cache.clear()


# --- Obsidian's list ----------------------------------------------------------------

def obsidian_lists():
    """Where the Obsidian app keeps its list of vaults, on this system."""
    home = Path.home()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "obsidian" / "obsidian.json"] if appdata else []
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "obsidian" / "obsidian.json"]
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [config / "obsidian" / "obsidian.json",
            home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
            home / "snap" / "obsidian" / "current" / ".config" / "obsidian" / "obsidian.json"]


def registered(lists):
    """The vault folders in Obsidian's lists; an unreadable list counts as empty."""
    found = []
    for path in lists:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in (data.get("vaults") or {}).values():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                found.append(entry["path"])
    return found


# --- the disk -------------------------------------------------------------------------

def _fixed_drives():
    """Windows: the local fixed drives (no network, removable or optical one: they can hang)."""
    if os.name != "nt":
        return []
    import ctypes
    drives = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i):
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(root)
    return drives


def default_roots():
    """(folder, depth) walked when nothing else is said: the user's folders, then each drive's first levels."""
    home = Path.home()
    roots = [(home, 3)]
    roots += [(Path(d), 2) for d in _fixed_drives()]
    if os.name != "nt":
        roots += [(Path(p), 2) for p in ("/mnt", "/media") if Path(p).is_dir()]
    return roots


def walk(roots, max_dirs=MAX_DIRS, max_seconds=MAX_SECONDS):
    """Vault folders under `roots` [(folder, depth)]. Returns (found, complete).

    Breadth first, bounded in folders and time; hidden and system folders are
    skipped, links are not followed, and a vault is not looked into."""
    found, seen = [], set()
    queue = [(Path(r), int(d)) for r, d in roots]
    looked = 0
    deadline = time.monotonic() + max_seconds
    while queue:
        if looked >= max_dirs or time.monotonic() > deadline:
            return found, False
        folder, depth = queue.pop(0)
        key = _key(folder)
        if key in seen:
            continue
        seen.add(key)
        looked += 1
        if is_vault(folder):
            found.append(str(folder))
            continue
        if depth <= 0:
            continue
        try:
            with os.scandir(folder) as entries:
                children = [Path(e.path) for e in entries
                            if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$", "~"))
                            and e.name not in SKIP]
        except OSError:
            continue
        queue.extend((child, depth - 1) for child in sorted(children, key=lambda p: p.name.lower()))
    return found, True


# --- all together ----------------------------------------------------------------------

def count_notes(vault):
    """How many notes (.md) the vault holds, outside its hidden folders; None past MAX_COUNT files."""
    total = looked = 0
    for folder, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        looked += len(files)
        if looked > MAX_COUNT:
            return None
        total += sum(1 for f in files if f.lower().endswith(".md"))
    return total


def discover(*, configured=None, record=None, lists=None, roots=None, extra_roots=(), refresh=False):
    """Every vault found, the active one first, then by name.

    `lists`: Obsidian's lists (None: this system's); `roots`: the folders
    walked (None: `default_roots()`), plus `extra_roots`. Kept a minute."""
    lists = obsidian_lists() if lists is None else lists
    key = (configured, str(record), tuple(map(str, lists)), repr(roots), repr(extra_roots))
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    entries = {}

    def add(path, source):
        k = _key(path)
        entry = entries.setdefault(k, {"path": str(Path(path)), "sources": set()})
        entry["sources"].add(source)

    if configured:
        add(configured, "reglages")
    for item in created(record) if record else []:
        add(item["path"], "aihub")
    for path in registered(lists):
        add(path, "obsidian")
    walked = list(default_roots() if roots is None else roots) + list(extra_roots)
    # Around the vaults known: their brothers often are vaults too.
    walked += [(Path(e["path"]).parent, 1) for e in list(entries.values())]
    found, complete = walk(walked)
    for path in found:
        if _key(path) not in entries:  # "found on the disk": known by nothing else
            add(path, "disque")

    vaults = []
    for k, entry in entries.items():
        path = entry["path"]
        active = bool(configured) and k == _key(configured)
        # AI Hub writes its notes into any folder: the one of the settings counts even without .obsidian.
        exists = is_vault(path) or (active and Path(path).is_dir())
        vaults.append({"path": str(Path(path).resolve()) if exists else path,
                       "name": Path(path).name or path,
                       "sources": [s for s in SOURCES if s in entry["sources"]],
                       "exists": exists,
                       "active": active,
                       "notes": count_notes(path) if exists else None})
    vaults.sort(key=lambda v: (not v["active"], not v["exists"], v["name"].lower(), v["path"].lower()))
    result = {"vaults": vaults, "complete": complete}
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


# --- reading a vault ---------------------------------------------------------------------

def _vault(path, configured=None):
    """The vault's folder: one holding .obsidian, or the one of the settings (AI Hub's notes)."""
    if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
        raise obsidian.ObsidianError("indiquez le chemin complet du coffre")
    settings = bool(configured) and _key(path) == _key(configured) and Path(path).is_dir()
    if not (is_vault(path) or settings):
        raise obsidian.ObsidianError(f"ce dossier n'est pas un coffre Obsidian : {path}")
    return Path(path).resolve()


def list_notes(vault, configured=None):
    """The vault's notes: relative path (with /), size and change time, outside hidden folders."""
    root = _vault(vault, configured)
    notes, complete = [], True
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            path = Path(folder) / name
            try:
                if root not in path.resolve().parents:
                    continue  # a link out of the vault
                stat = path.stat()
            except OSError:
                continue
            if len(notes) >= MAX_NOTES:
                complete = False
                break
            notes.append({"path": path.relative_to(root).as_posix(), "size": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
        if not complete:
            break
    notes.sort(key=lambda n: n["path"].lower())
    return {"vault": str(root), "name": root.name, "notes": notes, "complete": complete}


def read_note(vault, note, configured=None):
    """One note's text, kept inside the vault; the end is cut past MAX_NOTE_BYTES."""
    root = _vault(vault, configured)
    if any(part.startswith(".") for part in Path(str(note)).parts):
        raise obsidian.ObsidianError("les dossiers cachés du coffre ne s'ouvrent pas ici")
    path = obsidian.resolve(str(root), note)
    if not path.is_file():
        raise obsidian.ObsidianError(f"note introuvable : {note}")
    size = path.stat().st_size
    with open(path, "rb") as fh:
        raw = fh.read(MAX_NOTE_BYTES)
    return {"vault": str(root), "path": path.relative_to(root).as_posix(),
            "content": raw.decode("utf-8", errors="replace"), "size": size,
            "cut": size > MAX_NOTE_BYTES,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
