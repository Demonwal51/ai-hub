"""Restart the server on the code now on disk (1.2): a development copy (git) whose code
changed since the server started, because Codex or a « git pull » brought new commits.

- `code_state()`: the code on disk against the code the server started with: its
  version, its commit, the new commits' subjects, and the files changed but not
  committed yet (work in progress: no restart then).
- `preflight()` (before anything stops): the code on disk, in a process of its own,
  imports the whole app and migrates a copy of the base. A code that does not
  start is refused there, while the server still runs.
- The helper (`python -m aihub.relaunch …`), started just before the server stops:
  it waits for the server to be gone, starts it again as it was started, and checks
  it answers with the new version. A server that does not come back gets the base
  from before back (when the schema changed, a copy was kept beside it) and is
  started once more. What happened is written to `<base>-relance.json`.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import updater

NO_WINDOW = updater.NO_WINDOW
# What a restart reloads: a change elsewhere (docs, tests) needs none.
CODE_PATHS = ("aihub", "requirements.txt")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def disk_version(root):
    """The version written in aihub/__init__.py on disk now, or None."""
    try:
        text = (Path(root) / "aihub" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return found.group(1) if found else None


def git_head(root):
    """The commit the copy is on, read from .git (no git needed), or None."""
    folder = Path(root) / ".git"
    try:
        head = (folder / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if SHA.fullmatch(head) else None
        ref = head[5:].strip()
        if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", ref) or ".." in ref:
            return None
        loose = folder / ref
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8").strip()
            return sha if SHA.fullmatch(sha) else None
        for line in (folder / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and SHA.fullmatch(parts[0]):
                return parts[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def run_git(root, *args, timeout=10):
    """git in the copy, without a window; its output, or None when git is missing or fails."""
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def started_state(root):
    """Read when the server starts: what it runs."""
    from . import __version__
    return {"version": __version__, "commit": git_head(root)}


def code_state(started, root, git=run_git):
    """{running, disk, commit, started_commit, changes, dirty, git, restart_needed}.

    `changes`: the subjects of the commits made since the start (git needed).
    `dirty`: code files changed but not committed (None: unknown, without git)."""
    root = Path(root)
    disk = disk_version(root)
    commit = git_head(root) if (root / ".git").exists() else None
    is_git = commit is not None
    moved = is_git and started.get("commit") is not None and commit != started["commit"]
    changes, dirty = [], None
    if is_git:
        if moved:
            log = git(root, "log", "--format=%s", "-n", "20", f"{started['commit']}..{commit}")
            changes = [line.strip() for line in (log or "").splitlines() if line.strip()]
        status = git(root, "status", "--porcelain", "--untracked-files=no", "--", *CODE_PATHS)
        dirty = None if status is None else sorted({line[3:].strip() for line in status.splitlines() if line.strip()})
    return {"running": started["version"], "disk": disk, "commit": commit, "started_commit": started.get("commit"),
            "git": is_git, "changes": changes, "dirty": dirty,
            "restart_needed": bool(moved or (disk is not None and disk != started["version"]))}


def report_file(db_path):
    base = Path(db_path).resolve()
    return base.with_name(f"{base.stem}-relance.json")


def last_report(db_path):
    try:
        return json.loads(report_file(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_report(db_path, **fields):
    target = report_file(db_path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields},
                                    ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def check(db_path, out):
    """In the process of the code on disk: the app imports and migrates a copy of the base."""
    result = {"ok": False}
    try:
        from . import __version__, db
        import aihub.web.app  # noqa: F401  the whole app, as the server will load it
        with tempfile.TemporaryDirectory(prefix="aihub-essai-") as folder:
            copy = Path(folder) / "essai.db"
            updater.copy_database(db_path, copy)
            conn = db.connect(str(copy))
            try:
                schema = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
        result = {"ok": True, "version": __version__, "schema": schema}
    except BaseException as exc:  # everything the new code may raise is the answer
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}
    Path(out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


def preflight(db_path, root, *, run=subprocess.run, timeout=180):
    """{ok, version, schema} or {ok: False, error}: the code on disk tried before anything stops."""
    with tempfile.TemporaryDirectory(prefix="aihub-relance-") as folder:
        out = Path(folder) / "resultat.json"
        try:
            run([sys.executable, "-m", "aihub.relaunch", "check", "--db", str(Path(db_path).resolve()),
                 "--out", str(out)], cwd=str(root), timeout=timeout, creationflags=NO_WINDOW,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"le nouveau code n'a pas fini son essai en {timeout} s"}
        except OSError as exc:
            return {"ok": False, "error": f"essai impossible : {exc}"}
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": "le nouveau code s'est arrêté avant de répondre (erreur au chargement)"}


def keep_copy(db_path, version):
    """A copy of the base beside it, before a schema change: aihub-avant-<version>-<moment>.db."""
    base = Path(db_path).resolve()
    target = base.with_name(f"{base.stem}-avant-{version}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.db")
    updater.copy_database(base, target)
    return target


def run(db_path, version, *, root, pid=None, restart=None, keep=None, wait_s=60, popen=subprocess.Popen,
        health=None, alive=None):
    """Wait for the server to be gone, start it again, check it. Returns the report written."""
    alive = alive or updater._alive
    db_path = Path(db_path).resolve()
    root = Path(root).resolve()
    deadline = time.monotonic() + wait_s
    while pid and alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    details = updater.restart_details(restart, root) if restart else None

    def launch(probe=None):
        options = updater.detached_options()
        if probe is not None:
            options["env"] = dict(os.environ, AIHUB_UPDATE_PROBE=probe)
        return popen(restart, cwd=str(root), **options, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)

    if pid and alive(pid):
        report = {"status": "echec", "version": version,
                  "error": "l'ancien serveur ne s'est pas arrêté : il tourne toujours, rien n'a changé"}
        _write_report(db_path, **report)
        return report
    report = {"status": "ok", "version": version, "keep": str(keep) if keep else None}
    if restart:
        probe = uuid.uuid4().hex if details else None
        process = launch(probe)
        if details and not (health or updater.wait_health)(process, details[1], version, probe):
            from .providers.claude_code import _kill_tree
            _kill_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            if keep and Path(keep).is_file():
                updater.copy_database(keep, db_path)  # the base from before the schema change
            launch()
            report = {"status": "echec", "version": version, "keep": str(keep) if keep else None,
                      "error": "le nouveau code ne répondait pas : serveur relancé une fois de plus"
                               + (", avec la base d'avant" if keep else "")}
    _write_report(db_path, **report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aihub.relaunch")
    sub = parser.add_subparsers(dest="command")
    trial = sub.add_parser("check")
    trial.add_argument("--db", required=True)
    trial.add_argument("--out", required=True)
    parser.add_argument("--db")
    parser.add_argument("--version")
    parser.add_argument("--root")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--restart", default="null")
    parser.add_argument("--keep", default=None)
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(args.db, args.out)
    if not (args.db and args.version and args.root):
        parser.error("--db, --version et --root sont nécessaires")
    report = run(args.db, args.version, root=args.root, pid=args.pid, restart=json.loads(args.restart),
                 keep=args.keep)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
