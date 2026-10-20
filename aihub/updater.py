"""One-click update from the releases of AI Hub on GitHub (an install made from the .zip).

- `check()`: the newest release whose file is ai-hub-<version>.zip, newer than
  this one (read at most every 6 hours; GitHub's public API, no account).
- `start()` (the owner's click): downloads that file into data/mise-a-jour/,
  checks it (its SHA256 is the one the release's SHA256SUMS gives; one folder
  ai-hub-<version>/ holding aihub/__init__.py of that very version, nothing
  outside it), then starts the helper below, detached, and stops the server.
- The helper (`python -m aihub.updater apply …`): waits for the server to be
  gone, keeps the current files in data/mise-a-jour/avant-<version>/, puts the
  new ones (the app's files only: never data/ nor .venv/), installs the
  requirements, and starts the server again as it was started. A new version
  that does not answer is undone (files, base, dependencies); why goes to
  data/mise-a-jour/journal.txt.
A development copy (a folder with .git, like the one this is written in) is
never updated so: git does that.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
import stat
from pathlib import Path, PurePosixPath

from . import __version__

REPO = "Demonwal51/ai-hub"
RELEASES = f"https://api.github.com/repos/{REPO}/releases"
ASSET = re.compile(r"^ai-hub-(\d+\.\d+\.\d+(?:b\d+)?)\.zip$")
MAX_ZIP = 50 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
# The release's list of SHA256 (release.yml): one « <sha256>  <file> » line per file.
SUMS = "SHA256SUMS"
SUMS_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")
MAX_SUMS = 64 * 1024
CHECK_EVERY_S = 6 * 3600
# What an update replaces: the app's own files, never the person's (data/, .venv/).
APP_FILES = ("aihub", "requirements.txt", "LICENSE", "README.md", "CHANGELOG.md", "installer.bat", "Lancer AI Hub.bat",
             "installer.sh", "lancer.sh")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_cache = {}


def detached_options():
    """Detach on either OS; Windows creation flags are invalid on POSIX."""
    return ({"creationflags": NO_WINDOW | 0x00000008} if os.name == "nt"
            else {"start_new_session": True})


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
    """The newest release newer than `current`: {version, name, notes, url, size, page, sums}, or None.

    `sums`: the address of that release's SHA256SUMS (None without one: the download is refused then)."""
    best = None
    for release in releases or []:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        sums = next((asset.get("browser_download_url") for asset in release.get("assets") or []
                     if asset.get("name") == SUMS), None)
        for asset in release.get("assets") or []:
            found = ASSET.match(str(asset.get("name") or ""))
            key = version_key(found.group(1)) if found else None
            if key is None or key <= (version_key(current) or (0,)):
                continue
            if best is None or key > best[0]:
                best = (key, {"version": found.group(1), "name": release.get("name") or found.group(1),
                              "notes": (release.get("body") or "")[:4000], "url": asset.get("browser_download_url"),
                              "size": asset.get("size"), "page": release.get("html_url"), "sums": sums})
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
    if version_key(version) is None:
        raise UpdateError("version inattendue")
    folder = f"ai-hub-{version}/"
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (not names or len(names) > 5000 or len({name.rstrip('/').casefold() for name in names}) != len(names)
                    or sum(entry.file_size for entry in entries) > MAX_UNPACKED
                    or any(not n.startswith(folder) or ".." in PurePosixPath(n).parts
                           or PurePosixPath(n).is_absolute() or "\\" in n or ":" in n
                           or any(ord(char) < 32 for char in n) for n in names)
                    or any(part in ('.', '..') or part.endswith(('.', ' '))
                           for name in names for part in name.rstrip('/').split('/'))
                    or any(stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                           for entry in entries)):
                raise UpdateError("fichier inattendu : ce n'est pas une version d'AI Hub")
            init = archive.read(folder + "aihub/__init__.py").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        raise UpdateError("fichier inattendu : ce n'est pas une version d'AI Hub") from None
    found = re.search(r'__version__ = "([^"]+)"', init)
    if not found or found.group(1) != version:
        raise UpdateError("le fichier ne contient pas la version annoncée")
    return folder


