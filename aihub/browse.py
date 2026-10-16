"""Folders and files of the server PC, shown in the app's own picker (no Windows dialog).

The app is used from a phone or another PC as well: a path on the server can
only be chosen in a window the app draws itself. Two views, reading only:

- `folders`: the folders of a folder, to choose a working folder, a vault, a
  backup folder… and, when asked (`files`), its files of the kinds wanted, to
  choose a file to read. Limited by aihub/places.py, the one rule: the allowed
  folders when the settings name some (`roots`), unless the caller is the owner
  choosing a setting.
- `entries`: the files and folders inside a working folder, as paths relative
  to it, to pick what an isolated space receives.

Links are not followed out of where the view is limited; names only, never
the content of a file.
"""

import os
import re
from pathlib import Path

from . import places
from .store import StoreError
from .vaults import _fixed_drives, is_vault

MAX_ENTRIES = 2000       # shown in one folder, at most
MAX_SCAN = 10000         # read in one folder, at most (a huge Temp stays quick)


# A network path is never even resolved (aihub/places.py says why).
local_path = places.local


def _absolute(path):
    return places.Places().folder(path)


def _inside(path, roots):
    return any(path == r or path.is_relative_to(r) for r in roots)


def starts(roots=()):
    """Where the picker begins: the allowed folders, or the disks and the user's usual folders."""
    if roots:
        return [{"name": str(r), "path": str(r)} for r in roots if r.is_dir()]
    home = Path.home()
    places = [("Dossier personnel", home)]
    places += [(name, home / folder) for name, folder in (("Bureau", "Desktop"), ("Documents", "Documents"))]
    if os.name == "nt":
        places += [(f"Disque {d[:2]}", Path(d)) for d in _fixed_drives()]
    else:
        places.append(("Racine /", Path("/")))
    return [{"name": name, "path": str(p)} for name, p in places if p.is_dir()]


def folders(path=None, roots=(), files=None):
    """The folders in `path` (None: the starting places). `roots`: the limit, if any.

    `files`: also its files, of these kinds (".md"…, or "*" for any): to choose a file."""
    rule = places.Places(roots)
    allowed = rule.roots
    if not path:
        return {"path": None, "parent": None, "starts": starts(allowed), "entries": [], "complete": True,
                "vault": False, "limited": bool(allowed)}
    here = rule.folder(path)  # local, allowed, existing: or said why not
    entries, complete = [], True
    try:
        with os.scandir(here) as it:
            # A folder of tens of thousands of entries (a Temp) is read up to MAX_SCAN of them, then said cut.
            items = []
            for e in it:
                if len(items) >= MAX_SCAN:
                    complete = False
                    break
                if not e.name.startswith((".", "$")):
                    items.append(e)
            items.sort(key=lambda e: e.name.lower())
    except PermissionError:
        raise StoreError(f"accès refusé par Windows à ce dossier : {here}") from None
    except OSError as exc:
        raise StoreError(f"lecture impossible de {here} : {exc.strerror or exc}") from None
    for entry in items:
        try:
            if entry.is_dir():
                kind = "dir"
            elif files is not None and entry.is_file() and (files == "*" or Path(entry.name).suffix.lower() in files):
                kind = "file"
            else:
                continue
            target = Path(entry.path).resolve()
            size = entry.stat().st_size if kind == "file" else None
        except OSError:
            continue
        if allowed and not _inside(target, allowed):
            continue  # a link out of the allowed folders
        if len(entries) >= MAX_ENTRIES:
            complete = False
            break
        entries.append({"name": entry.name, "path": str(Path(entry.path)), "kind": kind,
                        **({"size": size} if kind == "file" else {"vault": is_vault(entry.path)})})
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))  # the folders first
    parent = here.parent if here.parent != here else None
    if parent is not None and allowed and not _inside(parent, allowed):
        parent = None
    return {"path": str(here), "parent": str(parent) if parent else None, "starts": starts(allowed),
            "entries": entries, "complete": complete, "vault": is_vault(here), "limited": bool(allowed)}


def entries(base, relative=""):
    """Files and folders under `relative` in the working folder `base`, as paths relative to it."""
    root = _absolute(base)
    raw = (relative or "").strip()
    # Judged as it came, before any change: "\\serveur", "/x", "C:x" and ".." never go further.
    if raw and (raw.startswith(("/", "\\")) or ":" in raw or ".." in re.split(r"[\\/]", raw)):
        raise StoreError("indiquez un chemin relatif au dossier de travail")
    relative = raw.replace("\\", "/").strip("/")
    here = (root / relative).resolve() if relative else root
    if not _inside(here, [root]) or not here.is_dir():
        raise StoreError("ce dossier n'est pas dans le dossier de travail")
    found, complete = [], True
    try:
        with os.scandir(here) as it:
            items = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as exc:
        raise StoreError(f"lecture impossible de {here} : {exc.strerror or exc}") from None
    for entry in items:
        try:
            kind = "dir" if entry.is_dir() else "file" if entry.is_file() else None
            if kind is None or not _inside(Path(entry.path).resolve(), [root]):
                continue  # neither a file nor a folder, or a link out of the working folder
            size = entry.stat().st_size if kind == "file" else None
        except OSError:
            continue
        if len(found) >= MAX_ENTRIES:
            complete = False
            break
        found.append({"name": entry.name, "path": Path(entry.path).relative_to(root).as_posix(),
                      "kind": kind, "size": size})
    parent = here.parent.relative_to(root).as_posix() if here != root else None
    return {"base": str(root), "path": here.relative_to(root).as_posix() if here != root else "",
            "parent": "" if parent == "." else parent, "entries": found, "complete": complete}
