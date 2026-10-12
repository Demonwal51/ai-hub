"""Review and recover copied work. Original files change only on explicit apply."""

import hashlib
import io
import json
import os
import threading
import tarfile
import subprocess
import difflib
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from . import sandbox
from .store import StoreError

_APPLY_LOCK = threading.Lock()


def _stop_export(context, process):
    try:
        context.stop()
    finally:
        from .providers.claude_code import _kill_tree
        _kill_tree(process)


def locations(context, provider):
    _, suffix = context.identity(provider)
    root = Path(context.database).resolve().parent / 'espaces-isoles' / suffix
    return root, root / 'selection'


def collect(context, provider):
    """Recover a mountless guest via bounded tar; never unpack an untrusted link."""
    if context.policy['files'] != 'none':
        return
    if context.prepare(provider, create=False) is None:
        return
    root, selection = locations(context, provider)
    code = Path(sandbox.__file__).with_name('guest_export.py').read_text(encoding='utf-8')
    process = context.spawn(['python3', '-I', '-c', code, context.workdir, ','.join(sorted(sandbox.FORBIDDEN))],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=sandbox.NO_WINDOW, start_new_session=(os.name != 'nt'))
    timer = threading.Timer(60, _stop_export, args=(context, process)); timer.daemon = True; timer.start()
    try:
        payload = process.stdout.read(sandbox.MAX_BYTES + sandbox.MAX_FILES * 4096 + 1)
        if len(payload) > sandbox.MAX_BYTES + sandbox.MAX_FILES * 4096:
            raise StoreError('export de résultats trop grand')
        if process.wait(timeout=10) != 0:
            raise StoreError('récupération de l’espace interrompue')
    finally:
        timer.cancel()
        if process.poll() is None:
            context.stop(); process.kill(); process.wait(timeout=10)
        process.stdout.close(); context.finish()
    destination = root / 'exports' / uuid.uuid4().hex
    destination.mkdir(parents=True, mode=0o700)
    try:
        total, seen = 0, set()
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
            for member in archive:
                name = member.name
                path = PurePosixPath(name)
                if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or name != path.as_posix()
                        or any(ord(char) < 32 for char in name) or name in seen or not member.isfile() or member.size < 0):
                    raise StoreError('résultat de l’espace invalide : fichier ou chemin inattendu')
                seen.add(name); total += member.size
                if total > sandbox.MAX_BYTES or len(seen) > sandbox.MAX_FILES or not sandbox._allowed_name(path):
                    raise StoreError('résultat de l’espace hors des limites')
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                if len(data) != member.size:
                    raise StoreError('résultat de l’espace tronqué')
                target.write_bytes(data)
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise StoreError('archive de résultats illisible') from exc
    # Previous captures remain recoverable; the validated one becomes current.
    if selection.exists():
        _safe(selection, root)
        selection.rename(root / ('selection-avant-' + uuid.uuid4().hex))
    destination.rename(selection)


def _link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _safe(path, root):
    root = Path(root)
    if not Path(path).resolve().is_relative_to(root.resolve()):
        raise StoreError('chemin de résultat hors de cet espace')
    for parent in [Path(path), *Path(path).parents]:
        if _link(parent):
            raise StoreError('les liens ne peuvent pas être récupérés ou appliqués')
        if parent == root:
            break


def _manifest(root):
    path = root / 'manifest.json'
    if not path.is_file():
        return {}
    _safe(path, root)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise StoreError('inventaire de cet espace trop grand')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise StoreError('inventaire de cet espace illisible') from None
    if not isinstance(value, dict) or len(value) > sandbox.MAX_FILES:
        raise StoreError('inventaire de cet espace invalide')
    for name, digest in value.items():
        if (not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or ':' in name):
            raise StoreError('inventaire de cet espace invalide')
    return value


def files_of(context, provider):
    root, selection = locations(context, provider)
    if not selection.exists():
        return root, {}, {}
    _safe(selection, root)
    before, files, total, count = _manifest(root), {}, 0, 0
    pending = [selection]
    while pending:
        item = pending.pop()
        if item != selection and not sandbox._allowed_name(item.relative_to(selection)):
            continue
        _safe(item, selection)
        count += 1
        if count > sandbox.MAX_FILES:
            raise StoreError('trop de résultats : récupérez une sélection plus petite')
        if item.is_dir():
            for child in item.iterdir():
                if len(pending) >= sandbox.MAX_FILES:
                    raise StoreError('trop de résultats dans cet espace')
                pending.append(child)
            continue
        relative = item.relative_to(selection)
        if not sandbox._allowed_name(relative):
            continue
        size = item.stat().st_size
        total += size
        if total > sandbox.MAX_BYTES:
            raise StoreError('résultats trop grands : 128 Mo au maximum')
        data = sandbox.read_checked(item, selection)
        digest = hashlib.sha256(data).hexdigest()
        files[relative.as_posix()] = {'path': item, 'size': len(data), 'sha256': digest}
    return root, before, files


def review(context, provider):
    _, before, files = files_of(context, provider)
    changes = []
    for name in sorted(set(before) | set(files)):
        after = files.get(name)
        if after and before.get(name) == after['sha256']:
            continue
        changes.append({'name': name, 'kind': 'deleted' if after is None else 'modified' if name in before else 'new',
                        'size': after['size'] if after else 0, 'sha256': after['sha256'] if after else None})
    return changes


