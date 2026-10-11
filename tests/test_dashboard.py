"""The server's dashboard: it only reads, and only for the owner."""

import unittest

from aihub import auth
from aihub.web.app import create_app

from .helpers import FAKE_COMMAND, fake_env
from .test_auth import Clock
from .test_sharing import H, SharingTestCase


class DashboardTest(SharingTestCase):
    def test_what_the_owner_sees_and_no_one_else(self):
        guest, _ = self.guest("Lou")
        self.assertEqual(guest.get("/api/server", headers=H).status_code, 403)
        s = self.owner.get("/api/server", headers=H).json()
        self.assertGreater(s["disk"]["free"], 0)
        self.assertGreater(s["base"], 0)
        self.assertEqual((s["counts"]["people"], s["log"], s["log_file"]), (2, [], None))
        self.assertIn("claude", s["ais"])
        self.assertGreaterEqual(s["uptime_s"], 0)

    def test_the_end_of_the_log(self):
        journal = self.make_tempdir() / "serveur.log"
        journal.write_text("".join(f"ligne {n}\n" for n in range(80)), encoding="utf-8")
        app = create_app(self.db_path, command=FAKE_COMMAND, env=fake_env("text", self.make_tempdir() / "l.json"),
                         auth_policy=auth.Policy(), clock=Clock(), run_scheduler=False, journal_path=str(journal))
        from fastapi.testclient import TestClient
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            client.post("/api/auth/login", json={"password": self.password})
            log = client.get("/api/server", headers=H).json()["log"]
        self.assertEqual((len(log), log[0], log[-1]), (50, "ligne 30", "ligne 79"))


if __name__ == "__main__":
    unittest.main()
