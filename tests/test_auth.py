import secrets
import unittest
from datetime import datetime, timedelta, timezone

from aihub import auth, db
from tests.helpers import TempDirMixin

try:
    from fastapi.testclient import TestClient
    from aihub.web.app import create_app
    from tests.helpers import FAKE_COMMAND
except ImportError:  # web dependencies missing
    TestClient = None


class Clock:
    def __init__(self):
        self.at = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.at

    def advance(self, **delta):
        self.at += timedelta(**delta)


def new_password():
    return secrets.token_urlsafe(18)  # a test value made for this run


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        password = new_password()
        stored = auth.hash_password(password)
        self.assertTrue(stored.startswith("scrypt$32768$8$1$"))
        self.assertNotIn(password, stored)
        self.assertTrue(auth.verify_password(password, stored))
        self.assertFalse(auth.verify_password(password + "x", stored))
        self.assertNotEqual(stored, auth.hash_password(password))  # a new salt each time

    def test_garbage_never_verifies(self):
        for stored in ("", "md5$abc", "scrypt$x$8$1$AA==$AA==", "scrypt$1$2"):
            with self.subTest(stored=stored):
                self.assertFalse(auth.verify_password("n'importe quoi", stored))

    def test_too_short(self):
        with self.assertRaisesRegex(auth.AuthError, "au moins 12"):
            auth.hash_password("court")


class StoreTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(str(self.make_tempdir() / "hub.db"))
        self.addCleanup(self.conn.close)
        self.clock = Clock()
        # Short sessions here, to see them end; the default is a year (test_default_is_once_per_device).
        self.auth = auth.Auth(auth.Policy(idle_days=7, max_days=30), now=self.clock)
        self.password = new_password()
        auth.set_password(self.conn, self.password)

    def fail(self, client="1.2.3.4", times=1):
        for _ in range(times):
            with self.assertRaises(auth.AuthError):
                self.auth.login(self.conn, "mauvais mot de passe", client)


class LockoutTests(StoreTestCase):
    def test_wrong_password_counts_down(self):
        with self.assertRaisesRegex(auth.AuthError, r"mot de passe incorrect \(4 essai"):
            self.auth.login(self.conn, "faux", "1.2.3.4")

    def test_locked_after_five_even_with_the_right_password(self):
        self.fail(times=4)
        with self.assertRaisesRegex(auth.Locked, "réessayez dans 15 min") as caught:
            self.auth.login(self.conn, "faux", "1.2.3.4")
        self.assertEqual(caught.exception.retry_after_s, 901)
        # The right password does not open a locked client: no free guesses.
        with self.assertRaises(auth.Locked):
            self.auth.login(self.conn, self.password, "1.2.3.4")
        # Another address is not locked by this one.
        self.assertTrue(self.auth.login(self.conn, self.password, "5.6.7.8"))

    def test_lock_ends_then_doubles(self):
        self.fail(times=5)
        self.clock.advance(minutes=15, seconds=1)
        self.fail(times=5)
        with self.assertRaisesRegex(auth.Locked, "réessayez dans 30 min"):
            self.auth.login(self.conn, self.password, "1.2.3.4")
        self.clock.advance(minutes=30, seconds=1)
        self.assertTrue(self.auth.login(self.conn, self.password, "1.2.3.4"))
        # A success clears the slate: the next lockout is short again.
        self.fail(times=5)
        with self.assertRaisesRegex(auth.Locked, "15 min"):
            self.auth.login(self.conn, self.password, "1.2.3.4")

    def test_old_failures_are_forgotten(self):
        self.fail(times=4)
        self.clock.advance(minutes=16)
        self.fail(times=4)  # 4 in the window, not 8: no lockout
        self.assertTrue(self.auth.login(self.conn, self.password, "1.2.3.4"))

    def test_an_old_lockout_does_not_lengthen_the_next(self):
        self.fail(times=5)
        self.clock.advance(days=2)
        self.fail(times=5)
        with self.assertRaisesRegex(auth.Locked, "15 min"):
            self.auth.login(self.conn, self.password, "1.2.3.4")

    def test_many_addresses_lock_everyone(self):
        for i in range(20):  # 4 per address: none is locked on its own
            self.fail(client=f"10.0.0.{i // 4}")
        with self.assertRaises(auth.Locked):
            self.auth.login(self.conn, self.password, "10.9.9.9")

    def test_lockouts_survive_a_restart(self):
        self.fail(times=5)
        restarted = auth.Auth(auth.Policy(), now=self.clock)
        with self.assertRaises(auth.Locked):
            restarted.login(self.conn, self.password, "1.2.3.4")


