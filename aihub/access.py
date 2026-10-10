"""Who may see and do what, when several people share a server (version 0.2).

- The owner (who set the server's password) owns every conversation and
  project, as before accounts existed.
- A person who came by invitation sees only what is shared with them: a
  conversation, or a project (then all its conversations), read only or
  read and write. Shared directly and through its project: the higher role.
- Read: see the conversation, follow it live. Write: also send requests,
  which run with their own AI accounts (aihub/accounts.py), and answer the
  permission prompts of their own requests. Everything else (folder,
  permission mode, AI, tools, sharing, deletion, settings) is the owner's.
- Without accounts (no password, this computer only), there is no one but
  the owner: `user` is None and may do everything.

A conversation one may not see is « introuvable », exactly as if it did not
exist: nothing leaks about the others.
"""

from datetime import datetime, timezone

from . import store
from .store import NotFound, StoreError

RANK = {None: 0, "read": 1, "write": 2, "owner": 3}
ROLES = ("read", "write")


class Forbidden(StoreError):
    """Visible, but this action is not this person's to do."""


def is_owner(user):
    return user is None or bool(user.get("is_owner"))


def conversation_role(conn, user, conversation):
    """owner, write, read, or None (not visible)."""
    if is_owner(user):
        return "owner"
    rows = conn.execute(
        "SELECT role FROM shares WHERE user_id = ? AND (conversation_id = ? OR (project_id IS NOT NULL AND project_id = ?))",
        (user["id"], conversation["id"], conversation["project_id"])).fetchall()
    roles = [r["role"] for r in rows]
    return max(roles, key=RANK.get) if roles else None


def project_role(conn, user, project_id):
    if is_owner(user):
        return "owner"
    row = conn.execute("SELECT role FROM shares WHERE user_id = ? AND project_id = ?", (user["id"], project_id)).fetchone()
    return row["role"] if row else None


def reads_project(conn, user_id, conversation):
    """May a request of this person let its agent read the conversation's project?

    Yes for the owner (user_id None or the owner's); for a guest, only when
    the whole project is shared with them: a guest who got one conversation
    must not have the agent read the others for them."""
    if user_id is None or conversation["project_id"] is None:
        return True
    row = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["is_owner"]:
        return row is not None
    return conn.execute("SELECT 1 FROM shares WHERE user_id = ? AND project_id = ?",
                        (user_id, conversation["project_id"])).fetchone() is not None


def require(conn, user, conversation_id, need="read"):
    """The conversation, if `user` may do `need` there; NotFound if they may not even see it."""
    try:
        conversation = store.get_conversation(conn, conversation_id)
    except NotFound:
        raise NotFound("conversation introuvable") from None
    role = conversation_role(conn, user, conversation)
    if role is None:
        raise NotFound("conversation introuvable")
    if RANK[role] < RANK[need]:
        raise Forbidden("lecture seule : cette conversation vous est partagée sans droit d'écriture"
                        if need == "write" else "réservé au propriétaire de la conversation")
    return conversation


def require_owner(user):
    if not is_owner(user):
        raise Forbidden("réservé au propriétaire du serveur")


def visible_conversation_ids(conn, user):
    """None for « all » (the owner), else the set of ids this person may see."""
    if is_owner(user):
        return None
    rows = conn.execute(
        "SELECT conversation_id AS id FROM shares WHERE user_id = ? AND conversation_id IS NOT NULL "
        "UNION SELECT c.id FROM conversations c JOIN shares s ON s.project_id = c.project_id WHERE s.user_id = ?",
        (user["id"], user["id"])).fetchall()
    return {r["id"] for r in rows}


def visible_project_ids(conn, user):
    if is_owner(user):
        return None
    rows = conn.execute(
        "SELECT project_id AS id FROM shares WHERE user_id = ? AND project_id IS NOT NULL "
        "UNION SELECT c.project_id FROM conversations c JOIN shares s ON s.conversation_id = c.id "
        "WHERE s.user_id = ? AND c.project_id IS NOT NULL", (user["id"], user["id"])).fetchall()
    return {r["id"] for r in rows}


# --- sharing (the owner's) ---

def share(conn, *, user_id, role, conversation_id=None, project_id=None):
    if role not in ROLES:
        raise StoreError(f"droit inconnu : {role!r} (attendu : read, write)")
    if (conversation_id is None) == (project_id is None):
        raise StoreError("partager une conversation ou un projet")
    person = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if person is None:
        raise NotFound("personne inconnue")
    if person["is_owner"]:
        raise StoreError("le propriétaire voit déjà tout")
    if conversation_id is not None:
        store.get_conversation(conn, conversation_id)
        column, target = "conversation_id", conversation_id
    else:
        store.get_project(conn, project_id)
        column, target = "project_id", project_id
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = conn.execute(f"UPDATE shares SET role = ? WHERE {column} = ? AND user_id = ?",
                           (role, target, user_id)).rowcount
    if not updated:
        conn.execute(f"INSERT INTO shares ({column}, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                     (target, user_id, role, now))


def unshare(conn, *, user_id, conversation_id=None, project_id=None):
    column, target = ("conversation_id", conversation_id) if conversation_id is not None else ("project_id", project_id)
    return conn.execute(f"DELETE FROM shares WHERE {column} = ? AND user_id = ?", (target, user_id)).rowcount > 0


def shares_of(conn, *, conversation_id=None, project_id=None):
    """[{user_id, name, role, through}] — through: « conversation » or « projet »."""
    if conversation_id is not None:
        conversation = store.get_conversation(conn, conversation_id)
        rows = conn.execute(
            "SELECT s.user_id, u.name, s.role, CASE WHEN s.conversation_id IS NULL THEN 'projet' ELSE 'conversation' END "
            "AS through FROM shares s JOIN users u ON u.id = s.user_id WHERE s.conversation_id = ? "
            "OR (s.project_id IS NOT NULL AND s.project_id = ?) ORDER BY u.name",
            (conversation_id, conversation["project_id"])).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.user_id, u.name, s.role, 'projet' AS through FROM shares s JOIN users u ON u.id = s.user_id "
            "WHERE s.project_id = ? ORDER BY u.name", (project_id,)).fetchall()
    return [dict(r) for r in rows]


def readers(conn, conversation_id):
    """The people (ids) besides the owner who see a conversation: its alerts go to them too."""
    conversation = store.get_conversation(conn, conversation_id)
    rows = conn.execute("SELECT DISTINCT user_id FROM shares WHERE conversation_id = ? "
                        "OR (project_id IS NOT NULL AND project_id = ?)",
                        (conversation_id, conversation["project_id"])).fetchall()
    return {r["user_id"] for r in rows}
