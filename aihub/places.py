"""Where AI Hub may read and write on this PC (1.2.3): one rule for every file and folder.

Every path the app is given is judged here, whatever it is for: a working folder,
a vault, a folder to create in or to save a conversation to, a file to put in a
project's memory, to attach to a request or to import.

1. A local disk of the server only. A network path (\\\\serveur\\partage) is refused
   before anything looks at it: resolving it would make Windows connect to that
   machine, and could hand it the PC's sign-in.
2. Resolved (links followed to where they lead), then inside the allowed folders of
   the settings when the settings name some; any local disk otherwise.
3. What it must be: an existing folder, an existing file of the expected kinds and
   size, or a new name inside an allowed folder.

The owner's own settings are the one exception: the allowed folders themselves,
the backups and the settings' vault are chosen on any local disk (`Places()`
without roots, from the settings window only).
"""

import os
import re
from pathlib import Path

from .store import StoreError

_LOCAL = re.compile(r"^[A-Za-z]:[\\/]")
_BAD_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)


def local(raw):
    """Refused before anything is looked at: on Windows, only a local disk's path (C:\\…)."""
    if os.name == "nt" and not _LOCAL.match(str(raw)):
        raise StoreError("choisissez un dossier d'un disque local du serveur")


class Places:
    """The allowed folders of the settings (`roots`); none: every local disk."""

    def __init__(self, roots=()):
        self.roots = [Path(r).resolve() for r in roots]

    @property
    def limited(self):
        return bool(self.roots)

    def allows(self, path):
        """Already resolved `path`, inside the allowed folders (or no limit)."""
        path = Path(path)
        return not self.roots or any(path == r or path.is_relative_to(r) for r in self.roots)

    def _resolve(self, raw, what):
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 1000 or "\x00" in raw:
            raise StoreError(f"indiquez un chemin complet pour le {what}")
        raw = raw.strip()
        # Before anything resolves it: a network or drive form (\\serveur, \dossier, C:dossier) is refused
        # for what it is. An absolute path on Windows starts with one of these, so none gets past.
        if raw[:1] in ("\\", "/") or re.match(r"^[A-Za-z]:", raw):
            local(raw)
        path = Path(raw).expanduser()  # reading the text only: nothing on the disk or the network yet
        if not path.is_absolute() or (os.name == "nt" and not path.drive):
            raise StoreError(f"indiquez un chemin complet pour le {what}")
        try:
            path = path.resolve()
        except OSError:
            raise StoreError(f"ce {what} n'existe pas : {raw}") from None
        local(str(path))  # a link to a network share is refused as well
        if not self.allows(path):
            allowed = ", ".join(str(r) for r in self.roots)
            raise StoreError(f"{what} non autorisé : {path} (autorisés : {allowed})")
        return path

    def folder(self, raw, what="dossier"):
        """An existing folder, allowed."""
        path = self._resolve(raw, what)
        if not path.is_dir():
            raise StoreError(f"ce {what} n'existe pas : {path}")
        return path

    def file(self, raw, what="fichier", *, extensions=None, max_size=None):
        """An existing regular file, allowed, of one of the `extensions` (".md"…) and at most `max_size` bytes."""
        path = self._resolve(raw, what)
        if not path.is_file():
            raise StoreError(f"ce {what} n'existe pas : {path}")
        if extensions is not None and path.suffix.lower() not in extensions:
            raise StoreError(f"ce {what} n'est pas d'un genre accepté ici ({', '.join(sorted(extensions))})")
        if max_size is not None and path.stat().st_size > max_size:
            raise StoreError(f"{what} trop gros ({max_size // 1024 // 1024 or 1} Mo au plus)")
        return path

    def child(self, parent, name, what="dossier"):
        """`name` (a name, not a path) inside the allowed folder `parent`: where to make something new."""
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or _BAD_NAME.search(name.strip())
                or name.strip() in (".", "..") or name.strip().endswith((" ", "."))
                or _RESERVED.fullmatch(name.strip())):
            raise StoreError("choisissez un nom simple, sans / \\ : * ? \" < > |")
        return self.folder(parent, what) / name.strip()


def free(path):
    """`path`, else « name (2).ext », « (3) »… : the first name not taken in its folder."""
    path = Path(path)
    if not path.exists():
        return path
    for n in range(2, 1000):
        other = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not other.exists():
            return other
    raise StoreError("trop de fichiers du même nom dans ce dossier")
