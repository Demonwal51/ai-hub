"""Phone notifications: Web Push to the devices that asked, and ntfy if set.

The person chooses: each device turns notifications on and picks its
alerts (« Notifications » in the sidebar); ntfy, an outside notification
app, is set in the settings, with its own alerts and how much it says.

Web Push (RFC 8030): the message is encrypted for the device (RFC 8291,
aes128gcm) and signed with the server's key (VAPID, RFC 8292); the push
service of the browser (Google, Mozilla, Apple, Microsoft) only relays
bytes it cannot read. Only those services' addresses are accepted, so a
subscription cannot make the server post to any address.

ntfy: the text goes through the ntfy server in clear; by default it only
says what happened, not in which conversation.

Sending never blocks an agent and never fails a request: a background
thread delivers, failures are counted and logged.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import struct
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db

log = logging.getLogger("aihub.notify")

EVENTS = {
    "permission": "Une IA attend votre accord",
    "done": "Réponse prête",
    "error": "Une requête a échoué",
    "plan": "Plan proposé, à valider",
    "plan_done": "Plan terminé",
    "autonomous_done": "Travail autonome terminé",
    "scheduled": "Tâche planifiée terminée",
    "quota": "Quota bientôt atteint",
}
DETAILS = ("minimal", "titre")   # ntfy: the event alone, or with the conversation's title

# The push services of the browsers (host suffixes). Any other endpoint is refused.
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
              "push.apple.com", "notify.windows.com")
_SUBJECT = "mailto:aihub@localhost"   # VAPID contact; no real address leaves the PC
_RECORD_SIZE = 4096
MAX_FAILURES = 5                      # a device that keeps refusing is dropped
_TTL = {"permission": 1800}           # seconds a push service keeps an undelivered message
_DEFAULT_TTL = 86400


class NotifyError(Exception):
    """A refused subscription or channel; the message is fit for the user."""


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- the server's key (VAPID) ------------------------------------------------

def server_key(conn):
    """(private key, public key as base64url), made at the first use and kept in the base."""
    row = conn.execute("SELECT private_pem, public_key FROM push_key WHERE id = 1").fetchone()
    if row is None:
        private = ec.generate_private_key(ec.SECP256R1())
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode("ascii")
        public = b64url(private.public_key().public_bytes(serialization.Encoding.X962,
                                                          serialization.PublicFormat.UncompressedPoint))
        conn.execute("INSERT OR IGNORE INTO push_key (id, private_pem, public_key, created_at) VALUES (1, ?, ?, ?)",
                     (pem, public, db.utc_now()))
        row = conn.execute("SELECT private_pem, public_key FROM push_key WHERE id = 1").fetchone()
    private = serialization.load_pem_private_key(row["private_pem"].encode("ascii"), password=None)
    return private, row["public_key"]


def vapid_header(private, public_key, endpoint, now=None):
    """The Authorization header of a push (RFC 8292): a JWT signed with ES256."""
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int((now or time.time()) + 12 * 3600),
              "sub": _SUBJECT}
    head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    der = private.sign(f"{head}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{signature}, k={public_key}"


# --- encryption for one device (RFC 8291) -------------------------------------

def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(plaintext, p256dh, auth, *, salt=None, ephemeral=None):
    """The aes128gcm body for a device whose keys are p256dh and auth (base64url)."""
    ua_public = unb64url(p256dh)
    auth_secret = unb64url(auth)
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise NotifyError("clés d'abonnement illisibles")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ephemeral = ephemeral or ec.generate_private_key(ec.SECP256R1())
    as_public = ephemeral.public_key().public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint)
    shared = ephemeral.exchange(ec.ECDH(), peer)
    salt = salt or os.urandom(16)
    ikm = _hmac(_hmac(auth_secret, shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 17 > _RECORD_SIZE:
        raise NotifyError("notification trop longue")
    record = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # \x02: the last (only) record
    return salt + struct.pack("!IB", _RECORD_SIZE, len(as_public)) + as_public + record


def check_endpoint(endpoint):
    parts = urlsplit(str(endpoint or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):
        raise NotifyError("adresse de notification refusée : seuls les services des navigateurs sont acceptés")
    return parts.geturl()


def check_events(events):
    if not isinstance(events, list) or any(e not in EVENTS for e in events):
        raise NotifyError(f"alertes inconnues (attendu : {', '.join(EVENTS)})")
    return [e for e in EVENTS if e in events]  # always in the same order


def check_ntfy_url(url):
    """https, or http to this computer only (a ntfy server of one's own)."""
    parts = urlsplit(str(url or "").strip())
    local = (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local):
        raise NotifyError("adresse ntfy : https attendu (http seulement vers cet ordinateur)")
    if not parts.hostname or parts.path.strip("/") == "" or "/" in parts.path.strip("/"):
        raise NotifyError("adresse ntfy : indiquez le sujet, par exemple https://ntfy.sh/mon-sujet-secret")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise NotifyError("adresse ntfy : sans paramètres ni identifiants")
    return parts.geturl()


# --- subscriptions ------------------------------------------------------------

def subscribe(conn, subscription, events, *, session_id=None, user_agent=None):
    """Record (or update) a device's subscription with the alerts it chose. Returns its id."""
    if not isinstance(subscription, dict):
        raise NotifyError("abonnement illisible")
    endpoint = check_endpoint(subscription.get("endpoint"))
    keys = subscription.get("keys") or {}
    encrypt(b"", keys.get("p256dh", ""), keys.get("auth", ""))  # refuses unusable keys now, not at the first alert
    chosen = json.dumps(check_events(events))
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, events_json, session_id, user_agent, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
        "auth = excluded.auth, events_json = excluded.events_json, session_id = excluded.session_id, "
        "user_agent = excluded.user_agent, failures = 0",
        (endpoint, keys["p256dh"], keys["auth"], chosen, session_id, (user_agent or "")[:300] or None,
         db.utc_now()))
    return conn.execute("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()["id"]


def unsubscribe(conn, endpoint):
    return conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (str(endpoint or ""),)).rowcount > 0


