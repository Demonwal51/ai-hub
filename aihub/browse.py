"""Folders and files of the server PC, shown in the app's own picker (no Windows dialog).

The app is used from a phone or another PC as well: a path on the server can
only be chosen in a window the app draws itself. Two views, reading only:

- `folders`: the folders of a folder, to choose a working folder, a vault, a
  backup folder… Limited to the allowed folders when the settings name some
  (`roots`), unless the caller is the owner choosing a setting.
- `entries`: the files and folders inside a working folder, as paths relative
  to it, to pick what an isolated space receives.

Links are not followed out of where the view is limited; names only, never
the content of a file.
"""

import os
import re
from pathlib import Path

from .store import StoreError
from .vaults import _fixed_drives, is_vault

MAX_ENTRIES = 2000       # shown in one folder, at most


def local_path(raw):
    """Refused before anything is looked at: on Windows, only a local disk's path (C:\\…).

    A network path (\\\\serveur\\partage) is never even resolved: Windows would
    then connect to that machine, and could hand it the PC's sign-in."""
    if os.name == "nt" and not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise StoreError("choisissez un dossier d'un disque local du serveur")


def _absolute(path):
    if not isinstance(path, str) or not path.strip() or len(path) > 1000 or "\x00" in path:
        raise StoreError("indiquez un chemin complet")
    local_path(path.strip())
    p = Path(path.strip()).expanduser()
    if not p.is_absolute() or (os.name == "nt" and not p.drive):
        raise StoreError("indiquez un chemin complet")
    try:
        p = p.resolve()
    except OSError:
        raise StoreError(f"dossier introuvable : {path}") from None
    if not p.is_dir():
        raise StoreError(f"ce dossier n'existe pas : {p}")
    return p


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


def folders(path=None, roots=()):
    """The folders in `path` (None: the starting places). `roots`: the limit, if any."""
    allowed = [Path(r).resolve() for r in roots]
    if not path:
        return {"path": None, "parent": None, "starts": starts(allowed), "entries": [], "complete": True,
                "vault": False, "limited": bool(allowed)}
    here = _absolute(path)
    if allowed and not _inside(here, allowed):
        raise StoreError("ce dossier est hors des dossiers autorisés")
    entries, complete = [], True
    try:
        with os.scandir(here) as it:
            items = sorted((e for e in it if not e.name.startswith((".", "$"))), key=lambda e: e.name.lower())
    except PermissionError:
        raise StoreError(f"accès refusé par Windows à ce dossier : {here}") from None
    except OSError as exc:
        raise StoreError(f"lecture impossible de {here} : {exc.strerror or exc}") from None
    for entry in items:
        try:
            if not entry.is_dir():
                continue
            target = Path(entry.path).resolve()
        except OSError:
            continue
        if allowed and not _inside(target, allowed):
            continue  # a link out of the allowed folders
        if len(entries) >= MAX_ENTRIES:
            complete = False
            break
        entries.append({"name": entry.name, "path": str(Path(entry.path)), "vault": is_vault(entry.path)})
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
