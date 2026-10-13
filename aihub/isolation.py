"""Execution permissions: projects set ceilings, conversations can narrow them.

An isolated profile never falls back to execution on the host. Files are
explicitly selected and copied; provider accounts are not copied from the host.
"""

import json
import re
from pathlib import PurePosixPath

from .store import StoreError

DEFAULT = {'isolated': False, 'files': 'none', 'paths': [], 'network': 'ai', 'memory': 'own'}
LEVELS = {'none': 0, 'read': 1, 'write': 2}


def check(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise StoreError('profil d’accès inconnu')
    result = {**DEFAULT, **value}
    if not isinstance(result['isolated'], bool):
        raise StoreError('espace isolé : oui ou non attendu')
    if (not all(isinstance(result[key], str) for key in ('files', 'network', 'memory'))
            or result['files'] not in LEVELS or result['network'] not in ('none', 'ai') or result['memory'] not in ('own', 'project')):
        raise StoreError('profil d’accès invalide')
    paths = result['paths']
    if not isinstance(paths, list) or len(paths) > 100:
        raise StoreError('choisissez au maximum 100 fichiers ou dossiers')
    for path in paths:
        if (not isinstance(path, str) or not path or len(path) > 500 or '\\' in path or ':' in path
                or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
                or path in ('.', '/') or re.search(r'[\x00-\x1f]', path)):
            raise StoreError('indiquez des chemins relatifs, sans remontée vers un dossier parent')
    result['paths'] = sorted(set(paths))
    if result['files'] == 'none' and paths:
        raise StoreError('aucun accès aux fichiers : retirez la sélection')
    return result


def get(conn, *, conversation_id=None, project_id=None):
    column, key = ('conversation_id', conversation_id) if conversation_id is not None else ('project_id', project_id)
    row = conn.execute(f'SELECT policy_json FROM execution_policies WHERE {column} = ?', (key,)).fetchone()
    return check(json.loads(row[0])) if row else None


def narrower(child, parent):
    """Validate a child before saving or running: a later parent edit also takes effect."""
    if parent['isolated'] and not child['isolated']:
        raise StoreError('ce projet impose un espace isolé')
    if parent['isolated']:
        if LEVELS[child['files']] > LEVELS[parent['files']]:
            raise StoreError('le projet ne permet pas cet accès aux fichiers')
        if parent['network'] == 'none' and child['network'] != 'none':
            raise StoreError('le projet interdit le réseau')
        if parent['memory'] == 'own' and child['memory'] != 'own':
            raise StoreError('le projet limite la mémoire à cette conversation')
        for path in child['paths']:
            if not any(path == allowed or path.startswith(allowed.rstrip('/') + '/') for allowed in parent['paths']):
                raise StoreError('ce fichier dépasse la sélection autorisée par le projet')
    return child


def effective(conn, conversation):
    policy = _effective(conn, conversation)
    if suspended(conn, conversation['id']):
        policy = dict(policy, paused=True)
    return policy


def suspended(conn, cid):
    return conn.execute('SELECT 1 FROM execution_suspensions WHERE conversation_id = ?', (cid,)).fetchone() is not None


def suspend(conn, cid, value=True):
    if value:
        conn.execute('INSERT OR IGNORE INTO execution_suspensions (conversation_id) VALUES (?)', (cid,))
    else:
        conn.execute('DELETE FROM execution_suspensions WHERE conversation_id = ?', (cid,))


def _effective(conn, conversation):
    conv = dict(conversation)
    parent = get(conn, project_id=conv.get('project_id')) or DEFAULT.copy()
    child = get(conn, conversation_id=conv['id'])
    if child is None:
        return dict(parent)
    if not parent['isolated']:
        return child
    # A reduced project ceiling takes effect immediately, even for older
    # explicit conversation profiles; their saved preferences remain intact.
    out = dict(child, isolated=True)
    out['files'] = min((child['files'], parent['files']), key=LEVELS.get)
    out['network'] = 'none' if parent['network'] == 'none' else child['network']
    out['memory'] = 'own' if parent['memory'] == 'own' else child['memory']
    overlap = set()
    for selected in child['paths']:
        for allowed in parent['paths']:
            if selected == allowed or selected.startswith(allowed.rstrip('/') + '/'):
                overlap.add(selected)
            elif allowed.startswith(selected.rstrip('/') + '/'):
                overlap.add(allowed)
    out['paths'] = sorted(overlap) if out['files'] != 'none' else []
    return out


def save(conn, value, *, conversation_id=None, project_id=None):
    policy = check(value)
    previous = []
    if conversation_id is not None:
        conv = conn.execute('SELECT * FROM conversations WHERE id = ?', (conversation_id,)).fetchone()
        if conv is None:
            raise StoreError('conversation introuvable')
        parent = get(conn, project_id=conv['project_id']) or DEFAULT
        previous = [(conv['id'], effective(conn, conv))]
        narrower(policy, parent)
    elif project_id is not None:
        previous = [(row['id'], effective(conn, row)) for row in conn.execute('SELECT * FROM conversations WHERE project_id = ?', (project_id,))]
    column, key = ('conversation_id', conversation_id) if conversation_id is not None else ('project_id', project_id)
    conn.execute(f'INSERT INTO execution_policies ({column}, policy_json) VALUES (?, ?) '
                 f'ON CONFLICT({column}) DO UPDATE SET policy_json = excluded.policy_json',
                 (key, json.dumps(policy, ensure_ascii=False, sort_keys=True)))
    for cid, old in previous:
        conv = conn.execute('SELECT * FROM conversations WHERE id = ?', (cid,)).fetchone()
        if old['isolated'] and not effective(conn, conv)['isolated']:
            # Consent to bypass INSIDE a VM does not consent to bypass on the PC.
            conn.execute("UPDATE conversations SET permission_mode = 'manual' WHERE id = ? AND permission_mode = 'bypass'", (cid,))
    # A change of access context invalidates CLI resume sessions from its former context.
    if conversation_id is not None:
        conn.execute('UPDATE messages SET session_id = NULL WHERE conversation_id = ?', (conversation_id,))
        conn.execute('UPDATE subtasks SET session_id = NULL WHERE plan_id IN '
                     '(SELECT id FROM plans WHERE conversation_id = ?)', (conversation_id,))
        if policy['isolated']:
            conn.execute("UPDATE conversations SET tools_json = '[]' WHERE id = ?", (conversation_id,))
    else:
        conn.execute('UPDATE messages SET session_id = NULL WHERE conversation_id IN '
                     '(SELECT id FROM conversations WHERE project_id = ?)', (project_id,))
        conn.execute('UPDATE subtasks SET session_id = NULL WHERE plan_id IN '
                     '(SELECT p.id FROM plans p JOIN conversations c ON c.id = p.conversation_id WHERE c.project_id = ?)', (project_id,))
        if policy['isolated']:
            conn.execute("UPDATE conversations SET tools_json = '[]' WHERE project_id = ?", (project_id,))
    return policy


def for_run(conn, conversation, *, account_id=None, read_only=False, settings=None):
    policy = effective(conn, conversation)
    if policy.get('paused'):
        from .providers.claude_code import ProviderError
        raise ProviderError('accès suspendus : le propriétaire doit réactiver cet espace')
    if account_id is not None:
        from . import access
        from .providers.claude_code import ProviderError
        person = conn.execute('SELECT * FROM users WHERE id = ?', (account_id,)).fetchone()
        if person is None or person['disabled']:
            raise ProviderError('compte utilisateur indisponible : exécution refusée')
        if not policy['isolated'] and not access.trusted(dict(person)):
            raise ProviderError('les membres et invités doivent utiliser un espace isolé ; demandez au propriétaire de configurer ses accès')
    if not policy['isolated']:
        return None
    from .sandbox import SandboxExecution
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    context = SandboxExecution(database, dict(conversation), policy, account_id=account_id, read_only=read_only, settings=settings)
    import uuid
    conn.execute('INSERT OR IGNORE INTO sandbox_contexts (conversation_id, nonce) VALUES (?, ?)',
                 (conversation['id'], uuid.uuid4().hex))
    context.scope_nonce = conn.execute('SELECT nonce FROM sandbox_contexts WHERE conversation_id = ?',
                                       (conversation['id'],)).fetchone()[0]
    if account_id is not None:
        import uuid
        conn.execute('INSERT OR IGNORE INTO sandbox_principals (user_id, nonce) VALUES (?, ?)',
                     (account_id, uuid.uuid4().hex))
        nonce = conn.execute('SELECT nonce FROM sandbox_principals WHERE user_id = ?', (account_id,)).fetchone()[0]
        # SQLite may reuse a deleted numeric user id. Never reuse its logged-in VM.
        context.account_id = f'{account_id}:{nonce}'
    context.user_id = account_id
    return context
