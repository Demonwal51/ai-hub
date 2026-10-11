"""A device connected to an account by a QR code (a one-time link), and devices named."""

import unittest
from unittest import mock

from aihub import auth
from aihub.web import app as web_app

from .test_sharing import H, SharingTestCase


class DeviceLinkTest(SharingTestCase):
    def link(self, client, uid, label="Téléphone de Lou"):
        r = client.post(f"/api/users/{uid}/device-link", json={"label": label}, headers=H)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_the_qr_code_connects_the_phone_to_that_account_once(self):
        guest, uid = self.guest("Lou")
        made = self.link(self.owner, uid)
        self.assertIn("/?appareil=", made["link"])
        self.assertTrue(made["qr_svg"].startswith("<svg"))
        self.assertEqual(made["minutes"], auth.DEVICE_LINK_MINUTES)
        phone = self.client(("100.64.0.7", 50900))
        token = made["link"].split("appareil=")[1]
        r = phone.post("/api/devices/accept", json={"link": token}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(phone.get("/api/auth/session").json()["user"]["name"], "Lou")
        devices = phone.get("/api/sessions", headers=H).json()["sessions"]
        self.assertIn("Téléphone de Lou", [d["label"] for d in devices if d["current"]])
        # Once only, and never without the page's header (another site's page).
        again = self.client(("100.64.0.8", 50901))
        self.assertEqual(again.post("/api/devices/accept", json={"link": token}, headers=H).status_code, 401)
        other = self.link(self.owner, uid)["link"].split("appareil=")[1]
        self.assertEqual(again.post("/api/devices/accept", json={"link": other}).status_code, 403)
        # The owner sees which devices each person has.
        lou = next(u for u in self.owner.get("/api/users", headers=H).json()["users"] if u["id"] == uid)
        self.assertIn("Téléphone de Lou", [d["label"] for d in lou["devices"]])

    def test_expired_and_who_may_make_one(self):
        guest, uid = self.guest("Lou")
        _, other = self.guest_named("Sam")
        self.assertEqual(guest.post(f"/api/users/{other}/device-link", json={}, headers=H).status_code, 403)
        mine = self.link(guest, uid, "Tablette")  # for oneself: yes
        token = mine["link"].split("appareil=")[1]
        phone = self.client(("100.64.0.9", 50902))
        later = auth.datetime.now(auth.timezone.utc) + auth.timedelta(minutes=auth.DEVICE_LINK_MINUTES + 1)
        with mock.patch.object(auth.Auth, "now", lambda self: later):
            self.assertEqual(phone.post("/api/devices/accept", json={"link": token}, headers=H).status_code, 401)
        # A disabled person's link no longer works.
        fresh = self.link(self.owner, uid)["link"].split("appareil=")[1]
        self.owner.patch(f"/api/users/{uid}", json={"disabled": True}, headers=H)
        self.assertEqual(phone.post("/api/devices/accept", json={"link": fresh}, headers=H).status_code, 401)

    def guest_named(self, name):
        r = self.owner.post("/api/users", json={"name": name}, headers=H)
        return None, r.json()["user"]["id"]

    def test_each_one_names_their_own_devices(self):
        guest, uid = self.guest("Lou")
        mine = guest.get("/api/sessions", headers=H).json()["sessions"][0]
        r = guest.patch(f"/api/sessions/{mine['id']}", json={"label": "  PC   du salon  "}, headers=H)
        self.assertEqual(r.json()["label"], "PC du salon")
        self.assertEqual(guest.get("/api/sessions", headers=H).json()["sessions"][0]["label"], "PC du salon")
        owners = self.owner.get("/api/sessions", headers=H).json()["sessions"][0]
        self.assertEqual(guest.patch(f"/api/sessions/{owners['id']}", json={"label": "pris"}, headers=H).status_code, 404)

    def test_tailscale_serve_says_which_port_it_relays(self):
        status = {"TCP": {"443": {"HTTPS": True}}, "Web": {
            "pc.tailnet.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8770"}}},
            "pc.tailnet.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://localhost:9000/"}}}}}
        self.assertEqual(web_app.serve_relays(status), {8770: "pc.tailnet.ts.net", 9000: "pc.tailnet.ts.net:8443"})
        self.assertEqual(web_app.serve_relays({}), {})
        # The real lookup: only the port tailscale serve relays gets the address.
        import json
        import sys
        from pathlib import Path
        answer = mock.Mock(stdout=json.dumps(status))
        with mock.patch.object(web_app, "_TAILSCALE_CLI", (Path(sys.executable),)), \
                mock.patch.object(web_app.subprocess, "run", return_value=answer) as run, \
                mock.patch.dict(web_app._tailscale_cache, clear=True):
            self.assertEqual(web_app.tailscale_name(8770), "pc.tailnet.ts.net")
            self.assertIsNone(web_app.tailscale_name(8768))  # a trial server on another port: its own address
            self.assertEqual(run.call_count, 1)  # read once, then kept a while
            self.assertEqual(run.call_args[0][0][1:], ["serve", "status", "--json"])

    def test_a_link_made_on_this_computer_carries_the_tailscale_name(self):
        _, uid = self.guest("Lou")
        with mock.patch.object(web_app, "tailscale_name", return_value="pc.tailnet.ts.net"):
            here = self.link(self.owner, uid)  # the owner's client is 127.0.0.1: this computer
            relayed = self.owner.post(f"/api/users/{uid}/device-link", json={},
                                      headers={**H, "X-Forwarded-For": "100.64.0.2",
                                               "X-Forwarded-Proto": "https", "Host": "pc.tailnet.ts.net"}).json()
        self.assertTrue(here["link"].startswith("https://pc.tailnet.ts.net/?appareil="))
        self.assertTrue(relayed["link"].startswith("https://pc.tailnet.ts.net/?appareil="))
        with mock.patch.object(web_app, "tailscale_name", return_value=None):
            plain = self.link(self.owner, uid)
        self.assertTrue(plain["link"].startswith("http://testserver/?appareil="))


if __name__ == "__main__":
    unittest.main()
