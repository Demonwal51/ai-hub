"""Data access for AI Hub: agents, conversations, messages and plans.

Every function takes an open connection from `aihub.db.connect`. Errors the
user can cause are raised as `StoreError` with a message meant to be shown
as is; the database constraints stay the last line of defence.
"""

import json
import sqlite3

from .db import transaction, utc_now

PROVIDERS = ("claude_code", "codex", "ollama")
MODES = ("single", "orchestration")
PERMISSION_MODES = ("manual", "plan", "bypass")
ASSIGNMENT_MODES = ("manual", "auto")

ACTIVE_STATUSES = ("pending", "running")
FINAL_STATUSES = ("ok", "error", "stopped")

_RENUMBER_BASE = 1_000_000

# Allowed status changes for a message or a subtask.
_TRANSITIONS = {
    "pending": {"running", "stopped", "error"},
    "running": {"ok", "error", "stopped"},
}

BYPASS_WARNING = (
    "Mode sans permission : l'agent pourra modifier ou supprimer des fichiers "
    "et lancer des commandes dans le dossier de travail sans rien demander. "
    "Une commande peut aussi agir en dehors de ce dossier. "
    "Confirmez pour l'activer."
)


class StoreError(Exception):
    """A request the store refuses, with a message fit for the user."""


class NotFound(StoreError):
    """The thing asked for does not exist."""


class Busy(StoreError):
    """The request conflicts with work in progress: stop or finish it first."""


class BypassConfirmationRequired(StoreError):
    """The bypass mode was requested without the warning being confirmed."""

    def __init__(self):
        super().__init__(BYPASS_WARNING)


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def _require(row, what):
    if row is None:
        raise NotFound(f"{what} introuvable")
    return row


def _check_choice(value, allowed, what):
    if value not in allowed:
        raise StoreError(f"{what} inconnu : {value!r} (attendu : {', '.join(allowed)})")


def _check_text(value, what):
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{what} vide")


def _check_count(value, what):
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise StoreError(f"{what} invalide : {value!r}")


def _check_transition(current, new, what):
    if new not in _TRANSITIONS.get(current, set()):
        raise StoreError(f"{what} : passage de « {current} » à « {new} » impossible")


# --- Agents -----------------------------------------------------------------

def create_agent(conn, name, provider, model, enabled=True):
    _check_text(name, "nom de l'agent")
    _check_choice(provider, PROVIDERS, "fournisseur")
    _check_text(model, "modèle")
    try:
        cur = conn.execute(
            "INSERT INTO agents (name, provider, model, enabled, created_at) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), provider, model.strip(), int(bool(enabled)), utc_now()),
        )
    except sqlite3.IntegrityError:
        raise StoreError(f"un agent nommé « {name.strip()} » existe déjà") from None
    return cur.lastrowid


_PROVIDER_NAMES = {"claude_code": "Claude Code", "codex": "Codex", "ollama": "IA locale"}
DEFAULT_MODEL = "default"
DEFAULT_MODELS = {"claude_code": DEFAULT_MODEL, "codex": "codex"}  # "the CLI's own default model"


def agent_name(provider, model):
    base = _PROVIDER_NAMES.get(provider, provider)
    return base if model == DEFAULT_MODELS.get(provider, DEFAULT_MODEL) else f"{base} ({model})"


def ensure_agent(conn, provider, model):
    """The agent for this provider and model, created if missing. Returns its id."""
    _check_choice(provider, PROVIDERS, "fournisseur")
    _check_text(model, "modèle")
    name = agent_name(provider, model.strip())
    row = _one(conn, "SELECT id FROM agents WHERE name = ?", (name,))
    if row is not None:
        return row["id"]
    return create_agent(conn, name, provider, model.strip())


def get_agent(conn, agent_id):
    return _require(_one(conn, "SELECT * FROM agents WHERE id = ?", (agent_id,)), "agent")


def list_agents(conn, enabled_only=False):
    sql = "SELECT * FROM agents"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return conn.execute(sql + " ORDER BY name").fetchall()


def update_agent(conn, agent_id, *, model=None, enabled=None):
    get_agent(conn, agent_id)
    with transaction(conn):
        if model is not None:
            _check_text(model, "modèle")
            conn.execute("UPDATE agents SET model = ? WHERE id = ?", (model.strip(), agent_id))
        if enabled is not None:
            conn.execute("UPDATE agents SET enabled = ? WHERE id = ?", (int(bool(enabled)), agent_id))


def delete_agent(conn, agent_id):
    """Remove an agent from the configuration.

    Past messages keep the provider and model they ran with. Subtasks of
    plans still being prepared lose their assignment and must be reassigned
    before validation.
    """
    agent = get_agent(conn, agent_id)
    busy = _one(
        conn,
        """
        SELECT 1 FROM subtasks s JOIN plans p ON p.id = s.plan_id
        WHERE s.agent_id = ? AND p.status IN ('validated', 'running')
        UNION ALL
        SELECT 1 FROM messages WHERE agent_id = ? AND status IN ('pending', 'running')
        """,
        (agent_id, agent_id),
    )
    if busy:
        raise Busy(f"l'agent « {agent['name']} » est utilisé par une tâche en cours ou validée")
    conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))


# --- Projects ---------------------------------------------------------------

