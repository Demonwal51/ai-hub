"""One-click update from the releases of AI Hub on GitHub (an install made from the .zip).

- `check()`: the newest release whose file is ai-hub-<version>.zip, newer than
  this one (read at most every 6 hours; GitHub's public API, no account).
- `start()` (the owner's click): downloads that file into data/mise-a-jour/,
  checks it (one folder ai-hub-<version>/ holding aihub/__init__.py of that very
  version, nothing outside it), then starts the helper below, detached, and
  stops the server.
- The helper (`python -m aihub.updater apply …`): waits for the server to be
  gone, keeps the current files in data/mise-a-jour/avant-<version>/, puts the
  new ones (the app's files only: never data/ nor .venv/), installs the
  requirements, and starts the server again as it was started.
A development copy (a folder with .git, like the one this is written in) is
never updated so: git does that.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import __version__

REPO = "Demonwal51/ai-hub"
RELEASES = f"https://api.github.com/repos/{REPO}/releases"
ASSET = re.compile(r"^ai-hub-(\d+\.\d+\.\d+(?:b\d+)?)\.zip$")
MAX_ZIP = 50 * 1024 * 1024
CHECK_EVERY_S = 6 * 3600
# What an update replaces: the app's own files, never the person's (data/, .venv/).
APP_FILES = ("aihub", "requirements.txt", "LICENSE", "README.md", "CHANGELOG.md", "installer.bat", "Lancer AI Hub.bat")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_cache = {}


class UpdateError(Exception):
    """An update that cannot be done; the message is for the person."""


def version_key(text):
    """0.3.0b1 < 0.3.0 < 0.3.1: a beta comes before its version."""
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:b(\d+))?", str(text or ""))
    if not found:
        return None
    major, minor, patch, beta = found.groups()
    return int(major), int(minor), int(patch), 0 if beta else 1, int(beta or 0)


def install_root():
    return Path(__file__).resolve().parents[1]


def why_not_here(root=None):
    """Why this install cannot update itself, or None."""
    root = Path(root or install_root())
    if (root / ".git").exists():
        return "copie de développement (dossier git) : elle se met à jour avec git, pas d'ici"
    if not (root / "requirements.txt").is_file():
        return "installation inconnue (pas de requirements.txt à côté du dossier aihub)"
    return None


def _get(url, timeout=15):
    import httpx
    response = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-hub"})
    response.raise_for_status()
    return response


def latest(releases, current=__version__):
    """The newest release newer than `current`: {version, name, notes, url, size, page}, or None."""
    best = None
    for release in releases or []:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        for asset in release.get("assets") or []:
            found = ASSET.match(str(asset.get("name") or ""))
            key = version_key(found.group(1)) if found else None
            if key is None or key <= (version_key(current) or (0,)):
                continue
            if best is None or key > best[0]:
                best = (key, {"version": found.group(1), "name": release.get("name") or found.group(1),
                              "notes": (release.get("body") or "")[:4000], "url": asset.get("browser_download_url"),
                              "size": asset.get("size"), "page": release.get("html_url")})
    return best[1] if best else None


def check(get=_get, now=time.monotonic, current=__version__):
    """{current, latest (or None), error}; kept a while: the API is asked at most every 6 hours."""
    cached = _cache.get("check")
    if cached and now() - cached[0] < CHECK_EVERY_S and cached[1]["current"] == current:
        return cached[1]
    try:
        found = latest(get(RELEASES).json(), current)
        result = {"current": current, "latest": found, "error": None}
    except Exception as exc:  # no network, GitHub refusing: said, never fatal
        result = {"current": current, "latest": None, "error": f"vérification impossible : {exc}"[:300]}
    _cache["check"] = (now(), result)
    return result


def verify(zip_path, version):
    """The downloaded file is AI Hub of that version, and writes nothing outside its folder."""
    folder = f"ai-hub-{version}/"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            if not names or any(not n.startswith(folder) or ".." in Path(n).parts or Path(n).is_absolute()
                                for n in names):
                raise UpdateError("fichier inattendu : ce n'est pas une version d'AI Hub")
            init = archive.read(folder + "aihub/__init__.py").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        raise UpdateError("fichier inattendu : ce n'est pas une version d'AI Hub") from None
    found = re.search(r'__version__ = "([^"]+)"', init)
    if not found or found.group(1) != version:
        raise UpdateError("le fichier ne contient pas la version annoncée")
    return folder


def download(release, folder, get=_get):
    """The release's file, into `folder`, checked. Returns its path."""
    url = str(release.get("url") or "")
    if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise UpdateError("adresse de téléchargement inattendue")
    if (release.get("size") or 0) > MAX_ZIP:
        raise UpdateError("fichier trop gros pour une version d'AI Hub")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"ai-hub-{release['version']}.zip"
    data = get(url, timeout=120).content
    if len(data) > MAX_ZIP:
        raise UpdateError("fichier trop gros pour une version d'AI Hub")
    target.write_bytes(data)
    try:
        verify(target, release["version"])
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    return target


