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
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

COOKIE = "aihub_session"
MIN_PASSWORD = 12
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

def hash_password(password):
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"le mot de passe doit faire au moins {MIN_PASSWORD} caractères")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=_MAXMEM, dklen=32, **_SCRYPT)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
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
            "disabled": bool(row["disabled"])}


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
    """A person changes their own password: their other sessions are closed."""
    stored = hash_password(password)
    with db.transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (stored, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_users(conn):
    return [_user(r) | {"created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM users ORDER BY is_owner DESC, name")]


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user(row) if row else None


def create_user(conn, name, password):
    """The owner makes a person's account directly (no link): they give the
    person the name and the password, which the person can change after."""
    name = check_name(name)
    stored = hash_password(password)
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise AuthError(f"le nom « {name} » est déjà pris")
        user_id = conn.execute("INSERT INTO users (name, password_hash, created_at) VALUES (?, ?, ?)",
                               (name, stored, db.utc_now())).lastrowid
    return get_user(conn, user_id)


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


def invite(conn, note=None, now=None):
    """A one-time link for one person. Returns the token (shown once; only its hash is kept)."""
    token = secrets.token_urlsafe(24)
    now = now or datetime.now(timezone.utc)
    conn.execute("INSERT INTO invitations (token_hash, note, created_at, expires_at) VALUES (?, ?, ?, ?)",
                 (_token_hash(token), (note or "").strip()[:80] or None, _iso(now),
                  _iso(now + timedelta(days=INVITE_DAYS))))
    return token


def list_invitations(conn):
    return [dict(r) for r in conn.execute(
        "SELECT i.id, i.note, i.created_at, i.expires_at, i.used_at, u.name AS used_by FROM invitations i "
        "LEFT JOIN users u ON u.id = i.user_id ORDER BY i.id DESC")]


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

    def verify(self, conn, password, client, name=None):
        """Check a person's password, with the lockout, without opening a session.

        `name`: whose; empty for the owner (a server of one person types no
        name). Returns the person. Raises AuthError or Locked. Also used to
        confirm a sensitive change (the settings) made from an open session.
        """
        if not password_set(conn):
            raise AuthError("aucun mot de passe n'est défini : python -m aihub mot-de-passe")
        # A locked client is refused even with the right password: the lockout
        # must not be a way to test passwords for free.
        self.check_lock(conn, client)
        name = " ".join(str(name or "").split())
        row = (conn.execute("SELECT * FROM users WHERE name = ? AND disabled = 0", (name,)).fetchone() if name
               else conn.execute("SELECT * FROM users WHERE is_owner = 1").fetchone())
        # An unknown name costs the same and says the same as a wrong password.
        stored = row["password_hash"] if row is not None else _DUMMY_HASH
        if not verify_password(password or "", stored) or row is None:
            left = self._record_failure(conn, client)
            try:
                self.check_lock(conn, client)
            except Locked as exc:
                raise Locked(f"mot de passe incorrect. {exc}", exc.retry_after_s) from None
            raise AuthError(f"nom ou mot de passe incorrect ({left} essai(s) avant blocage)")
        with db.transaction(conn):
            conn.execute("DELETE FROM login_failures WHERE client = ?", (client,))
            conn.execute("DELETE FROM login_locks WHERE client = ?", (client,))
        return _user(row)

    def login(self, conn, password, client, user_agent=None, name=None):
        """Return a new session token, or raise AuthError / Locked."""
        user = self.verify(conn, password, client, name)
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
        stored = hash_password(password)
        with db.transaction(conn):
            if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise AuthError(f"le nom « {name} » est déjà pris")
            user_id = conn.execute("INSERT INTO users (name, password_hash, created_at) VALUES (?, ?, ?)",
                                   (name, stored, db.utc_now())).lastrowid
            conn.execute("UPDATE invitations SET used_at = ?, user_id = ? WHERE id = ?",
                         (_iso(self.now()), user_id, row["id"]))
        return self._open(conn, user_id, client, user_agent)

    def _expiry(self, created, now):
        return min(created + timedelta(days=self.policy.max_days), now + timedelta(days=self.policy.idle_days))

    def check(self, conn, token):
        """The person whose valid session the token opens (prolonged while used), or None."""
        if not token:
            return None
        row = conn.execute("SELECT s.*, u.name, u.is_owner, u.disabled FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token_hash = ?", (_token_hash(token),)).fetchone()
        if row is None or row["disabled"]:
            return None
        now = self.now()
        if _parse(row["expires_at"]) <= now:
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        if now - _parse(row["last_seen_at"]) >= _TOUCH_EVERY:
            conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                         (_iso(now), _iso(self._expiry(_parse(row["created_at"]), now)), row["id"]))
        return {"id": row["user_id"], "name": row["name"], "is_owner": bool(row["is_owner"]), "disabled": False}

    def logout(self, conn, token):
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- connected devices ---

    def sessions(self, conn, token=None):
        """The caller's open sessions (their devices), the most recently used first; `current` marks this one."""
        mine = _token_hash(token) if token else None
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(self.now()),))
        rows = conn.execute("SELECT id, token_hash, created_at, last_seen_at, expires_at, client, user_agent "
                            "FROM sessions WHERE user_id = (SELECT user_id FROM sessions WHERE token_hash = ?) "
                            "ORDER BY last_seen_at DESC, id DESC", (mine or "",)).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
                 "expires_at": r["expires_at"], "client": r["client"], "user_agent": r["user_agent"],
                 "current": r["token_hash"] == mine} for r in rows]

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