def create_project(conn, name, *, description=None, workdir=None):
    _check_text(name, "nom du projet")
    now = utc_now()
    try:
        cur = conn.execute(
            "INSERT INTO projects (name, description, workdir, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), (description or "").strip() or None, workdir, now, now))
    except sqlite3.IntegrityError:
        raise StoreError(f"un projet nommé « {name.strip()} » existe déjà") from None
    return cur.lastrowid


def get_project(conn, project_id):
    return _require(_one(conn, "SELECT * FROM projects WHERE id = ?", (project_id,)), "projet")


def list_projects(conn):
    return conn.execute(
        """
        SELECT p.*, COUNT(c.id) AS conversations
        FROM projects p LEFT JOIN conversations c ON c.project_id = p.id
        GROUP BY p.id ORDER BY p.name COLLATE NOCASE
        """).fetchall()


def update_project(conn, project_id, *, name=None, description=None, workdir=None):
    """Arguments left to None are not changed; description "" clears it."""
    get_project(conn, project_id)
    changes = {}
    if name is not None:
        _check_text(name, "nom du projet")
        changes["name"] = name.strip()
    if description is not None:
        changes["description"] = description.strip() or None
    if workdir is not None:
        changes["workdir"] = workdir or None
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    try:
        conn.execute(f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
                     (*changes.values(), utc_now(), project_id))
    except sqlite3.IntegrityError:
        raise StoreError(f"un projet nommé « {changes['name']} » existe déjà") from None


def project_conversations(conn, project_id):
    return conn.execute("SELECT * FROM conversations WHERE project_id = ? ORDER BY updated_at DESC, id DESC",
                        (project_id,)).fetchall()


def delete_project(conn, project_id, *, with_conversations=False):
    """Delete a project. Its conversations stay, without a project, unless
    `with_conversations`: then they are deleted for good with it (none may be
    running). Returns the ids of the deleted conversations."""
    get_project(conn, project_id)
    ids = [row["id"] for row in project_conversations(conn, project_id)]
    if with_conversations:
        for cid in ids:
            if active_request(conn, cid) is not None:
                raise Busy("une conversation du projet a une requête en cours : arrêtez-la d'abord")
            plan = open_plan(conn, cid)
            if plan is not None and plan["status"] == "running":
                raise Busy("une conversation du projet a un plan en cours : arrêtez-le d'abord")
    with transaction(conn):
        if with_conversations:
            conn.execute("DELETE FROM conversations WHERE project_id = ?", (project_id,))
        else:
            from . import isolation
            policies = [(row['id'], isolation.effective(conn, row)) for row in project_conversations(conn, project_id)]
            conn.execute("UPDATE conversations SET project_id = NULL WHERE project_id = ?", (project_id,))
            for cid, policy in policies:
                if policy['isolated']:
                    isolation.save(conn, {k: v for k, v in policy.items() if k != 'paused'}, conversation_id=cid)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return ids if with_conversations else []


def move_conversation(conn, conversation_id, project_id):
    """Put a conversation in a project, or take it out (None)."""
    conversation = get_conversation(conn, conversation_id)
    from . import isolation
    policy = isolation.effective(conn, conversation)
    if project_id is not None:
        get_project(conn, project_id)
    with transaction(conn):
        conn.execute("UPDATE conversations SET project_id = ?, updated_at = ? WHERE id = ?",
                     (project_id, utc_now(), conversation_id))
        if policy['isolated']:
            isolation.save(conn, {k: v for k, v in policy.items() if k != 'paused'}, conversation_id=conversation_id)


def set_shared(conn, conversation_id, shared):
    """Open or close this conversation to the others of its project (both ways)."""
    get_conversation(conn, conversation_id)
    conn.execute("UPDATE conversations SET shared = ? WHERE id = ?", (int(bool(shared)), conversation_id))


# --- Conversations ----------------------------------------------------------

def create_conversation(
    conn,
    title,
    *,
    mode="single",
    agent_id=None,
    conductor_agent_id=None,
    permission_mode="manual",
    confirm_bypass=False,
    assignment_mode="manual",
    workdir=None,
    project_id=None,
):
    _check_text(title, "titre")
    if project_id is not None:
        get_project(conn, project_id)
    _check_choice(mode, MODES, "mode")
    _check_choice(permission_mode, PERMISSION_MODES, "mode de permission")
    _check_choice(assignment_mode, ASSIGNMENT_MODES, "mode d'assignation")
    if permission_mode == "bypass" and not confirm_bypass:
        raise BypassConfirmationRequired()
    for aid in (agent_id, conductor_agent_id):
        if aid is not None:
            get_agent(conn, aid)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO conversations (title, mode, agent_id, conductor_agent_id, permission_mode,
                                   bypass_confirmed_at, assignment_mode, workdir,
                                   created_at, updated_at, project_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title.strip(), mode, agent_id, conductor_agent_id, permission_mode,
            now if permission_mode == "bypass" else None,
            assignment_mode, workdir, now, now, project_id,
        ),
    )
    return cur.lastrowid


def get_conversation(conn, conversation_id):
    return _require(
        _one(conn, "SELECT * FROM conversations WHERE id = ?", (conversation_id,)),
        "conversation",
    )


def list_conversations(conn):
    return conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC, id DESC").fetchall()


def _touch(conn, conversation_id):
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (utc_now(), conversation_id))


def rename_conversation(conn, conversation_id, title):
    _check_text(title, "titre")
    get_conversation(conn, conversation_id)
    conn.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title.strip(), utc_now(), conversation_id),
    )


