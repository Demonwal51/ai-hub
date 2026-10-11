"""Who may see and do what, when several people share a server.

- The owner (who created the server) and the admins see and manage every
  conversation and project; only the owner changes the server's settings.
- A member (« membre ») makes conversations and projects of their own, and
  owns them; besides, like a guest, sees what is shared with them. Their own
  conversations have a folder of theirs on the server, never the mode without
  permission, and only the tools that touch nothing of the server's (the web,
  another AI's opinion): `trusted()` says who may go beyond.
- A guest (« invité ») sees only what is shared with them: a conversation,
  or a project (then all its conversations), read only or read and write.
  Shared directly and through its project: the higher role.
- Read: see the conversation, follow it live. Write: also send requests,
  which run with their own AI accounts (aihub/accounts.py), and answer the
  permission prompts of their own requests.
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


def sees_all(user):
    """The owner and the admins: every conversation and project, and the people."""
    return is_owner(user) or user.get("role") == "admin"


def trusted(user):
    """May choose any folder, the mode without permission, every tool, a check command."""
    return sees_all(user)


def can_create(user):
    return sees_all(user) or user.get("role") == "membre"


def _owns(user, row):
    return user is not None and row["owner_id"] is not None and row["owner_id"] == user["id"]


def conversation_role(conn, user, conversation):
    """owner, write, read, or None (not visible)."""
    if sees_all(user) or _owns(user, conversation):
        return "owner"
    if conversation["project_id"] is not None:
        project = conn.execute("SELECT owner_id FROM projects WHERE id = ?", (conversation["project_id"],)).fetchone()
        if project is not None and _owns(user, project):
            return "owner"  # in a project of theirs
    rows = conn.execute(
        "SELECT role FROM shares WHERE user_id = ? AND (conversation_id = ? OR (project_id IS NOT NULL AND project_id = ?))",
        (user["id"], conversation["id"], conversation["project_id"])).fetchall()
    roles = [r["role"] for r in rows]
    return max(roles, key=RANK.get) if roles else None


def project_role(conn, user, project_id):
    if sees_all(user):
        return "owner"
    project = conn.execute("SELECT owner_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is not None and _owns(user, project):
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
    row = conn.execute("SELECT id, is_owner, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return False
    return project_role(conn, dict(row), conversation["project_id"]) is not None


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


def require_admin(user):
    if not sees_all(user):
        raise Forbidden("réservé au propriétaire et aux admins")


def require_creator(user):
    if not can_create(user):
        raise Forbidden("un invité ne crée rien : il prend part à ce qu'on lui partage")


def require_sharer(conn, user, *, conversation_id=None, project_id=None):
    """Who shares: the owner and the admins, and a member for what is theirs."""
    if sees_all(user):
        return
    if conversation_id is not None:
        conversation = require(conn, user, conversation_id)
        mine = conversation_role(conn, user, conversation) == "owner"
    elif project_id is not None:
        mine = project_role(conn, user, project_id) == "owner"
    else:
        mine = False
    if not mine:
        raise Forbidden("seul celui qui l'a créé (ou un admin) le partage")


def visible_conversation_ids(conn, user):
    """None for « all » (the owner, an admin), else the set of ids this person may see."""
    if sees_all(user):
        return None
    me = user["id"]
    rows = conn.execute(
        "SELECT conversation_id AS id FROM shares WHERE user_id = ? AND conversation_id IS NOT NULL "
        "UNION SELECT c.id FROM conversations c JOIN shares s ON s.project_id = c.project_id WHERE s.user_id = ? "
        "UNION SELECT id FROM conversations WHERE owner_id = ? "
        "UNION SELECT c.id FROM conversations c JOIN projects p ON p.id = c.project_id WHERE p.owner_id = ?",
        (me, me, me, me)).fetchall()
    return {r["id"] for r in rows}


def visible_project_ids(conn, user):
    if sees_all(user):
        return None
    me = user["id"]
    rows = conn.execute(
        "SELECT project_id AS id FROM shares WHERE user_id = ? AND project_id IS NOT NULL "
        "UNION SELECT c.project_id FROM conversations c JOIN shares s ON s.conversation_id = c.id "
        "WHERE s.user_id = ? AND c.project_id IS NOT NULL "
        "UNION SELECT id FROM projects WHERE owner_id = ?", (me, me, me)).fetchall()
    return {r["id"] for r in rows}


def grants_of(conn, user_id):
    """What is shared with a person: [{conversation_id | project_id, title, role}]."""
    rows = conn.execute(
        "SELECT s.conversation_id, s.project_id, s.role, COALESCE(c.title, p.name) AS title FROM shares s "
        "LEFT JOIN conversations c ON c.id = s.conversation_id LEFT JOIN projects p ON p.id = s.project_id "
        "WHERE s.user_id = ? ORDER BY title", (user_id,)).fetchall()
    return [dict(r) for r in rows]


# --- sharing (the owner's) ---

def share(conn, *, user_id, role, conversation_id=None, project_id=None):
    if role not in ROLES:
        raise StoreError(f"droit inconnu : {role!r} (attendu : read, write)")
    if (conversation_id is None) == (project_id is None):
        raise StoreError("partager une conversation ou un projet")
    person = conn.execute("SELECT is_owner, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if person is None:
        raise NotFound("personne inconnue")
    if person["is_owner"] or person["role"] == "admin":
        raise StoreError("le propriétaire et les admins voient déjà tout")
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
    """The people (ids) besides the owner and the admins who see a conversation: its alerts go to them too."""
    conversation = store.get_conversation(conn, conversation_id)
    rows = conn.execute("SELECT DISTINCT user_id FROM shares WHERE conversation_id = ? "
                        "OR (project_id IS NOT NULL AND project_id = ?) "
                        "UNION SELECT owner_id FROM conversations WHERE id = ? AND owner_id IS NOT NULL "
                        "UNION SELECT owner_id FROM projects WHERE id = ? AND owner_id IS NOT NULL",
                        (conversation_id, conversation["project_id"], conversation_id,
                         conversation["project_id"])).fetchall()
    return {r["user_id"] for r in rows}
