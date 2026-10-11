"""Password, sessions and lockout, for the access from the phone.

There is one password (the owner's), kept as a scrypt hash in the database. A
successful login opens a session: a random token in an HttpOnly cookie, and
only its SHA-256 in the database. A session ends after `idle_days` without
use, and after `max_days` in any case.

Wrong passwords are counted by client address. After `max_failures` within
`window_minutes`, the address is locked for `lock_minutes`, doubled at each
new lockout (capped at a day); a successful login clears it. Above
`global_max_failures` wrong passwords from all addresses in the window,
every login is locked: spreading the attempts over many addresses does not
get around the lockout.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

COOKIE = "aihub_session"
MIN_PASSWORD = 8
ROLES = ("invite", "membre", "admin")
ROLE_NAMES = {"invite": "Invité", "membre": "Membre", "admin": "Admin"}
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024
_EVERYONE = "*"                      # the client name of the global lockout
_MAX_LOCK = timedelta(days=1)
_TOUCH_EVERY = timedelta(minutes=1)  # last_seen_at is not rewritten at every request


class AuthError(Exception):
    """A refusal fit for the user."""


class Locked(AuthError):
    def __init__(self, message, retry_after_s):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Policy:
    # The password once per device: a session stays
    # open while the device is used at least once a year. A lost phone is
    # logged out from « Appareils », or by changing the password.
    idle_days: float = 365
    max_days: float = 3650
    max_failures: int = 5
    window_minutes: float = 15
    lock_minutes: float = 15
    global_max_failures: int = 20


# --- password hash -----------------------------------------------------------

def hash_password(password, optional=False):
    """The scrypt hash; with `optional`, an empty password is « none » (an empty hash)."""
    if optional and password == "":
        return ""
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"le mot de passe doit faire au moins {MIN_PASSWORD} caractères")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=_MAXMEM, dklen=32, **_SCRYPT)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
    if stored == "":
        return password == ""  # an account without a password: the name is enough
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def _iso(when):
    return when.isoformat(timespec="milliseconds")


def _parse(text):
    return datetime.fromisoformat(text)


def _minutes(delta):
    return max(1, int(-(-delta.total_seconds() // 60)))  # rounded up, at least 1


# --- the store -----------------------------------------------------------------

OWNER_NAME = "Propriétaire"   # the owner's name until they choose one
# Compared against when the name is unknown: the answer takes as long as for a real one.
_DUMMY_HASH = "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_NAME_MAX = 40


def password_set(conn):
    """True once the server has an owner (whose password protects it)."""
    return conn.execute("SELECT 1 FROM users WHERE is_owner = 1").fetchone() is not None


def owner(conn):
    row = conn.execute("SELECT * FROM users WHERE is_owner = 1").fetchone()
    return _user(row) if row else None


def _user(row):
    return {"id": row["id"], "name": row["name"], "is_owner": bool(row["is_owner"]),
            "disabled": bool(row["disabled"]), "role": "admin" if row["is_owner"] else row["role"],
            "has_password": bool(row["password_hash"])}


def check_role(role):
    if role not in ROLES:
        raise AuthError(f"rôle inconnu : {role!r} (invite, membre ou admin)")
    return role


# --- the server's own password (optional) ---

def server_password_set(conn):
    return conn.execute("SELECT 1 FROM server_lock WHERE id = 1").fetchone() is not None


def set_server_password(conn, password):
    """Set, change or (empty) remove the server's password. The open sessions stay:
    they already went through it."""
    if not password:
        conn.execute("DELETE FROM server_lock")
        return
    conn.execute("INSERT INTO server_lock (id, password_hash, changed_at) VALUES (1, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, changed_at = excluded.changed_at",
                 (hash_password(password), db.utc_now()))


def setup(conn, name, password="", server_password=""):
    """Create the server: its owner (a name, a password or none) and its own password, if any."""
    name = check_name(name)
    stored = hash_password(password or "", optional=True)
    with db.transaction(conn):
        if password_set(conn):
            raise AuthError("ce serveur a déjà son propriétaire")
        conn.execute("INSERT INTO users (name, password_hash, is_owner, role, created_at) VALUES (?, ?, 1, 'admin', ?)",
                     (name, stored, db.utc_now()))
        if server_password:
            set_server_password(conn, server_password)
    return owner(conn)


def _apply_shares(conn, user_id, shares):
    """Shares given with an account: [{conversation_id | project_id, role}]; gone targets are skipped."""
    for share in shares or []:
        role = share.get("role")
        if role not in ("read", "write"):
            continue
        for column, table in (("conversation_id", "conversations"), ("project_id", "projects")):
            target = share.get(column)
            if isinstance(target, int) and conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (target,)).fetchone():
                conn.execute(f"DELETE FROM shares WHERE {column} = ? AND user_id = ?", (target, user_id))
                conn.execute(f"INSERT INTO shares ({column}, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                             (target, user_id, role, db.utc_now()))


def check_name(name):
    name = " ".join(str(name or "").split())
    if not name or len(name) > _NAME_MAX:
        raise AuthError(f"nom : de 1 à {_NAME_MAX} caractères")
    return name


def set_password(conn, password):
    """Set or change the owner's password. Every open session is closed with it
    (the others' too: a server whose password changed is a server to secure)."""
    stored = hash_password(password)
    with db.transaction(conn):
        if conn.execute("UPDATE users SET password_hash = ? WHERE is_owner = 1", (stored,)).rowcount == 0:
            conn.execute("INSERT INTO users (name, password_hash, is_owner, created_at) VALUES (?, ?, 1, ?)",
                         (OWNER_NAME, stored, db.utc_now()))
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM login_failures")
        conn.execute("DELETE FROM login_locks")


def set_user_password(conn, user_id, password):
    """A person changes their own password (or, empty, goes without): their other sessions are closed."""
    stored = hash_password(password, optional=True)
    with db.transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (stored, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_users(conn):
    return [_user(r) | {"created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM users ORDER BY is_owner DESC, name")]


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def create_user(conn, name, password="", role="invite", shares=None):
    """The owner (or an admin) makes a person's account directly (no link): they give
    the person the name and the password (or none), which the person can change after."""
    name = check_name(name)
    check_role(role)
    stored = hash_password(password or "", optional=True)
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise AuthError(f"le nom « {name} » est déjà pris")
        user_id = conn.execute("INSERT INTO users (name, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                               (name, stored, role, db.utc_now())).lastrowid
        _apply_shares(conn, user_id, shares)
    return get_user(conn, user_id)


def set_role(conn, user_id, role):
    check_role(role)
    row = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("personne inconnue")
    if row["is_owner"]:
        raise AuthError("le propriétaire a tous les droits : son rôle ne change pas")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def rename_user(conn, user_id, name):
    """A new name to log in with (the owner may rename anyone, themselves too)."""
    name = check_name(name)
    if get_user(conn, user_id) is None:
        raise AuthError("personne inconnue")
    with db.transaction(conn):
        taken = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        if taken is not None and taken["id"] != user_id:
            raise AuthError(f"le nom « {name} » est déjà pris")
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
    return get_user(conn, user_id)


def set_disabled(conn, user_id, disabled):
    """The owner turns a person off (or on again): their sessions close at once."""
    row = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("personne inconnue")
    if row["is_owner"]:
        raise AuthError("le propriétaire ne peut pas être désactivé")
    with db.transaction(conn):
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(disabled)), user_id))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(conn, user_id):
    """Remove a person: their sessions and shares go; their messages stay, without a name."""
    row = conn.execute("SELECT is_owner FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("personne inconnue")
    if row["is_owner"]:
        raise AuthError("le propriétaire ne peut pas être supprimé")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- invitations ---

INVITE_DAYS = 7


def invite(conn, note=None, now=None, role="invite", shares=None):
    """A one-time link for one person, who will have `role` and `shares` once they come.
    Returns the token (shown once; only its hash is kept)."""
    check_role(role)
    token = secrets.token_urlsafe(24)
    now = now or datetime.now(timezone.utc)
    conn.execute("INSERT INTO invitations (token_hash, note, created_at, expires_at, role, shares_json) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (_token_hash(token), (note or "").strip()[:80] or None, _iso(now),
                  _iso(now + timedelta(days=INVITE_DAYS)), role, json.dumps(shares or [])))
    return token