def set_permission_mode(conn, conversation_id, permission_mode, *, confirm_bypass=False):
    """Change the permission mode. "bypass" requires the warning to be confirmed."""
    _check_choice(permission_mode, PERMISSION_MODES, "mode de permission")
    get_conversation(conn, conversation_id)
    if permission_mode == "bypass" and not confirm_bypass:
        raise BypassConfirmationRequired()
    now = utc_now()
    conn.execute(
        """
        UPDATE conversations
        SET permission_mode = ?, bypass_confirmed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (permission_mode, now if permission_mode == "bypass" else None, now, conversation_id),
    )


def update_conversation(conn, conversation_id, *, mode=None, agent_id=None,
                        conductor_agent_id=None, assignment_mode=None, workdir=None):
    """Change settings other than the title and the permission mode.

    Arguments left to None are not changed.
    """
    get_conversation(conn, conversation_id)
    if active_request(conn, conversation_id) is not None:
        raise Busy("une requête est en cours dans cette conversation")
    if mode is not None:
        _check_choice(mode, MODES, "mode")
        if open_plan(conn, conversation_id) is not None:
            raise Busy("un plan est ouvert : terminez-le ou arrêtez-le avant de changer de mode")
    if assignment_mode is not None:
        _check_choice(assignment_mode, ASSIGNMENT_MODES, "mode d'assignation")
    for aid in (agent_id, conductor_agent_id):
        if aid is not None:
            get_agent(conn, aid)
    fields = {
        "mode": mode, "agent_id": agent_id, "conductor_agent_id": conductor_agent_id,
        "assignment_mode": assignment_mode, "workdir": workdir,
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    with transaction(conn):
        conn.execute(
            f"UPDATE conversations SET {sets} WHERE id = ?",
            (*changes.values(), conversation_id),
        )
        _touch(conn, conversation_id)


def set_obsidian_note(conn, conversation_id, note_path):
    """Turn the project note on (a path relative to the vault) or off (None)."""
    get_conversation(conn, conversation_id)
    if note_path is not None:
        _check_text(note_path, "chemin de la note")
    conn.execute("UPDATE conversations SET obsidian_note = ? WHERE id = ?",
                 (note_path.strip() if note_path else None, conversation_id))


def mark_noted(conn, message_id):
    """Remember that a reply was sent to the project note."""
    get_message(conn, message_id)
    conn.execute("UPDATE messages SET noted_at = COALESCE(noted_at, ?) WHERE id = ?",
                 (utc_now(), message_id))


def delete_conversation(conn, conversation_id):
    """Delete a conversation for good: messages, plans and subtasks go with it.

    The content is overwritten in the file (secure_delete) and the write-ahead
    log is emptied, so nothing of it can be read back from the database files.
    The web server then deletes it from the daily backups too
    (backup.purge_conversation). A conversation with a
    request or a plan in progress must be stopped first.
    """
    get_conversation(conn, conversation_id)
    if active_request(conn, conversation_id) is not None:
        raise Busy("arrêtez la requête en cours avant de supprimer la conversation")
    plan = open_plan(conn, conversation_id)
    if plan is not None and plan["status"] == "running":
        raise Busy("arrêtez le plan en cours avant de supprimer la conversation")
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# --- Messages ---------------------------------------------------------------

def active_request(conn, conversation_id):
    return _one(
        conn,
        "SELECT * FROM messages WHERE conversation_id = ? AND status IN ('pending', 'running')",
        (conversation_id,),
    )


def add_user_message(conn, conversation_id, content, user_id=None):
    """`user_id`: who wrote it, when several people share the conversation (None: the owner)."""
    _check_text(content, "message")
    get_conversation(conn, conversation_id)
    if active_request(conn, conversation_id) is not None:
        raise Busy("une requête est déjà en cours dans cette conversation")
    # An open plan does not block messages: the user may discuss it with the
    # conductor. Asking for a second plan is what is refused (orchestrator).
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, status, created_at, user_id)
            VALUES (?, 'user', ?, 'ok', ?, ?)
            """,
            (conversation_id, content, utc_now(), user_id),
        )
        _touch(conn, conversation_id)
    return cur.lastrowid


def start_request(conn, conversation_id, agent_id):
    """Open the assistant reply of a request. Only one may be active per conversation."""
    get_conversation(conn, conversation_id)
    agent = get_agent(conn, agent_id)
    if not agent["enabled"]:
        raise StoreError(f"l'agent « {agent['name']} » est désactivé")
    try:
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO messages (conversation_id, role, agent_id, provider, model,
                                      status, created_at)
                VALUES (?, 'assistant', ?, ?, ?, 'pending', ?)
                """,
                (conversation_id, agent_id, agent["provider"], agent["model"], utc_now()),
            )
            _touch(conn, conversation_id)
    except sqlite3.IntegrityError:
        raise Busy("une requête est déjà en cours dans cette conversation") from None
    return cur.lastrowid


def get_message(conn, message_id):
    return _require(_one(conn, "SELECT * FROM messages WHERE id = ?", (message_id,)), "message")


def list_messages(conn, conversation_id, last=None):
    """The messages in order; `last`: only the last ones (a long conversation shown in part)."""
    get_conversation(conn, conversation_id)
    if last is not None:
        return conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conversation_id, int(last))).fetchall()
    return conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
    ).fetchall()


def has_replies(conn, conversation_id):
    """True when an AI already answered there (successfully)."""
    return conn.execute("SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'ok' "
                        "LIMIT 1", (conversation_id,)).fetchone() is not None


def count_messages(conn, conversation_id):
    return conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]


def mark_message_running(conn, message_id):
    message = get_message(conn, message_id)
    _check_transition(message["status"], "running", "message")
    conn.execute("UPDATE messages SET status = 'running' WHERE id = ?", (message_id,))


def append_message_content(conn, message_id, chunk):
    """Add a streamed piece of text to a running reply."""
    # Do not read the entire growing reply back into Python for every token.
    changed = conn.execute("UPDATE messages SET content = content || ? WHERE id = ? AND status = 'running'",
                           (chunk, message_id)).rowcount
    if not changed:
        get_message(conn, message_id)  # retain NotFound for an unknown message
        raise StoreError("le message n'est pas en cours")


def finish_message(conn, message_id, status, *, content=None, error=None,
                   duration_ms=None, tokens_in=None, tokens_out=None, session_id=None):
    """Close a reply as ok, error or stopped, with its measurements."""
    if status not in FINAL_STATUSES:
        raise StoreError(f"statut final inconnu : {status!r}")
    for value, what in ((duration_ms, "durée"), (tokens_in, "tokens envoyés"),
                        (tokens_out, "tokens reçus")):
        _check_count(value, what)
    message = get_message(conn, message_id)
    _check_transition(message["status"], status, "message")
    with transaction(conn):
        conn.execute(
            """
            UPDATE messages
            SET status = ?, content = COALESCE(?, content), error = ?, duration_ms = ?,
                tokens_in = ?, tokens_out = ?, session_id = COALESCE(?, session_id),
                finished_at = ?
            WHERE id = ?
            """,
            (status, content, error, duration_ms, tokens_in, tokens_out, session_id,
             utc_now(), message_id),
        )
        _touch(conn, message["conversation_id"])


def set_tools(conn, conversation_id, names):
    """The tools of a conversation (names already checked by aihub/tools.py)."""
    get_conversation(conn, conversation_id)
    if active_request(conn, conversation_id) is not None:
        raise Busy("une requête est en cours : attendez qu'elle ait fini pour changer les outils")
    conn.execute("UPDATE conversations SET tools_json = ? WHERE id = ?", (json.dumps(list(names)), conversation_id))


def last_session(conn, conversation_id):
    """(provider, session_id) of the conversation's latest session, whatever the AI, or None.

    The next request resumes it only with the same provider: a session of
    one AI knows nothing of what another answered since.
    """
    row = _one(
        conn,
        """
        SELECT provider, session_id FROM messages
        WHERE conversation_id = ? AND session_id IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (conversation_id,),
    )
    return (row["provider"], row["session_id"]) if row else None


