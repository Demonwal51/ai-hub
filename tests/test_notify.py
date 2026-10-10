"""Phone alerts: Web Push (encryption, signature, choices) and ntfy."""

import base64
import json
import os
import time
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from aihub import config, db, notify, store
from aihub.providers.claude_code import QuotaInfo

from .helpers import TempDirMixin

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """A browser: its own key pair and auth secret, and the decryption of RFC 8291.

    Written with HKDF from the library, not with the module's own HMAC steps,
    so a mistake in one is not copied in the other."""

    def __init__(self, endpoint=ENDPOINT):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = self.private.public_key().public_bytes(serialization.Encoding.X962,
                                                             serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)
        self.subscription = {"endpoint": endpoint, "keys": {"p256dh": b64(self.public), "auth": b64(self.auth)}}

    def decrypt(self, body):
        salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        server_public = body[21:21 + idlen]
        record = body[21 + idlen:]
        assert rs == 4096 and idlen == 65
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_public)
        shared = self.private.exchange(ec.ECDH(), peer)
        ikm = HKDF(hashes.SHA256(), 32, self.auth, b"WebPush: info\x00" + self.public + server_public).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, record, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1].decode("utf-8"))


class Post:
    """The push services and ntfy: records each request, answers `status`."""

    def __init__(self, status=201):
        self.status = status
        self.sent = []

    def __call__(self, url, content, headers, timeout):
        self.sent.append({"url": url, "content": content, "headers": headers})
        return self.status(url) if callable(self.status) else self.status


class NotifyTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Boutique en ligne", agent_id=agent, workdir=str(self.tmp))
        self.settings = config.Settings()
        self.post = Post()
        self.notifier = notify.Notifier(self.db_path, lambda: self.settings, post=self.post, sync=True)

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]


