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

def password_set(conn):
    return conn.execute("SELECT 1 FROM auth_password WHERE id = 1").fetchone() is not None


def set_password(conn, password):
    """Set or change the password. Every open session is closed with it."""
    stored = hash_password(password)
    with db.transaction(conn):
        conn.execute("INSERT INTO auth_password (id, password_hash, changed_at) VALUES (1, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, "
                     "changed_at = excluded.changed_at", (stored, db.utc_now()))
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM login_failures")
        conn.execute("DELETE FROM login_locks")


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

    def verify(self, conn, password, client):
        """Check the password, with the lockout, without opening a session.

        Raises AuthError or Locked. Also used to confirm a sensitive change
        (the settings) made from an open session.
        """
        row = conn.execute("SELECT password_hash FROM auth_password WHERE id = 1").fetchone()
        if row is None:
            raise AuthError("aucun mot de passe n'est défini : python -m aihub mot-de-passe")
        # A locked client is refused even with the right password: the lockout
        # must not be a way to test passwords for free.
        self.check_lock(conn, client)
        if not verify_password(password or "", row["password_hash"]):
            left = self._record_failure(conn, client)
            try:
                self.check_lock(conn, client)
            except Locked as exc:
                raise Locked(f"mot de passe incorrect. {exc}", exc.retry_after_s) from None
            raise AuthError(f"mot de passe incorrect ({left} essai(s) avant blocage)")
        with db.transaction(conn):
            conn.execute("DELETE FROM login_failures WHERE client = ?", (client,))
            conn.execute("DELETE FROM login_locks WHERE client = ?", (client,))

    def login(self, conn, password, client, user_agent=None):
        """Return a new session token, or raise AuthError / Locked."""
        self.verify(conn, password, client)
        token = secrets.token_urlsafe(32)
        now = self.now()
        with db.transaction(conn):
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))
            conn.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at, client, "
                         "user_agent) VALUES (?, ?, ?, ?, ?, ?)",
                         (_token_hash(token), _iso(now), _iso(now), _iso(self._expiry(now, now)),
                          client, (user_agent or "")[:200]))
        return token

    def _expiry(self, created, now):
        return min(created + timedelta(days=self.policy.max_days), now + timedelta(days=self.policy.idle_days))

    def check(self, conn, token):
        """True if the token opens a valid session; prolongs it while it is used."""
        if not token:
            return False
        row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
        if row is None:
            return False
        now = self.now()
        if _parse(row["expires_at"]) <= now:
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return False
        if now - _parse(row["last_seen_at"]) >= _TOUCH_EVERY:
            conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                         (_iso(now), _iso(self._expiry(_parse(row["created_at"]), now)), row["id"]))
        return True

    def logout(self, conn, token):
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- connected devices ---

    def sessions(self, conn, token=None):
        """The open sessions, the most recently used first; `current` marks the caller's."""
        mine = _token_hash(token) if token else None
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(self.now()),))
        rows = conn.execute("SELECT id, token_hash, created_at, last_seen_at, expires_at, client, user_agent "
                            "FROM sessions ORDER BY last_seen_at DESC, id DESC").fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
                 "expires_at": r["expires_at"], "client": r["client"], "user_agent": r["user_agent"],
                 "current": r["token_hash"] == mine} for r in rows]

    def session_id(self, conn, token):
        """The id of the caller's session (a device's notifications are tied to it), or None."""
        if not token:
            return None
        row = conn.execute("SELECT id FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
        return row["id"] if row else None

    def close_session(self, conn, session_id):
        """Log one device out. Returns True if it was open."""
        return conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

    def close_others(self, conn, token):
        """Log out every device but the caller's (a lost phone). Returns how many."""
        return conn.execute("DELETE FROM sessions WHERE token_hash <> ?",
                            (_token_hash(token) if token else "",)).rowcount