# --- Quotas and permissions -------------------------------------------------

QUOTA_STATUSES = ("allowed", "allowed_warning", "rejected")


def record_quota(conn, provider, status, *, limit_type=None, utilization=None,
                 resets_at=None, windows_json=None, user_id=None):
    """`user_id`: the quota of that person's own account (None: the computer's)."""
    _check_choice(provider, PROVIDERS, "fournisseur")
    _check_choice(status, QUOTA_STATUSES, "état de quota")
    if utilization is not None and (not isinstance(utilization, (int, float))
                                    or isinstance(utilization, bool) or utilization < 0):
        raise StoreError(f"taux d'utilisation invalide : {utilization!r}")
    cur = conn.execute(
        """
        INSERT INTO quota_observations (provider, status, limit_type, utilization, resets_at,
                                        windows_json, observed_at, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (provider, status, limit_type, utilization, resets_at, windows_json, utc_now(), user_id),
    )
    return cur.lastrowid


def latest_quota(conn, provider, user_id=None):
    """Last quota state seen for a provider's account (`user_id`: a person's own, None: the computer's)."""
    return _one(
        conn,
        "SELECT * FROM quota_observations WHERE provider = ? AND user_id IS ? ORDER BY id DESC LIMIT 1",
        (provider, user_id),
    )


def record_permission(conn, *, tool_name, input_json, decision, reason=None,
                      message_id=None, subtask_id=None):
    """Journal a permission asked by an agent and the answer given."""
    if (message_id is None) == (subtask_id is None):
        raise StoreError("une autorisation se rattache à un message ou à une sous-tâche")
    _check_text(tool_name, "outil")
    _check_choice(decision, ("allow", "deny"), "type de décision")
    cur = conn.execute(
        """
        INSERT INTO permission_requests (message_id, subtask_id, tool_name, input_json,
                                         decision, reason, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, subtask_id, tool_name, input_json, decision, reason, utc_now()),
    )
    return cur.lastrowid


def list_permissions(conn, *, message_id=None, subtask_id=None):
    column, value = ("message_id", message_id) if message_id is not None else ("subtask_id", subtask_id)
    return conn.execute(
        f"SELECT * FROM permission_requests WHERE {column} = ? ORDER BY id", (value,)
    ).fetchall()


INTERRUPTED = "interrompue : le serveur s'est arrêté pendant la requête"


def recover_interrupted(conn):
    """Close what a killed server left running, so no conversation stays locked.

    To be called when the server starts, before any request runs. Returns
    the number of replies and plans closed.
    """
    with transaction(conn):
        now = utc_now()
        replies = conn.execute(
            "UPDATE messages SET status = 'stopped', error = ?, finished_at = ? "
            "WHERE status IN ('pending', 'running')",
            (INTERRUPTED, now),
        ).rowcount
        conn.execute(
            "UPDATE subtasks SET status = 'stopped', error = ?, finished_at = ? "
            "WHERE status IN ('pending', 'running') "
            "AND plan_id IN (SELECT id FROM plans WHERE status = 'running')",
            (INTERRUPTED, now),
        )
        plans = conn.execute(
            "UPDATE plans SET status = 'stopped', finished_at = ? WHERE status = 'running'", (now,)
        ).rowcount
        conn.execute("UPDATE variants SET status = 'stopped', error = ? WHERE status = 'running'", (INTERRUPTED,))
        conn.execute("UPDATE delegations SET status = 'error', error = ? WHERE status = 'running'", (INTERRUPTED,))
        conn.execute("UPDATE schedules SET last_status = 'stopped', last_error = ? WHERE last_status = 'running'",
                     (INTERRUPTED,))
    return replies + plans


def stop_request(conn, conversation_id):
    """Stop the active request of a conversation, if any. Returns its id or None."""
    message = active_request(conn, conversation_id)
    if message is None:
        return None
    finish_message(conn, message["id"], "stopped")
    return message["id"]


# --- Plans ------------------------------------------------------------------

def _get_plan_row(conn, plan_id):
    return _require(_one(conn, "SELECT * FROM plans WHERE id = ?", (plan_id,)), "plan")


def _require_proposed(conn, plan_id):
    plan = _get_plan_row(conn, plan_id)
    if plan["status"] != "proposed":
        raise StoreError("le plan est validé : il ne peut plus être modifié")
    return plan


def _get_subtask(conn, subtask_id):
    return _require(_one(conn, "SELECT * FROM subtasks WHERE id = ?", (subtask_id,)), "sous-tâche")


def _agent_snapshot(conn, agent_id, model):
    if agent_id is None:
        if model is not None:
            raise StoreError("un modèle ne peut pas être choisi sans agent")
        return None, None
    agent = get_agent(conn, agent_id)
    if model is not None:
        _check_text(model, "modèle")
        model = model.strip()
    return agent["provider"], model or agent["model"]


def _would_cycle(conn, subtask_id, depends_on_id):
    """True if subtask_id is already reachable from depends_on_id."""
    row = _one(
        conn,
        """
        WITH RECURSIVE reach(id) AS (
            SELECT depends_on_id FROM subtask_deps WHERE subtask_id = ?
            UNION
            SELECT d.depends_on_id FROM subtask_deps d JOIN reach r ON d.subtask_id = r.id
        )
        SELECT 1 FROM reach WHERE id = ?
        """,
        (depends_on_id, subtask_id),
    )
    return row is not None


def _check_parallel(value):
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise StoreError(f"nombre d'agents en parallèle invalide : {value!r}")


def _files_json(files):
    """Validate the files a subtask declares; "*" means the whole project."""
    if files is None:
        return None
    if not isinstance(files, (list, tuple)) or not all(isinstance(f, str) and f.strip() for f in files):
        raise StoreError(f"liste de fichiers invalide : {files!r}")
    return json.dumps([f.strip() for f in files], ensure_ascii=False)


def _insert_subtasks(conn, plan_id, subtasks):
    ids = []
    for spec in subtasks:
        if "description" not in spec:
            raise StoreError("sous-tâche sans description")
        ids.append(add_subtask(conn, plan_id, spec["description"], title=spec.get("title"),
                               files=spec.get("files"), agent_id=spec.get("agent_id"),
                               model=spec.get("model"), kind=spec.get("kind")))
    for spec, sid in zip(subtasks, ids):
        for position in spec.get("depends_on", ()):
            if (not isinstance(position, int) or isinstance(position, bool)
                    or not 1 <= position <= len(ids)):
                raise StoreError(f"dépendance vers une position inexistante : {position!r}")
            add_dependency(conn, sid, ids[position - 1])
    return ids


def create_plan(conn, conversation_id, request_message_id, subtasks=(), *, summary=None,
                max_parallel=None):
    """Record the plan proposed by the conductor.

    `subtasks` is a sequence of dicts with "description", and optionally
    "title", "files", "agent_id", "model" and "depends_on" (positions,
    starting at 1, of earlier or later subtasks of the same plan).
    `max_parallel` is how many agents the conductor wants running at once.
    """
    conversation = get_conversation(conn, conversation_id)
    if conversation["mode"] != "orchestration":
        raise StoreError("la conversation n'est pas en mode orchestration")
    request = get_message(conn, request_message_id)
    if request["conversation_id"] != conversation_id or request["role"] != "user":
        raise StoreError("la demande ne correspond pas à un message de cette conversation")
    _check_parallel(max_parallel)
    with transaction(conn):
        if open_plan(conn, conversation_id) is not None:
            raise Busy("un plan est déjà ouvert dans cette conversation")
        cur = conn.execute(
            "INSERT INTO plans (conversation_id, request_message_id, summary, max_parallel, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, request_message_id, summary, max_parallel, utc_now()),
        )
        plan_id = cur.lastrowid
        _insert_subtasks(conn, plan_id, subtasks)
    return plan_id


def replace_plan(conn, plan_id, subtasks, *, summary=None, max_parallel=None):
    """Replace the content of a proposed plan, as the conductor adapted it."""
    _require_proposed(conn, plan_id)
    _check_parallel(max_parallel)
    with transaction(conn):
        conn.execute("DELETE FROM subtasks WHERE plan_id = ?", (plan_id,))
        conn.execute("UPDATE plans SET summary = ?, max_parallel = ? WHERE id = ?",
                     (summary, max_parallel, plan_id))
        _insert_subtasks(conn, plan_id, subtasks)


def update_subtask(conn, subtask_id, *, title=None, description=None, files=None, kind=None):
    """Edit a subtask of a proposed plan. Arguments left to None are not changed."""
    subtask = _get_subtask(conn, subtask_id)
    _require_proposed(conn, subtask["plan_id"])
    if description is not None:
        _check_text(description, "description de la sous-tâche")
    if title is not None:
        _check_text(title, "titre de la sous-tâche")
    if kind is not None:
        _check_text(kind, "genre de travail")
    conn.execute(
        "UPDATE subtasks SET title = COALESCE(?, title), description = COALESCE(?, description), "
        "files_json = COALESCE(?, files_json), kind = COALESCE(?, kind) WHERE id = ?",
        (title.strip() if title else None, description.strip() if description else None,
         _files_json(files), kind.strip() if kind else None, subtask_id),
    )


def set_plan_synthesis(conn, plan_id, message_id):
    _get_plan_row(conn, plan_id)
    conn.execute("UPDATE plans SET synthesis_message_id = ? WHERE id = ?", (message_id, plan_id))


def list_plans(conn, conversation_id):
    """Every plan of a conversation, oldest first, with subtasks and dependencies."""
    get_conversation(conn, conversation_id)
    return [get_plan(conn, row["id"]) for row in conn.execute(
        "SELECT id FROM plans WHERE conversation_id = ? ORDER BY id", (conversation_id,))]


def get_plan(conn, plan_id):
    """Return the plan with its subtasks (in order) and their dependencies."""
    plan = dict(_get_plan_row(conn, plan_id))
    rows = conn.execute(
        "SELECT * FROM subtasks WHERE plan_id = ? ORDER BY position", (plan_id,)
    ).fetchall()
    deps = {}
    for sid, dep in conn.execute(
        """
        SELECT d.subtask_id, d.depends_on_id FROM subtask_deps d
        JOIN subtasks s ON s.id = d.subtask_id WHERE s.plan_id = ?
        ORDER BY d.depends_on_id
        """,
        (plan_id,),
    ):
        deps.setdefault(sid, []).append(dep)
    plan["removed_agents"] = json.loads(plan.pop("removed_agents_json") or "[]")
    plan["subtasks"] = []
    for row in rows:
        subtask = dict(row, depends_on=deps.get(row["id"], []))
        subtask["files"] = json.loads(subtask.pop("files_json")) if row["files_json"] else []
        plan["subtasks"].append(subtask)
    return plan


def open_plan(conn, conversation_id):
    return _one(
        conn,
        "SELECT * FROM plans WHERE conversation_id = ? AND status IN ('proposed', 'validated', 'running')",
        (conversation_id,),
    )


def add_subtask(conn, plan_id, description, *, title=None, files=None, agent_id=None, model=None,
                kind=None):
    _check_text(description, "description de la sous-tâche")
    if title is not None:
        _check_text(title, "titre de la sous-tâche")
    if kind is not None:
        _check_text(kind, "genre de travail")
    _require_proposed(conn, plan_id)
    provider, model = _agent_snapshot(conn, agent_id, model)
    files_json = _files_json(files)
    position = _one(
        conn, "SELECT COALESCE(MAX(position), 0) + 1 FROM subtasks WHERE plan_id = ?", (plan_id,)
    )[0]
    cur = conn.execute(
        """
        INSERT INTO subtasks (plan_id, position, title, description, files_json, kind, agent_id,
                              provider, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, position, title.strip() if title else None, description.strip(), files_json,
         kind.strip() if kind else None, agent_id, provider, model),
    )
    return cur.lastrowid


def remove_subtask(conn, subtask_id):
    subtask = _get_subtask(conn, subtask_id)
    _require_proposed(conn, subtask["plan_id"])
    with transaction(conn):
        conn.execute("DELETE FROM subtasks WHERE id = ?", (subtask_id,))
        # Keep positions contiguous so the plan reads 1, 2, 3... Moving through
        # a high range first avoids clashing with UNIQUE (plan_id, position).
        rows = conn.execute(
            "SELECT id FROM subtasks WHERE plan_id = ? ORDER BY position", (subtask["plan_id"],)
        ).fetchall()
        for offset, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE subtasks SET position = ? WHERE id = ?", (_RENUMBER_BASE + offset, row["id"])
            )
        conn.execute(
            "UPDATE subtasks SET position = position - ? WHERE plan_id = ?",
            (_RENUMBER_BASE, subtask["plan_id"]),
        )