def subscription(conn, endpoint):
    row = conn.execute("SELECT id, events_json, created_at, last_ok_at FROM push_subscriptions WHERE endpoint = ?",
                       (str(endpoint or ""),)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "events": json.loads(row["events_json"]), "created_at": row["created_at"],
            "last_ok_at": row["last_ok_at"]}


# --- what an alert says -----------------------------------------------------------

def message(event, conversation_title=None, detail=None, *, with_title=True):
    """(title, body) of an alert. Without with_title, nothing about the conversation."""
    title = f"AI Hub : {EVENTS[event]}"
    if not with_title:
        return title, "Ouvrez AI Hub pour voir."
    parts = [f"« {conversation_title} »"] if conversation_title else []
    if detail:
        parts.append(str(detail)[:140])
    return title, " — ".join(parts) or "Ouvrez AI Hub pour voir."


def _default_post(url, content, headers, timeout):
    import httpx
    return httpx.post(url, content=content, headers=headers, timeout=timeout).status_code


class Notifier:
    """Delivers alerts in the background. `notify()` returns at once.

    `settings()` gives the settings in force (ntfy); `post(url, content,
    headers, timeout) -> status code` sends (httpx by default; tests give
    their own). `sync=True` delivers in the caller's thread (tests).
    """

    def __init__(self, db_path, settings, *, post=None, sync=False):
        self.db_path = db_path
        self.settings = settings
        self.post = post or _default_post
        self.sync = sync
        self._queue = queue.Queue()
        self._quota_levels = {}
        self._lock = threading.Lock()
        self._thread = None

    def notify(self, event, conversation_id=None, detail=None, *, test=False, user_id=None, session_id=None):
        """`test`: the « Essayer » button, shown even with the app in front, and
        only on the asking device (`session_id`). `user_id`: an alert about a
        person's own account (their quota): only their devices, not the owner's."""
        if event not in EVENTS:
            raise ValueError(event)
        if self.sync:
            self._deliver(event, conversation_id, detail, test, session_id, user_id)
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="aihub-notify", daemon=True)
                self._thread.start()
        self._queue.put((event, conversation_id, detail, test, session_id, user_id))

    def quota(self, provider_name, info, threshold_percent, user_id=None):
        """Alert once when a quota crosses the alert threshold, and once when it is reached.

        `user_id`: the quota of that person's own account (None: the computer's)."""
        used = info.utilization if isinstance(info.utilization, (int, float)) else 0
        level = 2 if info.status == "rejected" else 1 if used * 100 >= threshold_percent else 0
        with self._lock:
            before = self._quota_levels.get((provider_name, user_id), 0)
            self._quota_levels[(provider_name, user_id)] = level
        if level > before:
            what = "atteint" if level == 2 else f"à {round(used * 100)} %"
            self.notify("quota", None, f"{provider_name} : quota {what}", user_id=user_id)

    def flush(self, timeout=10):
        """Wait until every queued alert is delivered (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._queue.unfinished_tasks

    def _loop(self):
        while True:
            item = self._queue.get()
            try:
                self._deliver(*item)
            except Exception:  # an alert must never take the server down
                log.exception("notification non envoyée")
            finally:
                self._queue.task_done()

    def _deliver(self, event, conversation_id, detail, test=False, session_id=None, user_id=None):
        conn = db.connect(self.db_path)
        try:
            title_of = None
            if conversation_id is not None:
                row = conn.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
                title_of = row["title"] if row else None
            self._web_push(conn, event, conversation_id, title_of, detail, test, session_id, user_id)
            # ntfy is the owner's channel: never an alert about someone else's account.
            if (not test or session_id is None) and user_id is None:
                self._ntfy(event, title_of, detail, test)
        finally:
            conn.close()

    @staticmethod
    def _audience(conn, conversation_id):
        """Who besides the owner sees this alert: the people the conversation is shared with."""
        if conversation_id is None:
            return set()
        project = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        rows = conn.execute("SELECT user_id FROM shares WHERE conversation_id = ? OR "
                            "(project_id IS NOT NULL AND project_id = ?)",
                            (conversation_id, project["project_id"] if project else None)).fetchall()
        return {r["user_id"] for r in rows}

    def _web_push(self, conn, event, conversation_id, conversation_title, detail, test=False, session_id=None,
                  user_id=None):
        guests = self._audience(conn, conversation_id)
        rows = []
        for r in conn.execute("SELECT p.*, s.user_id, u.is_owner FROM push_subscriptions p "
                              "LEFT JOIN sessions s ON s.id = p.session_id LEFT JOIN users u ON u.id = s.user_id"):
            if test:
                if session_id is None or r["session_id"] == session_id:
                    rows.append(r)
                continue
            if event not in json.loads(r["events_json"]):
                continue
            if user_id is not None:
                if r["user_id"] == user_id:  # about that person's own account: theirs only
                    rows.append(r)
                continue
            # The owner's devices (and those of a server without accounts) get everything;
            # a guest's, only the alerts of what is shared with them.
            if r["user_id"] is None or r["is_owner"] or r["user_id"] in guests:
                rows.append(r)
        if not rows:
            return
        private, public = server_key(conn)
        title, body = message(event, conversation_title, detail)  # encrypted: only the device reads it
        payload = json.dumps({"title": title, "body": body, "event": event, "test": test,
                              "conversation_id": conversation_id, "tag": f"aihub-{event}-{conversation_id}"},
                             ensure_ascii=False).encode("utf-8")
        for row in rows:
            try:
                content = encrypt(payload, row["p256dh"], row["auth"])
                status = self.post(row["endpoint"], content, {
                    "Authorization": vapid_header(private, public, row["endpoint"]),
                    "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                    "TTL": str(_TTL.get(event, _DEFAULT_TTL)), "Urgency": "high" if event == "permission" else "normal",
                }, 15)
            except Exception as exc:  # network, refused keys: counted like a refusal
                log.warning("Web Push vers l'appareil %s : %s", row["id"], exc)
                status = None
            if status in (404, 410):
                # The browser dropped the subscription (notifications turned off, data cleared).
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
            elif status is not None and 200 <= status < 300:
                conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE id = ?",
                             (db.utc_now(), row["id"]))
            else:
                conn.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (row["id"],))
                conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND failures >= ?", (row["id"], MAX_FAILURES))

    def _ntfy(self, event, conversation_title, detail, test=False):
        s = self.settings()
        if s is None or not getattr(s, "ntfy_url", None) or not (test or event in s.ntfy_events):
            return
        title, body = message(event, conversation_title, detail if s.ntfy_detail == "titre" else None,
                              with_title=s.ntfy_detail == "titre")
        headers = {"Title": _header(title), "Priority": "high" if event == "permission" else "default", "Tags": "robot"}
        try:
            status = self.post(s.ntfy_url, body.encode("utf-8"), headers, 15)
        except Exception as exc:
            log.warning("ntfy : %s", exc)
            return
        if not (200 <= (status or 0) < 300):
            log.warning("ntfy a répondu %s", status)


def _header(text):
    """httpx sends headers in ASCII: ntfy decodes RFC 2047 for the rest (accents, French quotes)."""
    if text.isascii():
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
