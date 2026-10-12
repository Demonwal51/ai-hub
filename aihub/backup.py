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
import zipfile
import hashlib
import json
from datetime import date
from pathlib import Path
from .providers.claude_code import ProviderError

PATTERN = re.compile(r"^aihub-(\d{4}-\d{2}-\d{2})\.db$")
CHECK_EVERY_S = 3600  # a server running for days makes one copy per day
# A copy started before a deletion must not end after its purge, with the
# conversation still inside: copies and purges take turns.
_LOCK = threading.Lock()
MAX_ASSETS = 2 * 1024 * 1024 * 1024


def assets_path(database):
    return Path(database).with_suffix('.pieces.zip')


def _assets(source_db, copied_db, target):
    """Only files referenced by the consistent DB copy, never account folders."""
    from .sandbox import read_checked
    base = Path(source_db).resolve()
    pieces = base.with_name(base.stem + '-pieces')
    conn = sqlite3.connect(copied_db)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone() is None:
            return False
        rows = conn.execute('SELECT id, conversation_id, stored FROM attachments').fetchall()
    finally:
        conn.close()
    if not rows:
        return False
    total, manifest = 0, []
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for aid, cid, stored in rows:
            path = Path(stored)
            if not path.resolve().is_relative_to(pieces.resolve()) or path.is_symlink():
                raise BackupError('une pièce jointe est hors du stockage de cette base')
            data = read_checked(path, pieces, limit=20 * 1024 * 1024)
            total += len(data)
            if total > MAX_ASSETS:
                raise BackupError('pièces jointes trop grandes pour la sauvegarde (2 Go maximum)')
            name = f'{int(cid)}/{path.name}'
            archive.writestr(name, data)
            manifest.append({'id': aid, 'conversation_id': cid, 'name': name, 'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    return True


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
    pieces = assets_path(target)
    pieces_temporary = pieces.with_name(pieces.name + '.partiel')
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
        has_assets = _assets(db_path, temporary, pieces_temporary)
        if has_assets:
            os.replace(pieces_temporary, pieces)
        os.replace(temporary, target)
    except (OSError, sqlite3.Error, BackupError, ProviderError) as exc:
        try:
            temporary.unlink(missing_ok=True)
            pieces_temporary.unlink(missing_ok=True)
            if not target.exists():
                pieces.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError(f"sauvegarde impossible dans {folder} : {exc}") from None
    for old in backups(folder)[:-keep] if keep > 0 else []:
        old.unlink()
        assets_path(old).unlink(missing_ok=True)
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
                if table == 'projects':
                    removed = [row[0] for row in conn.execute('SELECT id FROM conversations WHERE project_id = ?', (row_id,))]
                else:
                    removed = [row_id]
                conn.execute(sql, (row_id,))
            finally:
                conn.close()
            if table != 'projects':
                _purge_assets(assets_path(path), removed)
        except (sqlite3.Error, OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            errors[path.name] = str(exc)
    return errors


def _purge_assets(path, conversation_ids):
    if not path.exists():
        return
    prefixes = tuple(f'{int(cid)}/' for cid in conversation_ids)
    temporary = path.with_name(path.name + '.purge')
    try:
        with zipfile.ZipFile(path) as old, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as fresh:
            manifest = json.loads(old.read('manifest.json'))
            manifest = [item for item in manifest if item['conversation_id'] not in conversation_ids]
            for entry in old.infolist():
                if entry.filename != 'manifest.json' and not entry.filename.startswith(prefixes):
                    fresh.writestr(entry.filename, old.read(entry))
            fresh.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restore_assets(chosen, target_db):
    """Restore attached files beside the target DB and rewrite portable paths."""
    from .sandbox import read_checked
    chosen, target_db = Path(chosen), Path(target_db).resolve()
    archive_path = assets_path(chosen)
    if not archive_path.is_file():
        return 0
    root = target_db.with_name(target_db.stem + '-pieces')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise BackupError('stockage des pièces jointes contenant un lien')
    conn = sqlite3.connect(target_db)
    try:
        rows = {row[0]: row for row in conn.execute('SELECT id, conversation_id, stored FROM attachments')}
        plan, total = [], 0
        with zipfile.ZipFile(archive_path) as archive:
            names = [entry.filename for entry in archive.infolist()]
            if len(names) != len(set(names)) or len(names) > len(rows) + 1:
                raise BackupError('archive de pièces jointes contenant des entrées répétées ou inattendues')
            if archive.getinfo('manifest.json').file_size > 8 * 1024 * 1024:
                raise BackupError('inventaire de pièces jointes trop grand')
            manifest = json.loads(archive.read('manifest.json'))
            if not isinstance(manifest, list) or len(manifest) != len(rows):
                raise BackupError('inventaire de pièces jointes incohérent')
            seen = set()
            for item in manifest:
                aid, cid, name = item['id'], item['conversation_id'], item['name']
                row = rows.get(aid)
                filename = name.split('/')[-1] if isinstance(name, str) else ''
                if (row is None or aid in seen or cid != row[1] or name != f'{int(cid)}/{filename}'
                        or not filename.startswith(f'{int(aid)}-') or any(char in filename for char in ('\\', ':', '\x00'))
                        or filename in ('.', '..')):
                    raise BackupError('chemin de pièce jointe inattendu')
                seen.add(aid)
                entry = archive.getinfo(name)
                import stat
                mode = entry.external_attr >> 16
                if entry.is_dir() or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
                    raise BackupError('pièce jointe contenant un lien ou un fichier spécial')
                total += entry.file_size
                if entry.file_size > 20 * 1024 * 1024 or total > MAX_ASSETS:
                    raise BackupError('pièces jointes trop grandes')
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise BackupError('une pièce jointe ne correspond pas à son empreinte')
                plan.append((aid, root / str(int(cid)) / filename, data))
        for aid, path, data in plan:
            if path.parent.exists() and (path.parent.is_symlink() or (hasattr(path.parent, 'is_junction') and path.parent.is_junction())):
                raise BackupError('stockage des pièces jointes contenant un lien')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_name(path.name + '.restauration')
            temporary.write_bytes(data)
            os.replace(temporary, path)
            conn.execute('UPDATE attachments SET stored = ? WHERE id = ?', (str(path), aid))
        conn.commit()
        return len(plan)
    except (OSError, KeyError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        raise BackupError(f'pièces jointes illisibles : {exc}') from None
    finally:
        conn.close()


def restore_database(chosen, target_db, keep):
    """Validate and migrate a private staging copy before touching live data.

    The server must be stopped by the caller. Preserve both the former DB
    and its attachment directory; undo publication if SQLite copying fails.
    """
    import tempfile
    from . import db
    chosen, target_db, keep = Path(chosen).resolve(), Path(target_db).resolve(), Path(keep)
    root = target_db.with_name(target_db.stem + '-pieces')
    previous = keep.with_name(keep.stem + '-pieces')
    has_assets = assets_path(chosen).is_file()
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise BackupError('stockage des pièces jointes contenant un lien')
    if keep.exists() or previous.exists():
        raise BackupError('une sauvegarde avant restauration existe déjà ; réessayez dans une seconde')

    def copy_database(source_path, destination):
        source = sqlite3.connect(f'{Path(source_path).resolve().as_uri()}?mode=ro', uri=True)
        try:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

    with tempfile.TemporaryDirectory(prefix='.restauration-', dir=target_db.parent) as staging:
        staged_db = Path(staging) / target_db.name
        copy_database(chosen, staged_db)
        db.connect(str(staged_db)).close()
        count = restore_assets(chosen, staged_db)
        staged_root = staged_db.with_name(staged_db.stem + '-pieces')
        conn = sqlite3.connect(staged_db)
        try:
            if has_assets:
                for aid, stored in conn.execute('SELECT id, stored FROM attachments').fetchall():
                    relative = Path(stored).relative_to(staged_root)
                    conn.execute('UPDATE attachments SET stored = ? WHERE id = ?', (str(root / relative), aid))
                conn.commit()
            conn.execute('PRAGMA journal_mode = DELETE')
        finally:
            conn.close()
        existed = target_db.exists()
        if existed:
            copy_database(target_db, keep)
        moved_old = published = False
        try:
            if has_assets:
                if root.exists():
                    root.rename(previous)
                    moved_old = True
                if staged_root.exists():
                    staged_root.rename(root)
                    published = True
            copy_database(staged_db, target_db)
        except (OSError, sqlite3.Error):
            if existed:
                copy_database(keep, target_db)
            else:
                target_db.unlink(missing_ok=True)
            if published:
                root.rename(staged_root)
            if moved_old:
                previous.rename(root)
            raise
        return count


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