def assign_subtask(conn, subtask_id, agent_id, *, model=None, reason=None):
    """Give a subtask to an agent, optionally on another model than the agent's default.

    `agent_id=None` leaves the subtask unassigned. `reason` is why the
    automatic assignment chose it; a choice made by the user has none.
    """
    subtask = _get_subtask(conn, subtask_id)
    _require_proposed(conn, subtask["plan_id"])
    provider, model = _agent_snapshot(conn, agent_id, model)
    with transaction(conn):
        conn.execute(
            "UPDATE subtasks SET agent_id = ?, provider = ?, model = ?, assignment_reason = ? WHERE id = ?",
            (agent_id, provider, model, reason if agent_id is not None else None, subtask_id),
        )
        if agent_id is not None and reason is None:
            # The user brings an agent back: it is no longer "taken out".
            removed = _removed_agents(conn, subtask["plan_id"])
            if agent_id in removed:
                removed.remove(agent_id)
                _set_removed_agents(conn, subtask["plan_id"], removed)


def _removed_agents(conn, plan_id):
    row = _one(conn, "SELECT removed_agents_json FROM plans WHERE id = ?", (plan_id,))
    return json.loads(row["removed_agents_json"] or "[]") if row else []


def _set_removed_agents(conn, plan_id, removed):
    conn.execute("UPDATE plans SET removed_agents_json = ? WHERE id = ?",
                 (json.dumps(sorted(set(removed))), plan_id))


