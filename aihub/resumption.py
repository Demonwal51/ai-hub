"""Continuation prompts and session scope, without copying provider credentials."""
import hashlib
import json
from pathlib import Path

from . import access, isolation


def scope(conn, conversation, account_id=None):
    person = conn.execute('SELECT created_at FROM users WHERE id = ?', (account_id,)).fetchone() if account_id else None
    value = {'account': account_id or 'host', 'created': person['created_at'] if person else None,
             'conversation': conversation['id'], 'project_id': conversation['project_id'],
             'workdir': str(Path(conversation['workdir']).resolve()),
             'policy': isolation.effective(conn, conversation),
             'project': access.reads_project(conn, account_id, conversation),
             'shared': bool(conversation['shared']), 'tools': conversation['tools_json'],
             'permissions': conversation['permission_mode']}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prompt(original, partial='', error=None):
    reason = 'arrêt ou erreur de la requête précédente'
    if error and any(word in error.lower() for word in ('quota', 'usage limit', '429')):
        reason = 'quota du fournisseur'
    payload = json.dumps({'demande_initiale': original[:16000], 'dernier_point_connu': (partial or '')[-6000:],
                          'interruption': reason}, ensure_ascii=False).replace('<', '\\u003c')
    return ('Reprends le travail interrompu à partir du dernier point connu. '
            'Vérifie l’état actuel des fichiers et ce qui a déjà réussi avant de répéter une action. '
            'Ne recommence pas les étapes terminées. Respecte les accès et permissions actuels. '
            'Termine par un compte rendu complet de ce qui est achevé et de ce qui reste à faire.\n' + payload)