class SessionTests(StoreTestCase):
    def test_token_is_only_stored_hashed(self):
        token = self.auth.login(self.conn, self.password, "1.2.3.4", "Firefox")
        rows = self.conn.execute("SELECT token_hash, client, user_agent FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["token_hash"], token)
        self.assertEqual((rows[0]["client"], rows[0]["user_agent"]), ("1.2.3.4", "Firefox"))
        self.assertTrue(self.auth.check(self.conn, token))
        self.assertFalse(self.auth.check(self.conn, rows[0]["token_hash"]))  # the hash opens nothing
        self.assertFalse(self.auth.check(self.conn, None))
        self.assertFalse(self.auth.check(self.conn, "inventé"))

    def test_idle_session_expires(self):
        token = self.auth.login(self.conn, self.password, "c")
        self.clock.advance(days=6)
        self.assertTrue(self.auth.check(self.conn, token))  # used: prolonged by 7 days
        self.clock.advance(days=6)
        self.assertTrue(self.auth.check(self.conn, token))
        self.clock.advance(days=7, seconds=1)
        self.assertFalse(self.auth.check(self.conn, token))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_default_is_once_per_device(self):
        long = auth.Auth(auth.Policy(), now=self.clock)
        token = long.login(self.conn, self.password, "téléphone")
        self.clock.advance(days=300)
        self.assertTrue(long.check(self.conn, token))  # still open after ten months unused
        self.clock.advance(days=366)
        self.assertFalse(long.check(self.conn, token))  # a year unused: closed

    def test_session_ends_after_max_days_even_if_used(self):
        token = self.auth.login(self.conn, self.password, "c")
        for _ in range(29):
            self.clock.advance(days=1)
            self.assertTrue(self.auth.check(self.conn, token))
        self.clock.advance(days=1, seconds=1)
        self.assertFalse(self.auth.check(self.conn, token))

    def test_logout_and_password_change_close_sessions(self):
        first = self.auth.login(self.conn, self.password, "a")
        second = self.auth.login(self.conn, self.password, "b")
        self.auth.logout(self.conn, first)
        self.assertFalse(self.auth.check(self.conn, first))
        self.assertTrue(self.auth.check(self.conn, second))
        auth.set_password(self.conn, new_password())
        self.assertFalse(self.auth.check(self.conn, second))

    def test_password_change_clears_lockouts(self):
        self.fail(times=5)
        password = new_password()
        auth.set_password(self.conn, password)
        self.assertTrue(self.auth.login(self.conn, password, "1.2.3.4"))

    def test_no_password_set(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertFalse(auth.password_set(conn))
        with self.assertRaisesRegex(auth.AuthError, "aucun mot de passe"):
            self.auth.login(conn, "x", "c")


@unittest.skipIf(TestClient is None, "web dependencies missing")
class WebAuthTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.db_path = str(self.make_tempdir() / "hub.db")
        self.password = new_password()
        conn = db.connect(self.db_path)
        auth.set_password(conn, self.password)
        conn.close()
        self.clock = Clock()
        self.app = create_app(self.db_path, command=FAKE_COMMAND, auth_policy=auth.Policy(), clock=self.clock)
        self.client = self.make_client()

    def make_client(self, address=("127.0.0.1", 50000)):
        client = TestClient(self.app, client=address)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def login(self, client=None, password=None, **headers):
        return (client or self.client).post("/api/auth/login", json={"password": password or self.password},
                                            headers=headers)

    def test_a_logged_out_device_gets_no_more_alerts(self):
        from tests.test_notify import Device
        phone = Device()
        self.assertEqual(self.login().status_code, 200)
        headers = {"X-AIHub": "1"}
        r = self.client.post("/api/push/subscription", headers=headers,
                             json={"subscription": phone.subscription, "events": ["done"]})
        self.assertEqual(r.status_code, 200, r.text)
        conn = db.connect(self.db_path)
        try:
            self.assertIsNotNone(conn.execute("SELECT session_id FROM push_subscriptions").fetchone()[0])
            self.client.post("/api/auth/logout", headers=headers)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)
        finally:
            conn.close()

    def test_everything_needs_a_session(self):
        for method, path in (("GET", "/api/conversations"), ("GET", "/api/options"), ("GET", "/api/quota"),
                             ("POST", "/api/push/subscription"), ("GET", "/api/push/key"),
                             ("GET", "/api/conversations/1/events"), ("POST", "/api/conversations"),
                             ("DELETE", "/api/conversations/1"), ("GET", "/api/agents")):
            with self.subTest(path=path):
                r = self.client.request(method, path, headers={"X-AIHub": "1"})
                self.assertEqual(r.status_code, 401)
                self.assertEqual(r.json()["code"], "auth_required")

    def test_the_page_and_its_install_files_are_public(self):
        for path in ("/", "/static/app.js", "/static/icon-192.png", "/manifest.webmanifest", "/sw.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/session").json(),
                         {"required": True, "logged_in": False, "user": None})
        # Checked again before reuse: an update reaches the browser and the phone at once.
        for path in ("/", "/static/app.js", "/static/style.css", "/sw.js", "/manifest.webmanifest"):
            self.assertEqual(self.client.get(path).headers["cache-control"], "no-cache", path)
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.headers["content-type"], "application/manifest+json")
        self.assertIn({"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                       "purpose": "maskable"}, manifest.json()["icons"])

    def test_login_opens_the_api(self):
        r = self.login()
        self.assertEqual(r.status_code, 200)
        cookie = r.headers["set-cookie"]
        self.assertIn(f"{auth.COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Secure", cookie)  # plain http on this computer
        self.assertEqual(self.client.get("/api/conversations").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/session").json(),
                         {"required": True, "logged_in": True, "user": {"name": "Propriétaire", "is_owner": True}})

    def test_cookie_is_secure_behind_https(self):
        r = self.login(**{"X-Forwarded-Proto": "https", "X-Forwarded-For": "100.64.0.2"})
        self.assertIn("Secure", r.headers["set-cookie"])

    def test_wrong_password(self):
        r = self.login(password="pas le bon mot de passe")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "bad_password"))
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)

    def test_writes_need_the_x_aihub_header(self):
        self.login()
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": self.db_path})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf"))
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": self.db_path},
                             headers={"X-AIHub": "1"})
        self.assertEqual(r.status_code, 400)  # through: refused for its folder, not for the header

    def test_lockout_answers_429_with_retry_after(self):
        for _ in range(4):
            self.login(password="faux faux faux")
        r = self.login(password="faux faux faux")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "locked"))
        self.assertEqual(r.headers["retry-after"], "901")
        self.assertEqual(self.login().status_code, 429)
        self.clock.advance(minutes=16)
        self.assertEqual(self.login().status_code, 200)

    def test_forwarded_clients_are_counted_apart(self):
        # Behind `tailscale serve` every request comes from 127.0.0.1: the
        # forwarded address tells the phone from another device.
        for _ in range(5):
            self.login(password="faux faux faux", **{"X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(self.login(**{"X-Forwarded-For": "100.64.0.2"}).status_code, 429)
        self.assertEqual(self.login(**{"X-Forwarded-For": "100.64.0.3"}).status_code, 200)

    def test_forwarded_header_is_ignored_from_another_machine(self):
        remote = self.make_client(("100.64.0.9", 40000))
        for i in range(5):  # a forged header must not dodge the count
            self.login(remote, password="faux faux faux", **{"X-Forwarded-For": f"10.0.0.{i}"})
        self.assertEqual(self.login(remote).status_code, 429)

    def test_logout(self):
        token = self.login().cookies[auth.COOKIE]
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        # The session is closed on the server, not only forgotten by this
        # browser: a copy of the cookie taken before is worth nothing.
        replay = self.make_client()
        replay.cookies.set(auth.COOKIE, token)
        self.assertEqual(replay.get("/api/conversations").status_code, 401)

    def test_expired_session_is_refused(self):
        self.login()
        self.clock.advance(days=300)
        self.assertEqual(self.client.get("/api/conversations", headers={"X-AIHub": "1"}).status_code, 200)
        self.clock.advance(days=366)  # a year without use
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)


@unittest.skipIf(TestClient is None, "web dependencies missing")
class LocalOnlyTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.app = create_app(str(self.make_tempdir() / "hub.db"), command=FAKE_COMMAND, local_only=True)

    def client(self, address):
        client = TestClient(self.app, client=address)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_this_computer_needs_no_password(self):
        local = self.client(("127.0.0.1", 1))
        self.assertEqual(local.get("/api/conversations").status_code, 200)
        self.assertEqual(local.get("/api/auth/session").json(), {"required": False, "logged_in": True, "user": None})

    def test_other_machines_and_proxies_are_refused(self):
        self.assertEqual(self.client(("100.64.0.2", 1)).get("/api/conversations").status_code, 403)
        local = self.client(("127.0.0.1", 1))
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login"):
            with self.subTest(header=header):
                r = local.get("/", headers={header: "100.64.0.2"})
                self.assertEqual((r.status_code, r.json()["code"]), (403, "local_only"))


if __name__ == "__main__":
    unittest.main()
