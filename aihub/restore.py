"""Restore a backup from the app (1.1): the helper the server starts just before it stops.

    python -m aihub.restore --backup <file> --db <base> --root <install> --pid <server> --restart '<json command>'

It waits for the server to be gone, puts the backup in place with the same
function as « python -m aihub restaurer » (the former base kept beside it),
starts the server again and checks it answers. A server that does not come
back gets the former base back. What happened is written to
`<base>-restauration.json`, which the app shows once it is up again.
"""

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, backup, updater


def report_file(db_path):
    base = Path(db_path).resolve()
    return base.with_name(f"{base.stem}-restauration.json")


def _write_report(db_path, **fields):
    target = report_file(db_path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields},
                                    ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def last_report(db_path):
    try:
        return json.loads(report_file(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run(backup_path, db_path, *, root=None, pid=None, restart=None, wait_s=60, popen=subprocess.Popen, health=None,
        alive=None):
    """Returns the report written (status: "ok", "echec" or "retabli")."""
    alive = alive or updater._alive
    db_path = Path(db_path).resolve()
    root = Path(root or updater.install_root()).resolve()  # where the server is started again
    deadline = time.monotonic() + wait_s
    while pid and alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    if pid and alive(pid):
        report = {"status": "echec", "backup": Path(backup_path).name,
                  "error": "le serveur ne s'est pas arrêté : rien n'a été restauré"}
        _write_report(db_path, **report)
        return report
    # Unique even for two restores in the same second (restore_database refuses an existing name).
    keep = db_path.with_name(f"avant-restauration-{datetime.now():%Y-%m-%d_%H%M%S}-{uuid.uuid4().hex[:6]}.db")
    report = {"status": "ok", "backup": Path(backup_path).name, "keep": str(keep)}
    try:
        report["files"] = backup.restore_database(backup_path, db_path, keep)
    except Exception as exc:  # restore_database undoes what it did: the former base is in place
        report = {"status": "echec", "backup": Path(backup_path).name, "error": str(exc)}
    details = updater.restart_details(restart, root) if restart else None

    def launch(probe=None):
        options = updater.detached_options()
        if probe is not None:
            import os
            options["env"] = dict(os.environ, AIHUB_UPDATE_PROBE=probe)
        return popen(restart, cwd=str(root), **options, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)

    if restart:
        probe = uuid.uuid4().hex if details else None
        process = launch(probe)
        if report["status"] == "ok" and details and not (health or updater.wait_health)(process, details[1], __version__, probe):
            # The restored base does not let the server start: the former one comes back.
            from .providers.claude_code import _kill_tree
            _kill_tree(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            updater.copy_database(keep, db_path)
            launch()
            report = {"status": "retabli", "backup": Path(backup_path).name, "keep": str(keep),
                      "error": "le serveur ne répondait pas avec cette sauvegarde : la base d'avant a été remise"}
    _write_report(db_path, **report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aihub.restore")
    parser.add_argument("--backup", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--root", default=None)
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--restart", default="null")
    args = parser.parse_args(argv)
    report = run(args.backup, args.db, root=args.root, pid=args.pid, restart=json.loads(args.restart))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
