"""1.1: « Essais à faire », what was never tried for real and how it went (the owner's)."""

import unittest

from aihub import trials

try:
    from tests.test_roles import RolesTestCase
    from tests.test_sharing import H
except unittest.SkipTest:  # web dependencies missing
    RolesTestCase = None


class ListTests(unittest.TestCase):
    def test_every_trial_has_a_title_and_steps(self):
        keys = [key for key, _, _ in trials.TRIALS]
        self.assertEqual(len(keys), len(set(keys)))  # a key is unique: it is the row's key
        for key, title, steps in trials.TRIALS:
            with self.subTest(key=key):
                self.assertTrue(title and steps and all(steps))


if RolesTestCase is not None:
    class TrialsWebTests(RolesTestCase):
        def test_said_and_said_again(self):
            listed = self.owner.get("/api/trials", headers=H).json()["trials"]
            self.assertEqual(len(listed), len(trials.TRIALS))
            self.assertEqual({t["status"] for t in listed}, {"attente"})
            r = self.owner.put("/api/trials/voix", json={"status": "ok", "note": "lue en français"}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            voice = next(t for t in r.json()["trials"] if t["key"] == "voix")
            self.assertEqual((voice["status"], voice["note"]), ("ok", "lue en français"))
            self.assertIsNotNone(voice["updated_at"])
            r = self.owner.put("/api/trials/voix", json={"status": "probleme", "note": "coupe au milieu"}, headers=H)
            voice = next(t for t in r.json()["trials"] if t["key"] == "voix")
            self.assertEqual((voice["status"], voice["note"]), ("probleme", "coupe au milieu"))
            again = next(t for t in self.owner.get("/api/trials", headers=H).json()["trials"] if t["key"] == "voix")
            self.assertEqual(again["status"], "probleme")  # kept in the base

        def test_refusals(self):
            for key, body in (("inconnu", {"status": "ok"}), ("voix", {"status": "fini"}),
                              ("voix", {"status": "ok", "note": "x" * (trials.MAX_NOTE + 1)})):
                with self.subTest(key=key, body=str(body)[:30]):
                    self.assertEqual(self.owner.put(f"/api/trials/{key}", json=body, headers=H).status_code, 400)
            ada, _ = self.person("Ada", role="admin")
            self.assertEqual(ada.get("/api/trials", headers=H).status_code, 403)
            self.assertEqual(ada.put("/api/trials/voix", json={"status": "ok"}, headers=H).status_code, 403)


if __name__ == "__main__":
    unittest.main()