def remove_agent_from_plan(conn, plan_id, agent_id):
    """Take an agent out of a plan: its subtasks become unassigned.

    Returns the ids of the subtasks the conductor must reassign.
    """
    _require_proposed(conn, plan_id)
    with transaction(conn):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM subtasks WHERE plan_id = ? AND agent_id = ? ORDER BY position",
            (plan_id, agent_id),
        )]
        conn.execute(
            "UPDATE subtasks SET agent_id = NULL, provider = NULL, model = NULL, assignment_reason = NULL "
            "WHERE plan_id = ? AND agent_id = ?",
            (plan_id, agent_id),
        )
        _set_removed_agents(conn, plan_id, _removed_agents(conn, plan_id) + [agent_id])
    return ids


def add_dependency(conn, subtask_id, depends_on_id):
    subtask = _get_subtask(conn, subtask_id)
    other = _get_subtask(conn, depends_on_id)
    if subtask["plan_id"] != other["plan_id"]:
        raise StoreError("les deux sous-tâches ne sont pas dans le même plan")
    _require_proposed(conn, subtask["plan_id"])
    if subtask_id == depends_on_id:
        raise StoreError("une sous-tâche ne peut pas dépendre d'elle-même")
    if _would_cycle(conn, subtask_id, depends_on_id):
        raise StoreError("cette dépendance créerait une boucle")
    conn.execute(
        "INSERT OR IGNORE INTO subtask_deps (subtask_id, depends_on_id) VALUES (?, ?)",
        (subtask_id, depends_on_id),
    )


