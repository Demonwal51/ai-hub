"""Daily backup of the database.

One copy per day, `aihub-YYYY-MM-DD.db`, made with SQLite's online backup
(consistent even while the server writes). The `keep` most recent copies
are kept, older ones are deleted.

A conversation deleted for good is also deleted from every backup
(`purge_conversation`): otherwise "définitive" would only last until the
next restore.
"""

import os
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path

PATTERN = re.compile(r"^aihub-(\d{4}-\d{2}-\d{2})\.db$")
CHECK_EVERY_S = 3600  # a server running for days makes one copy per day
# A copy started before a deletion must not end after its purge, with the
# conversation still inside: copies and purges take turns.
_LOCK = threading.Lock()


class BackupError(Exception):
    pass


def backups(folder):
    """The backup files, oldest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and PATTERN.match(p.name))


def backup_now(db_path, folder, keep=14, today=None):
    """Make today's copy if it does not exist yet. Returns its path, or None."""
    with _LOCK:
        return _backup(db_path, Path(folder), keep, today)


def _backup(db_path, folder, keep, today):
    target = folder / f"aihub-{(today or date.today()).isoformat()}.db"
    if target.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partiel")
    try:
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(db_path)
        try:
            copy = sqlite3.connect(temporary)
            try:
                source.backup(copy)
                # A standalone file: no write-ahead log next to it.
                copy.execute("PRAGMA journal_mode = DELETE")
            finally:
                copy.close()
        finally:
            source.close()
        os.replace(temporary, target)
    except (OSError, sqlite3.Error) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError(f"sauvegarde impossible dans {folder} : {exc}") from None
    for old in backups(folder)[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def purge_conversation(folder, conversation_id):
    """Delete a conversation from every backup. Returns the errors, by file."""
    with _LOCK:
        return _purge(folder, "DELETE FROM conversations WHERE id = ?", conversation_id)


def purge_project(folder, project_id):
    """Delete a project (its name, its description) from every backup."""
    with _LOCK:
        return _purge(folder, "DELETE FROM projects WHERE id = ?", project_id, table="projects")


def _purge(folder, sql, row_id, table=None):
    errors = {}
    for path in backups(folder):
        try:
            conn = sqlite3.connect(path, isolation_level=None)
            try:
                if table and conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                                          (table,)).fetchone() is None:
                    continue  # a copy older than this table: nothing of it inside
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA secure_delete = ON")
                conn.execute(sql, (row_id,))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            errors[path.name] = str(exc)
    return errors


class DailyBackup:
    """Backs up at start, then checks every hour whether the day has changed."""

    def __init__(self, db_path, folder, keep=14, on_error=None):
        self.db_path, self.folder, self.keep = db_path, folder, keep
        self.on_error = on_error or (lambda message: None)
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def run_once(self):
        try:
            made = backup_now(self.db_path, self.folder, self.keep)
            self.last_error = None
            return made
        except BackupError as exc:
            self.last_error = str(exc)
            self.on_error(self.last_error)
            return None

    def _loop(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(CHECK_EVERY_S)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="sauvegarde", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