def published_sha256(url, name, get=_get):
    """The SHA256 that the release's SHA256SUMS gives for `name`: one line for it, and only well-formed lines."""
    try:
        data = get(url, timeout=30).content
    except Exception as exc:  # no network, GitHub refusing: refused, said
        raise UpdateError(f"liste d'empreintes SHA256SUMS impossible à lire : {exc}"[:300]) from None
    if len(data) > MAX_SUMS:
        raise UpdateError("liste d'empreintes SHA256SUMS trop grosse : mise à jour refusée")
    try:
        entries = [SUMS_LINE.fullmatch(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError:
        entries = [None]
    if not entries or None in entries:
        raise UpdateError("liste d'empreintes SHA256SUMS illisible : mise à jour refusée")
    found = [entry.group(1) for entry in entries if entry.group(2) == name]
    if len(found) != 1:
        raise UpdateError(f"la liste d'empreintes SHA256SUMS n'a pas une ligne (et une seule) pour {name} : "
                          "mise à jour refusée")
    return found[0]


def download(release, folder, get=_get):
    """The release's file, into `folder`, checked (first against its SHA256SUMS). Returns its path."""
    url = str(release.get("url") or "")
    if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise UpdateError("adresse de téléchargement inattendue")
    sums = str(release.get("sums") or "")
    if not sums:
        raise UpdateError("cette version n'a pas de liste d'empreintes (SHA256SUMS) : mise à jour refusée")
    if not sums.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise UpdateError("adresse de la liste d'empreintes (SHA256SUMS) inattendue")
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
        if hashlib.sha256(data).hexdigest() != published_sha256(sums, target.name, get):
            raise UpdateError("le fichier téléchargé n'a pas l'empreinte publiée (SHA256SUMS) : "
                              "abîmé ou modifié, mise à jour refusée")
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
    spawn = spawn or (lambda cmd: subprocess.Popen(cmd, cwd=str(root), **detached_options(),
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


def restart_details(restart, root):
    """Recognize the app's own CLI; probes always go to local loopback."""
    if not restart or 'aihub' not in restart or 'serve' not in restart:
        return None
    def value(option, default):
        try:
            return restart[restart.index(option) + 1]
        except (ValueError, IndexError):
            return default
    database = Path(value('--db', str(Path(root) / 'data/aihub.db')))
    if not database.is_absolute():
        database = Path(root) / database
    port = int(value('--port', '8770'))
    if not 1 <= port <= 65535:
        raise UpdateError('port de redémarrage invalide')
    host = '[::1]' if value('--host', '127.0.0.1') == '::1' else '127.0.0.1'
    return database.resolve(), f'http://{host}:{port}/api/health'


def copy_database(source_path, target_path, standalone=False):
    """`standalone`: a copy kept on its own, without a write-ahead log, so that reading it back
    (read only) leaves no -wal nor -shm beside it."""
    import sqlite3
    source = sqlite3.connect(f'{Path(source_path).resolve().as_uri()}?mode=ro', uri=True)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            if standalone:
                target.execute('PRAGMA journal_mode = DELETE')
        finally:
            target.close()
    finally:
        source.close()


def wait_health(process, url, version, probe, timeout=30, why=None):
    """True once the new server answers as itself (that version, this start's probe).

    Else False, and the cause added to `why` (a list), for the journal: the server
    stopped (its exit code), or the time ran out (with the last answer seen)."""
    import urllib.request
    deadline = time.monotonic() + timeout
    cause, seen = None, 'aucune réponse'
    while time.monotonic() < deadline:
        code = process.poll() if process is not None else None
        if code is not None:
            cause = f"le nouveau serveur s'est arrêté (code de sortie {code})"
            break
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                state = json.load(response)
            if state.get('ready') and state.get('version') == version and state.get('probe') == probe:
                return True
            seen = (f"il répond en version {state.get('version')}, pas en {version}" if state.get('version') != version
                    else "il répond sans la sonde de ce lancement : un autre serveur occupe ce port"
                    if state.get('probe') != probe else "il répond sans être prêt")
        except (OSError, ValueError) as exc:
            seen = f"pas de réponse ({exc})"
        time.sleep(0.25)
    if why is not None:
        why.append(cause or f"délai de {timeout} s dépassé ; dernier essai : {seen}")
    return False


def journal(root, text):
    """A dated line at the end of data/mise-a-jour/journal.txt: what the person reads after an update."""
    log = Path(root) / "data" / "mise-a-jour" / "journal.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def server_log(restart, root):
    """The log the restarted server writes (serve --journal), or None."""
    try:
        log = Path(restart[restart.index('--journal') + 1])
    except (ValueError, IndexError):
        return None
    return log if log.is_absolute() else Path(root) / log


def log_end(path, start, lines=10):
    """The last lines (blank ones left out) written to `path` from byte `start` on."""
    try:
        with open(path, 'rb') as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(start if start <= size else 0, size - 64 * 1024))  # a log begun again: from its start
            text = fh.read().decode('utf-8', errors='replace')
    except OSError:
        return []
    return [line.rstrip()[:300] for line in text.splitlines() if line.strip()][-lines:]


def _contents(path):
    """A file's lines (whatever its line ends), None without it."""
    return path.read_bytes().splitlines() if path.is_file() else None


def apply(zip_path, version, root, *, pid=None, restart=None, pip=True, wait_s=60, run=subprocess.run,
          popen=subprocess.Popen, health=None):
    """Replace app files, preserving the DB until the new server is healthy."""
    import uuid
    root = Path(root).resolve()
    deadline = time.monotonic() + wait_s
    while pid and _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    if pid and _alive(pid):
        raise UpdateError("le serveur ne s'est pas arrêté : mise à jour annulée")
    work = root / "data" / "mise-a-jour"
    if not work.resolve().is_relative_to(root):
        raise UpdateError('le dossier de mise à jour sort de cette installation')
    suffix = uuid.uuid4().hex[:12]
    backup = work / f"avant-{version}-{suffix}"
    fresh = work / f"nouvelle-{version}-{suffix}"
    details = restart_details(restart, root)
    database, url = details if details else (None, None)
    database_existed = database is not None and database.exists()
    saved_db = backup / 'base-avant.db'
    moved, installed = [], []
    process, restarted = None, False
    pip_ran, requirements_changed = False, False
    def launch(probe=None):
        options = detached_options()
        if probe is not None:
            options['env'] = dict(os.environ, AIHUB_UPDATE_PROBE=probe)
        return popen(restart, cwd=str(root), **options, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    def install_requirements():
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(python if python.exists() else sys.executable), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "-r", str(root / "requirements.txt")], cwd=str(root),
            creationflags=NO_WINDOW, check=True)
    try:
        # Taken before the swap: undoing it never runs the code of the version it undoes.
        from .providers.claude_code import _kill_tree
        folder = verify(zip_path, version)
        backup.mkdir(parents=True)
        if database_existed:
            copy_database(database, saved_db, standalone=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(fresh)
            if os.name != 'nt':
                for script in ('installer.sh', 'lancer.sh'):
                    path = fresh / folder / script
                    if path.is_file():
                        path.chmod(0o755)
        source = fresh / folder.rstrip("/")
        for name in APP_FILES:
            if (root / name).exists():
                shutil.move(str(root / name), str(backup / name))
                moved.append(name)
        for name in APP_FILES:
            if (source / name).exists():
                shutil.move(str(source / name), str(root / name))
                installed.append(name)
        if pip:
            # The old one is in the backup now: undoing reinstalls the old dependencies only when they differ.
            requirements_changed = _contents(backup / "requirements.txt") != _contents(root / "requirements.txt")
            pip_ran = True  # even a pip that fails may have changed the venv
            install_requirements()
        if restart:
            probe = uuid.uuid4().hex if details else None
            log = server_log(restart, root)
            written = log.stat().st_size if log is not None and log.is_file() else 0
            process = launch(probe)
            restarted = True
            causes = []
            if details and not (health or wait_health)(process, url, version, probe, why=causes):
                end = log_end(log, written) if log is not None else []
                if end:
                    journal(root, f"fin de {log} depuis la relance :" + "".join(f"\n    | {line}" for line in end))
                cause = '; '.join(causes) or 'cause inconnue'
                raise UpdateError(f'la nouvelle version ne répond pas correctement ({cause}) ; ancienne version rétablie')
    except Exception:
        if process is not None:
            _kill_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise UpdateError('nouveau serveur impossible à arrêter : sauvegardes conservées, rétablissement manuel nécessaire') from None
        if database is not None and saved_db.exists():
            copy_database(saved_db, database)
        elif database is not None and not database_existed and restarted:
            database.unlink(missing_ok=True)
            for tail in ('-wal', '-shm'):
                Path(str(database) + tail).unlink(missing_ok=True)
        for name in installed:
            target = root / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for name in moved:
            shutil.move(str(backup / name), str(root / name))
        if pip_ran and requirements_changed:
            try:
                install_requirements()  # requirements.txt is the old one again
                journal(root, "dépendances de l'ancienne version réinstallées (son requirements.txt diffère de la nouvelle)")
            except Exception as exc:  # the old server is started all the same
                journal(root, f"dépendances de l'ancienne version non réinstallées : {exc}"[:600])
        elif pip_ran:
            journal(root, "requirements.txt identique dans les deux versions : dépendances laissées telles quelles")
        if restart:
            launch()
        raise
    finally:
        if fresh.exists():
            shutil.rmtree(fresh)
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
    result = 0
    try:
        backup = apply(args.zip, args.version, args.root, pid=args.pid, restart=json.loads(args.restart))
        message = f"mise à jour vers {args.version} faite ; l'ancienne version est dans {backup}"
    except Exception as exc:  # the server is stopped: the journal says why
        result = 1
        message = f"mise à jour vers {args.version} refusée : {exc}"
    journal(args.root, message)
    return result


if __name__ == "__main__":
    sys.exit(main())