class WebPushTest(NotifyTestCase):
    def test_the_device_reads_it_and_only_the_device(self):
        phone = Device()
        notify.subscribe(self.conn, phone.subscription, ["permission", "done"])
        self.notifier.notify("permission", self.cid, "l'IA demande « Bash »")
        sent = self.post.sent[0]
        self.assertEqual(sent["url"], ENDPOINT)
        data = phone.decrypt(sent["content"])
        self.assertEqual((data["title"], data["conversation_id"], data["event"]),
                         ("AI Hub : Une IA attend votre accord", self.cid, "permission"))
        self.assertEqual(data["body"], "« Boutique en ligne » — l'IA demande « Bash »")
        self.assertNotIn(b"Boutique", sent["content"])  # what the push service sees is ciphertext
        h = sent["headers"]
        self.assertEqual((h["Content-Encoding"], h["Urgency"], h["TTL"]), ("aes128gcm", "high", "1800"))
        with self.assertRaises(Exception):
            Device().decrypt(sent["content"])  # another device cannot read it

    def test_signed_with_the_servers_key(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])
        self.notifier.notify("done", self.cid)
        auth = self.post.sent[0]["headers"]["Authorization"]
        token, key = auth.removeprefix("vapid t=").split(", k=")
        _, public = notify.server_key(self.conn)
        self.assertEqual(key, public)
        head, claims, signature = token.split(".")
        claims = json.loads(unb64(claims))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        self.assertGreater(claims["exp"], time.time())
        self.assertLessEqual(claims["exp"], time.time() + 24 * 3600)  # the push services refuse more
        raw = unb64(signature)
        verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(public))
        verifier.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                        f"{head}.{token.split('.')[1]}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_the_key_is_made_once_and_kept(self):
        first = notify.server_key(self.conn)[1]
        other = db.connect(self.db_path)
        try:
            self.assertEqual(notify.server_key(other)[1], first)
        finally:
            other.close()

    def test_each_device_gets_only_what_it_chose(self):
        phone, pc = Device(), Device("https://wns2-par02p.notify.windows.com/w/?token=x")
        notify.subscribe(self.conn, phone.subscription, ["permission"])
        notify.subscribe(self.conn, pc.subscription, ["permission", "done"])
        self.notifier.notify("done", self.cid)
        self.assertEqual([s["url"] for s in self.post.sent], [pc.subscription["endpoint"]])
        self.notifier.notify("done", None, "essai", test=True)  # the test button reaches every device
        self.assertEqual(len(self.post.sent), 3)
        to_phone = next(s for s in self.post.sent[1:] if s["url"] == ENDPOINT)
        self.assertTrue(phone.decrypt(to_phone["content"])["test"])

    def test_only_the_browsers_push_services(self):
        for endpoint in ("http://fcm.googleapis.com/x", "https://evil.example/x", "https://fcm.googleapis.com.evil.example/x",
                         "https://notfcm.googleapis.com/x",
                         "https://127.0.0.1/x", "file:///c:/x", ""):
            with self.subTest(endpoint=endpoint), self.assertRaises(notify.NotifyError):
                notify.subscribe(self.conn, Device(endpoint).subscription, ["done"])
        for endpoint in ("https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/x"):
            notify.subscribe(self.conn, Device(endpoint).subscription, ["done"])
        self.assertEqual(self.count(), 2)

    def test_bad_keys_and_events_are_refused_at_once(self):
        bad = Device().subscription
        bad["keys"]["p256dh"] = b64(b"\x04" + b"x" * 10)
        with self.assertRaisesRegex(notify.NotifyError, "clés"):
            notify.subscribe(self.conn, bad, ["done"])
        with self.assertRaisesRegex(notify.NotifyError, "alertes inconnues"):
            notify.subscribe(self.conn, Device().subscription, ["tout"])
        self.assertEqual(self.count(), 0)

    def test_a_dropped_subscription_is_removed(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])
        self.post.status = 410
        self.notifier.notify("done", self.cid)
        self.assertEqual(self.count(), 0)

    def test_a_device_that_keeps_failing_is_removed(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])
        self.post.status = 500
        for _ in range(notify.MAX_FAILURES - 1):
            self.notifier.notify("done", self.cid)
        self.assertEqual(self.count(), 1)
        self.notifier.notify("done", self.cid)
        self.assertEqual(self.count(), 0)

    def test_a_success_resets_the_failures(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])
        self.post.status = 500
        self.notifier.notify("done", self.cid)
        self.post.status = 201
        self.notifier.notify("done", self.cid)
        row = self.conn.execute("SELECT failures, last_ok_at FROM push_subscriptions").fetchone()
        self.assertEqual(row["failures"], 0)
        self.assertIsNotNone(row["last_ok_at"])

    def test_a_network_error_never_reaches_the_caller(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])

        def broken(*args):
            raise OSError("réseau coupé")
        self.notifier.post = broken
        self.notifier.notify("done", self.cid)  # no exception
        self.assertEqual(self.conn.execute("SELECT failures FROM push_subscriptions").fetchone()[0], 1)

    def test_a_logged_out_device_receives_nothing_more(self):
        self.conn.execute("INSERT INTO sessions (token_hash, created_at, last_seen_at, expires_at) "
                          "VALUES ('h', 'x', 'x', '9999')")
        sid = self.conn.execute("SELECT id FROM sessions").fetchone()[0]
        notify.subscribe(self.conn, Device().subscription, ["done"], session_id=sid)
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        self.assertEqual(self.count(), 0)

    def test_resubscribing_updates_the_choice(self):
        phone = Device()
        notify.subscribe(self.conn, phone.subscription, ["done"])
        notify.subscribe(self.conn, phone.subscription, ["quota"])
        self.assertEqual(self.count(), 1)
        self.assertEqual(notify.subscription(self.conn, ENDPOINT)["events"], ["quota"])
        self.assertTrue(notify.unsubscribe(self.conn, ENDPOINT))
        self.assertIsNone(notify.subscription(self.conn, ENDPOINT))

    def test_background_delivery(self):
        notify.subscribe(self.conn, Device().subscription, ["done"])
        background = notify.Notifier(self.db_path, lambda: self.settings, post=self.post)
        background.notify("done", self.cid)
        self.assertTrue(background.flush())
        self.assertEqual(len(self.post.sent), 1)