def start(release, *, server_pid, restart, root=None, get=_get, spawn=None):
    """Download, check, then start the helper that puts the new version in place once the server is gone."""
    root = Path(root or install_root())
    reason = why_not_here(root)
    if reason:
        raise UpdateError(reason)
    zip_path = download(release, root / "data" / "mise-a-jour", get)
    command = [sys.executable, "-m", "aihub.updater", "apply", "--zip", str(zip_path), "--version", release["version"],
               "--root", str(root), "--pid", str(server_pid), "--restart", json.dumps(restart)]
    spawn = spawn or (lambda cmd: subprocess.Popen(cmd, cwd=str(root), creationflags=NO_WINDOW | 0x00000008,
                                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, close_fds=True))
    spawn(command)
    return zip_path


def _alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def apply(zip_path, version, root, *, pid=None, restart=None, pip=True, wait_s=60, run=subprocess.run,
          popen=subprocess.Popen):
    """Put the new version in place (the helper, the server being gone). Returns the backup folder."""
    root = Path(root)
    deadline = time.monotonic() + wait_s
    while pid and _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    work = root / "data" / "mise-a-jour"
    backup = work / f"avant-{version}"
    fresh = work / f"nouvelle-{version}"
    moved = []
    try:
        folder = verify(zip_path, version)
        shutil.rmtree(fresh, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(fresh)
        source = fresh / folder.rstrip("/")
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        for name in APP_FILES:
            if (root / name).exists():
                shutil.move(str(root / name), str(backup / name))
                moved.append(name)
        for name in APP_FILES:
            if (source / name).exists():
                shutil.move(str(source / name), str(root / name))
        if pip:
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run([str(python if python.exists() else sys.executable), "-m", "pip", "install", "--quiet",
                 "--disable-pip-version-check", "-r", str(root / "requirements.txt")], cwd=str(root),
                creationflags=NO_WINDOW, check=True)
    except Exception:
        # Half done: the version that worked goes back in place, as it was.
        for name in APP_FILES:
            if name in moved:
                target = root / name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                shutil.move(str(backup / name), str(target))
        raise
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        if restart:
            popen(restart, cwd=str(root), creationflags=NO_WINDOW | 0x00000008, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aihub.updater")
    sub = parser.add_subparsers(dest="command", required=True)
    step = sub.add_parser("apply")
    step.add_argument("--zip", required=True)
    step.add_argument("--version", required=True)
    step.add_argument("--root", required=True)
    step.add_argument("--pid", type=int)
    step.add_argument("--restart", default="null")
    args = parser.parse_args(argv)
    log = Path(args.root) / "data" / "mise-a-jour" / "journal.txt"
    try:
        backup = apply(args.zip, args.version, args.root, pid=args.pid, restart=json.loads(args.restart))
        message = f"mise à jour vers {args.version} faite ; l'ancienne version est dans {backup}"
    except Exception as exc:  # the server is stopped: the journal says why
        message = f"mise à jour vers {args.version} refusée : {exc}"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