def download(context, provider):
    """Archive only safe copied files; no symlink, credential directory or host path."""
    root, _, files = files_of(context, provider)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, entry in sorted(files.items()):
            data = sandbox.read_checked(entry['path'], root / 'selection')
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise StoreError('un résultat a changé pendant la récupération : recommencez')
            archive.writestr(name, data)
    return output.getvalue()


def preview(context, provider, name):
    root, before, files = files_of(context, provider)
    if name not in before and name not in files:
        raise StoreError('fichier de résultat introuvable')
    entry = files.get(name)
    if entry and entry['size'] > 1024 * 1024:
        return {'name': name, 'binary': True, 'diff': '', 'note': 'Fichier trop grand pour un aperçu ; récupérez la copie.'}
    after = sandbox.read_checked(entry['path'], root / 'selection') if entry else b''
    original = Path(context.conversation['workdir']).resolve()
    target = original / name
    old = b''
    if _selected(name, context.policy) and target.exists():
        _safe(target, original)
        if target.stat().st_size <= 1024 * 1024:
            old = sandbox.read_checked(target, original, limit=1024 * 1024)
    try:
        left, right = old.decode('utf-8'), after.decode('utf-8')
        if '\x00' in left or '\x00' in right:
            raise UnicodeDecodeError('utf8', b'\0', 0, 1, 'binary')
    except UnicodeDecodeError:
        return {'name': name, 'binary': True, 'diff': '', 'note': 'Fichier binaire ; récupérez la copie pour le vérifier.'}
    diff = '\n'.join(difflib.unified_diff(left.splitlines(), right.splitlines(), fromfile='Original actuel',
                                         tofile='Copie de l’espace', lineterm=''))
    return {'name': name, 'binary': False, 'diff': diff[:100000], 'truncated': len(diff) > 100000}


def _selected(name, policy):
    return any(name == path or name.startswith(path.rstrip('/') + '/') for path in policy['paths'])


def apply(context, provider, expected, *, confirm_delete=False):
    if context.read_only or context.policy['files'] != 'write' or not context.policy['isolated']:
        raise StoreError('cet espace ne permet pas de modifier les fichiers originaux')
    if not isinstance(expected, dict) or not expected or len(expected) > sandbox.MAX_FILES:
        raise StoreError('choisissez les fichiers à appliquer')
    with _APPLY_LOCK:
        root, before, files = files_of(context, provider)
        original = Path(context.conversation['workdir']).resolve()
        planned = []
        for name, digest in expected.items():
            if (not isinstance(name, str) or not _selected(name, context.policy) or not sandbox._allowed_name(PurePosixPath(name))
                    or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or ':' in name):
                raise StoreError('un résultat dépasse les fichiers sélectionnés')
            entry = files.get(name)
            if (entry['sha256'] if entry else None) != digest or (not entry and name not in before):
                raise StoreError('les résultats ont changé : relisez-les avant de les appliquer')
            if not entry and not confirm_delete:
                raise StoreError('confirmez les suppressions sélectionnées')
            target = original / name
            _safe(target, original)
            existing = sandbox.read_checked(target, original) if target.exists() else None
            current = hashlib.sha256(existing).hexdigest() if existing is not None else None
            if current != before.get(name):
                raise StoreError(f'le fichier original a changé : {name}. Aucune modification appliquée.')
            data = sandbox.read_checked(entry['path'], root / 'selection') if entry else None
            if data is not None and hashlib.sha256(data).hexdigest() != digest:
                raise StoreError('un résultat a changé : aucune modification appliquée')
            planned.append((name, target, existing, data, target.stat().st_mode & 0o777 if target.exists() else None))
        backup = root / 'applications' / uuid.uuid4().hex
        backup.mkdir(parents=True, mode=0o700)
        (backup / 'inventaire.json').write_text(json.dumps({'files': list(expected)}, ensure_ascii=False), encoding='utf-8')
        done = []
        try:
            for name, target, existing, data, mode in planned:
                if existing is not None:
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    saved.write_bytes(existing)
                _safe(target, original)
                target.parent.mkdir(parents=True, exist_ok=True)
                _safe(target, original)
                if data is None:
                    target.unlink()
                else:
                    temporary = target.with_name('.aihub-' + uuid.uuid4().hex + '.tmp')
                    try:
                        with temporary.open('xb') as stream:
                            stream.write(data); stream.flush(); os.fsync(stream.fileno())
                        if target.exists():
                            temporary.chmod(target.stat().st_mode & 0o777)
                        _safe(target, original)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                done.append((target, existing, mode))
        except Exception:
            for target, existing, mode in reversed(done):
                _safe(target, original)
                if existing is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(existing)
                    target.chmod(mode)
            raise
        for name, _, _, data, _ in planned:
            if data is None:
                before.pop(name, None)
            else:
                before[name] = hashlib.sha256(data).hexdigest()
        temporary = root / ('manifest-' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(before, sort_keys=True), encoding='utf-8')
        os.replace(temporary, root / 'manifest.json')
        return {'applied': [name for name, *_ in planned], 'backup': str(backup)}