def list_invitations(conn):
    return [dict(r) for r in conn.execute(
        "SELECT i.id, i.note, i.created_at, i.expires_at, i.used_at, i.role, u.name AS used_by FROM invitations i "
        "LEFT JOIN users u ON u.id = i.user_id ORDER BY i.id DESC")]


DEVICE_LINK_MINUTES = 15
LABEL_MAX = 60


def check_label(label):
    label = " ".join(str(label or "").split())
    return label[:LABEL_MAX] or None


def device_link(conn, user_id, label=None, now=None):
    """A one-time link (shown as a QR code) that connects a device to this person's account.
    Returns the token (shown once; only its hash is kept)."""
    if get_user(conn, user_id) is None:
        raise AuthError("personne inconnue")
    token = secrets.token_urlsafe(24)
    now = now or datetime.now(timezone.utc)
    conn.execute("INSERT INTO device_links (token_hash, user_id, label, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (_token_hash(token), user_id, check_label(label), _iso(now),
                  _iso(now + timedelta(minutes=DEVICE_LINK_MINUTES))))
    return token


def revoke_invitation(conn, invitation_id):
    return conn.execute("DELETE FROM invitations WHERE id = ? AND used_at IS NULL", (invitation_id,)).rowcount > 0


class Auth:
    def __init__(self, policy=None, now=None):
        self.policy = policy or Policy()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def now(self):
        return self._now()

    # --- lockout ---

    def _lock_left(self, conn, client):
        row = conn.execute("SELECT until FROM login_locks WHERE client = ?", (client,)).fetchone()
        if row is None:
            return None
        left = _parse(row["until"]) - self.now()
        return left if left > timedelta(0) else None

    def check_lock(self, conn, client):
        for who in (client, _EVERYONE):
            left = self._lock_left(conn, who)
            if left is not None:
                raise Locked(f"trop de mots de passe erronés : réessayez dans {_minutes(left)} min",
                             int(left.total_seconds()) + 1)

    def _record_failure(self, conn, client):
        now = self.now()
        since = _iso(now - timedelta(minutes=self.policy.window_minutes))
        with db.transaction(conn):
            conn.execute("INSERT INTO login_failures (client, at) VALUES (?, ?)", (client, _iso(now)))
            conn.execute("DELETE FROM login_failures WHERE at < ?", (since,))
            mine = conn.execute("SELECT COUNT(*) FROM login_failures WHERE client = ? AND at >= ?",
                                (client, since)).fetchone()[0]
            everyone = conn.execute("SELECT COUNT(*) FROM login_failures WHERE at >= ?",
                                    (since,)).fetchone()[0]
            if mine >= self.policy.max_failures:
                self._lock(conn, client, now)
                conn.execute("DELETE FROM login_failures WHERE client = ?", (client,))
            if everyone >= self.policy.global_max_failures:
                self._lock(conn, _EVERYONE, now)
                conn.execute("DELETE FROM login_failures")
            return max(0, self.policy.max_failures - mine)

    def _lock(self, conn, client, now):
        row = conn.execute("SELECT until, count FROM login_locks WHERE client = ?", (client,)).fetchone()
        # A lockout over for more than a day is forgotten: the next one starts short again.
        recent = row is not None and _parse(row["until"]) > now - _MAX_LOCK
        count = row["count"] + 1 if recent else 1
        duration = min(timedelta(minutes=self.policy.lock_minutes) * 2 ** (count - 1), _MAX_LOCK)
        conn.execute("INSERT INTO login_locks (client, until, count) VALUES (?, ?, ?) "
                     "ON CONFLICT(client) DO UPDATE SET until = excluded.until, count = excluded.count",
                     (client, _iso(now + duration), count))

    # --- login and sessions ---

    def verify(self, conn, password, client, name=None, server_password=None):
        """Check a person's password, with the lockout, without opening a session.

        `name`: whose; empty for the owner (a server of one person types no
        name). `server_password`: checked when the server has one (a login);
        None skips it (confirming a change from an open session, which went
        through it already). Returns the person. Raises AuthError or Locked.
        """
        if not password_set(conn):
            raise AuthError("ce serveur n'est pas encore créé : ouvrez l'appli sur cet ordinateur")
        # A locked client is refused even with the right password: the lockout
        # must not be a way to test passwords for free.
        self.check_lock(conn, client)
        if server_password is not None:
            lock = conn.execute("SELECT password_hash FROM server_lock WHERE id = 1").fetchone()
            if lock is not None and not verify_password(server_password, lock["password_hash"]):
                self._refuse(conn, client, "mot de passe du serveur incorrect")
        name = " ".join(str(name or "").split())
        row = (conn.execute("SELECT * FROM users WHERE name = ? AND disabled = 0", (name,)).fetchone() if name
               else conn.execute("SELECT * FROM users WHERE is_owner = 1").fetchone())
        # An unknown name costs the same and says the same as a wrong password.
        stored = row["password_hash"] if row is not None else _DUMMY_HASH
        if not verify_password(password or "", stored) or row is None:
            self._refuse(conn, client, "nom ou mot de passe incorrect")
        with db.transaction(conn):
            conn.execute("DELETE FROM login_failures WHERE client = ?", (client,))
            conn.execute("DELETE FROM login_locks WHERE client = ?", (client,))
        return _user(row)

    def _refuse(self, conn, client, reason):
        left = self._record_failure(conn, client)
        try:
            self.check_lock(conn, client)
        except Locked as exc:
            raise Locked(f"mot de passe incorrect. {exc}", exc.retry_after_s) from None
        raise AuthError(f"{reason} ({left} essai(s) avant blocage)")

    def login(self, conn, password, client, user_agent=None, name=None, server_password=""):
        """Return a new session token, or raise AuthError / Locked."""
        user = self.verify(conn, password, client, name, server_password=server_password or "")
        return self._open(conn, user["id"], client, user_agent)

    def _open(self, conn, user_id, client, user_agent):
        token = secrets.token_urlsafe(32)
        now = self.now()
        with db.transaction(conn):
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))
            conn.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at, client, "
                         "user_agent, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (_token_hash(token), _iso(now), _iso(now), _iso(self._expiry(now, now)),
                          client, (user_agent or "")[:200], user_id))
        return token

    def accept_invitation(self, conn, invitation, name, password, client, user_agent=None):
        """A person comes with their link, chooses a name and a password. Returns a session token.

        The link works once, before it expires; a wrong link counts for the
        lockout like a wrong password (links must not be guessed for free)."""
        self.check_lock(conn, client)
        row = conn.execute("SELECT * FROM invitations WHERE token_hash = ?",
                           (_token_hash(str(invitation or "")),)).fetchone()
        if row is None or row["used_at"] is not None or _parse(row["expires_at"]) <= self.now():
            self._record_failure(conn, client)
            raise AuthError("lien d'invitation inconnu, déjà utilisé ou expiré : demandez-en un autre")
        name = check_name(name)
        stored = hash_password(password or "", optional=True)
        with db.transaction(conn):
            if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise AuthError(f"le nom « {name} » est déjà pris")
            # The role and the accesses chosen when the link was made.
            user_id = conn.execute("INSERT INTO users (name, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                                   (name, stored, row["role"], db.utc_now())).lastrowid
            _apply_shares(conn, user_id, json.loads(row["shares_json"] or "[]"))
            conn.execute("UPDATE invitations SET used_at = ?, user_id = ? WHERE id = ?",
                         (_iso(self.now()), user_id, row["id"]))
        return self._open(conn, user_id, client, user_agent)

    def accept_device_link(self, conn, link, client, user_agent=None):
        """A device comes with its link (its QR code): a session of that account opens, named.
        The link works once, within its minutes; a wrong one counts for the lockout."""
        self.check_lock(conn, client)
        row = conn.execute("SELECT l.*, u.disabled FROM device_links l JOIN users u ON u.id = l.user_id "
                           "WHERE l.token_hash = ?", (_token_hash(str(link or "")),)).fetchone()
        refused = "code de connexion inconnu, déjà utilisé ou expiré : faites-en afficher un autre"
        if row is None or row["disabled"] or _parse(row["expires_at"]) <= self.now():
            self._record_failure(conn, client)
            raise AuthError(refused)
        # Used once: taken in one statement, so two devices scanning at once cannot both come in.
        if conn.execute("UPDATE device_links SET used_at = ? WHERE id = ? AND used_at IS NULL",
                        (_iso(self.now()), row["id"])).rowcount != 1:
            self._record_failure(conn, client)
            raise AuthError(refused)
        token = self._open(conn, row["user_id"], client, user_agent)
        conn.execute("UPDATE sessions SET label = ? WHERE token_hash = ?", (row["label"], _token_hash(token)))
        return token

    def rename_session(self, conn, session_id, label, token):
        """Name one of the caller's devices. Returns True if it was theirs."""
        return conn.execute("UPDATE sessions SET label = ? WHERE id = ? AND user_id = "
                            "(SELECT user_id FROM sessions WHERE token_hash = ?)",
                            (check_label(label), session_id, _token_hash(token) if token else "")).rowcount > 0

    def _expiry(self, created, now):
        return min(created + timedelta(days=self.policy.max_days), now + timedelta(days=self.policy.idle_days))

    def check(self, conn, token):
        """The person whose valid session the token opens (prolonged while used), or None."""
        if not token:
            return None
        row = conn.execute("SELECT s.*, u.name, u.is_owner, u.disabled, u.role FROM sessions s "
                           "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (_token_hash(token),)).fetchone()
        if row is None or row["disabled"]:
            return None
        now = self.now()
        if _parse(row["expires_at"]) <= now:
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        if now - _parse(row["last_seen_at"]) >= _TOUCH_EVERY:
            conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                         (_iso(now), _iso(self._expiry(_parse(row["created_at"]), now)), row["id"]))
        return {"id": row["user_id"], "name": row["name"], "is_owner": bool(row["is_owner"]), "disabled": False,
                "role": "admin" if row["is_owner"] else row["role"]}

    def logout(self, conn, token):
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- connected devices ---

    def sessions(self, conn, token=None):
        """The caller's open sessions (their devices), the most recently used first; `current` marks this one."""
        mine = _token_hash(token) if token else None
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(self.now()),))
        rows = conn.execute("SELECT id, token_hash, created_at, last_seen_at, expires_at, client, user_agent, label "
                            "FROM sessions WHERE user_id = (SELECT user_id FROM sessions WHERE token_hash = ?) "
                            "ORDER BY last_seen_at DESC, id DESC", (mine or "",)).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
                 "expires_at": r["expires_at"], "client": r["client"], "user_agent": r["user_agent"],
                 "label": r["label"], "current": r["token_hash"] == mine} for r in rows]

    def devices_of(self, conn, user_id):
        """A person's devices (for the owner and the admins, in « Personnes »): names and last use."""
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(self.now()),))
        return [dict(r) for r in conn.execute(
            "SELECT id, label, user_agent, last_seen_at FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
            (user_id,))]

    def session_id(self, conn, token):
        """The id of the caller's session (a device's notifications are tied to it), or None."""
        if not token:
            return None
        row = conn.execute("SELECT id FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
        return row["id"] if row else None

    def close_session(self, conn, session_id, token=None):
        """Log one of the caller's devices out. Returns True if it was open."""
        return conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = "
                            "(SELECT user_id FROM sessions WHERE token_hash = ?)",
                            (session_id, _token_hash(token) if token else "")).rowcount > 0

    def close_others(self, conn, token):
        """Log out every device of the caller but this one (a lost phone). Returns how many."""
        mine = _token_hash(token) if token else ""
        return conn.execute("DELETE FROM sessions WHERE token_hash <> ? AND user_id = "
                            "(SELECT user_id FROM sessions WHERE token_hash = ?)", (mine, mine)).rowcount