def remove_dependency(conn, subtask_id, depends_on_id):
    subtask = _get_subtask(conn, subtask_id)
    _require_proposed(conn, subtask["plan_id"])
    conn.execute(
        "DELETE FROM subtask_deps WHERE subtask_id = ? AND depends_on_id = ?",
        (subtask_id, depends_on_id),
    )


def validate_plan(conn, plan_id):
    """Freeze the plan. Every subtask must have an enabled agent."""
    _require_proposed(conn, plan_id)
    rows = conn.execute(
        """
        SELECT s.position, s.agent_id, a.enabled, a.name FROM subtasks s
        LEFT JOIN agents a ON a.id = s.agent_id
        WHERE s.plan_id = ? ORDER BY s.position
        """,
        (plan_id,),
    ).fetchall()
    if not rows:
        raise StoreError("le plan ne contient aucune sous-tâche")
    unassigned = [str(r["position"]) for r in rows if r["agent_id"] is None]
    if unassigned:
        raise StoreError(f"sous-tâches sans agent : {', '.join(unassigned)}")
    disabled = sorted({r["name"] for r in rows if not r["enabled"]})
    if disabled:
        raise StoreError(f"agents désactivés dans le plan : {', '.join(disabled)}")
    conn.execute(
        "UPDATE plans SET status = 'validated', validated_at = ? WHERE id = ?",
        (utc_now(), plan_id),
    )


def start_plan(conn, plan_id):
    plan = _get_plan_row(conn, plan_id)
    if plan["status"] != "validated":
        raise StoreError("seul un plan validé peut être lancé")
    conn.execute("UPDATE plans SET status = 'running' WHERE id = ?", (plan_id,))


def ready_subtasks(conn, plan_id):
    """Pending subtasks whose dependencies are all done: they may run now.

    The conductor chooses how many of them to launch at once.
    """
    plan = _get_plan_row(conn, plan_id)
    if plan["status"] != "running":
        return []
    return conn.execute(
        """
        SELECT s.* FROM subtasks s
        WHERE s.plan_id = ? AND s.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM subtask_deps d JOIN subtasks dep ON dep.id = d.depends_on_id
              WHERE d.subtask_id = s.id AND dep.status <> 'ok'
          )
        ORDER BY s.position
        """,
        (plan_id,),
    ).fetchall()


def start_subtask(conn, subtask_id):
    subtask = _get_subtask(conn, subtask_id)
    if subtask["id"] not in {r["id"] for r in ready_subtasks(conn, subtask["plan_id"])}:
        raise StoreError(f"la sous-tâche {subtask['position']} ne peut pas démarrer maintenant")
    conn.execute(
        "UPDATE subtasks SET status = 'running', started_at = ? WHERE id = ?",
        (utc_now(), subtask_id),
    )


def finish_subtask(conn, subtask_id, status, *, result=None, error=None,
                   duration_ms=None, tokens_in=None, tokens_out=None, session_id=None):
    if status not in FINAL_STATUSES:
        raise StoreError(f"statut final inconnu : {status!r}")
    for value, what in ((duration_ms, "durée"), (tokens_in, "tokens envoyés"),
                        (tokens_out, "tokens reçus")):
        _check_count(value, what)
    subtask = _get_subtask(conn, subtask_id)
    _check_transition(subtask["status"], status, "sous-tâche")
    conn.execute(
        """
        UPDATE subtasks
        SET status = ?, result = ?, error = ?, duration_ms = ?, tokens_in = ?,
            tokens_out = ?, session_id = ?, finished_at = ?
        WHERE id = ?
        """,
        (status, result, error, duration_ms, tokens_in, tokens_out, session_id, utc_now(),
         subtask_id),
    )


def finish_plan(conn, plan_id):
    """Close a running plan once no subtask is active.

    Done if every subtask succeeded, error otherwise (failed, or blocked by a
    failed dependency). Subtasks that can never run are marked stopped.
    """
    plan = _get_plan_row(conn, plan_id)
    if plan["status"] != "running":
        raise StoreError("le plan n'est pas en cours")
    with transaction(conn):
        if _one(conn, "SELECT 1 FROM subtasks WHERE plan_id = ? AND status = 'running'", (plan_id,)):
            raise Busy("des sous-tâches sont encore en cours")
        if ready_subtasks(conn, plan_id):
            raise StoreError("des sous-tâches peuvent encore être lancées")
        now = utc_now()
        conn.execute(
            "UPDATE subtasks SET status = 'stopped', finished_at = ? "
            "WHERE plan_id = ? AND status = 'pending'",
            (now, plan_id),
        )
        failed = _one(conn, "SELECT 1 FROM subtasks WHERE plan_id = ? AND status <> 'ok'", (plan_id,))
        conn.execute(
            "UPDATE plans SET status = ?, finished_at = ? WHERE id = ?",
            ("error" if failed else "done", now, plan_id),
        )