class NtfyTest(NotifyTestCase):
    def use(self, **changes):
        self.settings = config.validate({"ntfy_url": "https://ntfy.sh/aihub-un-sujet-long", **changes})

    def test_minimal_says_nothing_of_the_conversation(self):
        self.use()
        self.notifier.notify("permission", self.cid, "l'IA demande « Bash »")
        sent = self.post.sent[0]
        self.assertEqual(sent["url"], "https://ntfy.sh/aihub-un-sujet-long")
        self.assertNotIn("Boutique", sent["content"].decode())
        self.assertNotIn("Bash", sent["content"].decode())
        self.assertEqual(sent["headers"]["Priority"], "high")
        self.assertEqual(sent["headers"]["Title"], "AI Hub : Une IA attend votre accord")
        self.notifier.notify("done", self.cid)
        title = self.post.sent[1]["headers"]["Title"]  # « Réponse prête »: not ASCII
        self.assertTrue(title.startswith("=?UTF-8?B?") and title.isascii())
        self.assertEqual(base64.b64decode(title[10:-2]).decode(), "AI Hub : Réponse prête")

    def test_with_the_title_when_chosen(self):
        self.use(ntfy_detail="titre")
        self.notifier.notify("done", self.cid)
        self.assertIn("« Boutique en ligne »", self.post.sent[0]["content"].decode())

    def test_only_the_chosen_alerts(self):
        self.use(ntfy_events=["quota"])
        self.notifier.notify("done", self.cid)
        self.assertEqual(self.post.sent, [])
        self.settings = config.validate({"ntfy_url": None})
        self.notifier.notify("quota", None, "x")
        self.assertEqual(self.post.sent, [])

    def test_address_checks(self):
        for url in ("http://ntfy.sh/sujet", "https://ntfy.sh/", "https://ntfy.sh/a/b", "ftp://x/y",
                    "https://u:p@ntfy.sh/sujet", "https://ntfy.sh/sujet?x=1"):
            with self.subTest(url=url), self.assertRaises(config.ConfigError):
                config.validate({"ntfy_url": url})
        self.assertEqual(config.validate({"ntfy_url": "http://127.0.0.1:8080/moi"}).ntfy_url,
                         "http://127.0.0.1:8080/moi")
        with self.assertRaises(config.ConfigError):
            config.validate({"ntfy_events": ["tout"]})
        with self.assertRaises(config.ConfigError):
            config.validate({"ntfy_detail": "complet"})
        self.assertEqual(list(config.NTFY_EVENTS), list(notify.EVENTS))  # the two lists stay the same


class QuotaAlertTest(NotifyTestCase):
    def test_once_at_the_threshold_once_at_the_limit(self):
        self.use_ntfy = self.settings = config.validate({"ntfy_url": "https://ntfy.sh/aihub-sujet",
                                                         "ntfy_detail": "titre"})
        for used, status in ((0.5, "allowed"), (0.85, "allowed_warning"), (0.9, "allowed_warning"),
                             (1.0, "rejected"), (1.0, "rejected")):
            self.notifier.quota("Codex", QuotaInfo(status=status, utilization=used), 80)
        bodies = [s["content"].decode() for s in self.post.sent]
        self.assertEqual(bodies, ["Codex : quota à 85 %", "Codex : quota atteint"])
        self.notifier.quota("Codex", QuotaInfo(status="allowed", utilization=0.1), 80)  # back after the reset
        self.notifier.quota("Codex", QuotaInfo(status="allowed", utilization=0.82), 80)
        self.assertEqual(len(self.post.sent), 3)


if __name__ == "__main__":
    unittest.main()