def stop_plan(conn, plan_id):
    """Stop a plan at any stage before its end: nothing more is run.

    Active and waiting subtasks are marked stopped. Returns the ids of the
    subtasks that were running, so their processes can be killed.
    """
    plan = _get_plan_row(conn, plan_id)
    if plan["status"] not in ("proposed", "validated", "running"):
        raise StoreError("le plan est déjà terminé")
    with transaction(conn):
        running = [r["id"] for r in conn.execute(
            "SELECT id FROM subtasks WHERE plan_id = ? AND status = 'running' ORDER BY position",
            (plan_id,),
        )]
        now = utc_now()
        conn.execute(
            "UPDATE subtasks SET status = 'stopped', finished_at = ? "
            "WHERE plan_id = ? AND status IN ('pending', 'running')",
            (now, plan_id),
        )
        conn.execute(
            "UPDATE plans SET status = 'stopped', finished_at = ? WHERE id = ?", (now, plan_id)
        )
    return running


# --- favorite requests (« Demandes favorites ») -------------------------------------

FAVORITE_TITLE_MAX = 80
FAVORITE_TEXT_MAX = 20000
FAVORITES_MAX = 100  # per person


def list_favorites(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, title, text, created_at FROM favorites WHERE user_id IS ? ORDER BY title COLLATE NOCASE, id",
        (user_id,))]


def add_favorite(conn, user_id, title, text):
    """A request kept to be inserted again in a click; the title shows in the list."""
    text = str(text or "").strip()
    if not text:
        raise StoreError("une demande favorite ne peut pas être vide")
    if len(text) > FAVORITE_TEXT_MAX:
        raise StoreError(f"demande trop longue ({FAVORITE_TEXT_MAX} caractères au plus)")
    title = " ".join(str(title or "").split()) or " ".join(text.split())[:FAVORITE_TITLE_MAX]
    title = title[:FAVORITE_TITLE_MAX]
    count = conn.execute("SELECT COUNT(*) FROM favorites WHERE user_id IS ?", (user_id,)).fetchone()[0]
    if count >= FAVORITES_MAX:
        raise StoreError(f"déjà {FAVORITES_MAX} demandes favorites : retirez-en une d'abord")
    return conn.execute("INSERT INTO favorites (user_id, title, text, created_at) VALUES (?, ?, ?, ?)",
                        (user_id, title, text, utc_now())).lastrowid


def delete_favorite(conn, user_id, favorite_id):
    """Only one's own: someone else's is as unknown as one that does not exist."""
    if conn.execute("DELETE FROM favorites WHERE id = ? AND user_id IS ?", (favorite_id, user_id)).rowcount == 0:
        raise NotFound("demande favorite introuvable")


# --- regenerate the last reply, branch at a request ----------------------------------

def last_exchange(conn, conversation_id):
    """(request, reply) when the conversation ends with a request and its reply, both finished; else None."""
    rows = conn.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 2",
                        (conversation_id,)).fetchall()
    if len(rows) != 2:
        return None
    reply, request = rows
    if request["role"] != "user" or reply["role"] != "assistant" or reply["status"] in ("pending", "running"):
        return None
    return dict(request), dict(reply)


def forget_exchange(conn, request_id, reply_id):
    """The last exchange goes, for its request to be sent again (« Régénérer »)."""
    with transaction(conn):
        conn.execute("DELETE FROM messages WHERE id IN (?, ?)", (reply_id, request_id))


def copy_messages(conn, source_id, target_id, before_id, *, with_ids=False):
    """The finished messages of a conversation before `before_id`, copied into another (a branch).
    Returns how many. The copies have no session: the branch starts from the memory."""
    rows = conn.execute(
        "SELECT id, role, provider, model, content, status, user_id, created_at, finished_at, tokens_in, tokens_out, "
        "duration_ms FROM messages WHERE conversation_id = ? AND id < ? AND content <> '' "
        "AND status IN ('ok', 'stopped') ORDER BY id", (source_id, before_id)).fetchall()
    copied = {}
    with transaction(conn):
        for r in rows:
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, role, provider, model, content, status, user_id, created_at, "
                "finished_at, tokens_in, tokens_out, duration_ms) VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?, NULL, NULL, ?)",
                (target_id, r["role"], None, r["model"], r["content"], r["user_id"], r["created_at"],
                 r["finished_at"], r["duration_ms"]))
            copied[r["id"]] = cursor.lastrowid
    return copied if with_ids else len(rows)


# --- instructions the AI always gets (« Consignes ») --------------------------------------

INSTRUCTIONS_MAX = 4000


def check_instructions(text):
    text = str(text or "").strip()
    if len(text) > INSTRUCTIONS_MAX:
        raise StoreError(f"consignes trop longues ({INSTRUCTIONS_MAX} caractères au plus)")
    return text or None


def person_instructions(conn, conversation):
    """The project's instructions, then the conversation's, as one text for the AI; None without any."""
    parts = []
    if conversation["project_id"] is not None:
        project = conn.execute("SELECT instructions FROM projects WHERE id = ?", (conversation["project_id"],)).fetchone()
        if project is not None and project["instructions"]:
            parts.append(f"Pour tout le projet :\n{project['instructions']}")
    own = conversation["instructions"] if "instructions" in conversation.keys() else None
    if own:
        parts.append(f"Pour cette conversation :\n{own}")
    if not parts:
        return None
    return "Consignes de la personne, à suivre à chaque réponse.\n\n" + "\n\n".join(parts)


def join_instructions(*texts):
    return "\n\n".join(t for t in texts if t) or None
