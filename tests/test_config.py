import json
import secrets
import unittest
from pathlib import Path
from unittest import mock

from aihub import auth, cli, config, db
from tests.helpers import FAKE_COMMAND, TempDirMixin

try:
    from fastapi.testclient import TestClient
    from aihub.web.app import create_app
except ImportError:  # web dependencies missing
    TestClient = None


class ValidateTests(TempDirMixin, unittest.TestCase):
    def test_defaults(self):
        s = config.validate({})
        self.assertEqual((s.models, s.default_model, s.default_conductor, s.quota_warning, s.backup_keep),
                         (list(config.MODELS), "default", "opus", 80, 14))
        self.assertTrue(s.backup_enabled and s.keep_awake)
        self.assertEqual(s.allowed_roots, [])

    def test_good_values_are_normalised(self):
        folder = self.make_tempdir()
        s = config.validate({"allowed_roots": [str(folder), str(folder)], "obsidian_vault": "",
                             "models": ["opus", "haiku"], "default_model": "haiku", "default_conductor": "opus",
                             "backup_dir": str(folder / "pas-encore")})
        self.assertEqual(s.allowed_roots, [str(folder.resolve())])  # once
        self.assertIsNone(s.obsidian_vault)
        self.assertEqual(s.models, ["haiku", "opus"])  # always the same order
        self.assertEqual(s.backup_dir, str((folder / "pas-encore").resolve()))  # may not exist yet

    def test_refusals(self):
        folder = self.make_tempdir()
        for change, fragment in (
                ({"allowed_roots": ["relatif"]}, "chemin complet"),
                ({"allowed_roots": [str(folder / "absent")]}, "introuvable"),
                ({"allowed_roots": "D:\\projets"}, "une liste"),
                ({"obsidian_vault": str(folder / "absent")}, "introuvable"),
                ({"models": []}, "au moins un"),
                ({"models": ["gpt"]}, "modèle inconnu"),
                ({"models": ["haiku"]}, "« default » n'est pas parmi"),
                ({"default_conductor": "gpt"}, "n'est pas parmi"),
                ({"quota_warning": 120}, "entre 50 et 99"),
                ({"quota_warning": "80"}, "un nombre"),
                ({"quota_warning": True}, "un nombre"),
                ({"backup_keep": 2.5}, "entier"),
                ({"backup_keep": 0}, "entre 1 et 365"),
                ({"idle_days": 0}, "entre 1 et 3650"),
                ({"max_failures": 1}, "entre 3 et 20"),
                ({"lock_minutes": 5000}, "entre 1 et 1440"),
                ({"keep_awake": "oui"}, "oui ou non"),
                ({"backup_enabled": 1}, "oui ou non"),
                ({"mot_de_passe": "x"}, "réglage inconnu")):
            with self.subTest(change=change), self.assertRaisesRegex(config.ConfigError, fragment):
                config.validate(change)
        with self.assertRaisesRegex(config.ConfigError, "illisibles"):
            config.validate(["pas", "un", "objet"])

    def test_change_on_a_base(self):
        base = config.validate({"quota_warning": 70})
        self.assertEqual(config.validate({"backup_keep": 3}, base=base).quota_warning, 70)


class FileTests(TempDirMixin, unittest.TestCase):
    def test_missing_file_means_defaults(self):
        self.assertEqual(config.load(self.make_tempdir() / "config.json"), config.Settings())

    def test_save_and_load(self):
        path = self.make_tempdir() / "sous" / "config.json"
        config.save(path, config.validate({"quota_warning": 65}))
        self.assertEqual(config.load(path).quota_warning, 65)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        self.assertTrue(path.read_bytes().startswith(b"{"))  # no BOM

    def test_old_file_keeps_working(self):
        path = self.make_tempdir() / "config.json"
        path.write_text(json.dumps({"quota_warning": 90}), encoding="utf-8")  # the other names missing
        s = config.load(path)
        self.assertEqual((s.quota_warning, s.backup_keep), (90, 14))

    def test_bad_file_is_refused_with_its_name(self):
        path = self.make_tempdir() / "config.json"
        path.write_text("{ pas du json", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "illisible"):
            config.load(path)
        path.write_text(json.dumps({"quota_warning": 5}), encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, "config.json : seuil"):
            config.load(path)


@unittest.skipIf(TestClient is None, "web dependencies missing")
class WebConfigTestCase(TempDirMixin, unittest.TestCase):
    password = None

    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.config_path = self.tmp / "config.json"
        self.vault = self.make_tempdir()
        policy = None
        if self.password:
            conn = db.connect(self.db_path)
            auth.set_password(conn, self.password)
            conn.close()
            policy = auth.Policy()
        kwargs = {"settings": config.load(self.config_path), **self.extra()}
        self.app = create_app(self.db_path, command=FAKE_COMMAND, auth_policy=policy,
                              config_path=self.config_path, **kwargs)
        self.client = TestClient(self.app, client=("127.0.0.1", 1), headers={"X-AIHub": "1"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        if self.password:
            self.client.post("/api/auth/login", json={"password": self.password})

    def extra(self):
        return {}

    def change(self, changes, password=None):
        return self.client.patch("/api/config", json={"changes": changes, "password": password or self.password or ""})


class WebConfigTests(WebConfigTestCase):
    def test_read(self):
        data = self.client.get("/api/config").json()
        self.assertEqual(data["settings"]["quota_warning"], 80)
        self.assertTrue(data["editable"])
        self.assertFalse(data["password_needed"])
        self.assertIn("quota_warning", data["labels"])
        self.assertEqual(data["file"], str(self.config_path.resolve()))

    def test_change_is_saved_and_applied_at_once(self):
        r = self.change({"obsidian_vault": str(self.vault), "models": ["haiku", "opus"],
                         "default_model": "haiku", "quota_warning": 70})
        self.assertEqual(r.status_code, 200, r.text)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["quota_warning"], saved["models"]), (70, ["haiku", "opus"]))
        options = self.client.get("/api/options").json()
        self.assertEqual((options["models"], options["default_model"], options["quota_warning"]),
                         (["haiku", "opus"], "haiku", 70))
        self.assertTrue(options["obsidian"]["enabled"])
        self.assertEqual(self.app.state.jobs.obsidian_vault, str(self.vault.resolve()))
        # A model no longer offered cannot be chosen any more.
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": str(self.tmp), "model": "sonnet"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("non proposé", r.json()["detail"])

    def test_allowed_folders_apply_at_once(self):
        inside = self.make_tempdir()
        outside = self.make_tempdir()
        self.change({"allowed_roots": [str(inside)]})
        r = self.client.post("/api/conversations", json={"title": "t", "workdir": str(outside)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("non autorisé", r.json()["detail"])
        self.assertEqual(self.client.post("/api/conversations", json={"title": "t", "workdir": str(inside)}).status_code, 201)

    def test_quota_threshold_is_used_when_reading_the_quota(self):
        import os
        with mock.patch.dict(os.environ, {"FAKE_USAGE": "mid", "FAKE_CLAUDE_LOGGED_IN": "1"}):  # 72 % used
            self.assertEqual(self.client.get("/api/quota?refresh=true").json()["claude_code"]["status"], "allowed")
            self.change({"quota_warning": 70})
            import aihub.web.app as web
            with mock.patch.object(web, "QUOTA_REFRESH_S", 0):  # read again at once
                status = self.client.get("/api/quota?refresh=true").json()["claude_code"]["status"]
        self.assertEqual(status, "allowed_warning")

    def test_refused_change_leaves_everything(self):
        self.change({"quota_warning": 70})
        before = self.config_path.read_text(encoding="utf-8")
        r = self.change({"quota_warning": 75, "models": ["gpt"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("modèle inconnu", r.json()["detail"])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.client.get("/api/options").json()["quota_warning"], 70)

    def test_backups_follow_the_settings(self):
        self.assertIsNotNone(self.app.state.backup)  # on by default
        first = self.app.state.backup
        self.assertTrue(first._thread.is_alive())
        self.change({"backup_enabled": False})
        self.assertIsNone(self.app.state.backup)
        self.assertFalse(first._thread.is_alive())  # stopped, not left behind
        folder = self.tmp / "ailleurs"
        self.change({"backup_enabled": True, "backup_dir": str(folder), "backup_keep": 3})
        self.assertEqual((self.app.state.backup.folder, self.app.state.backup.keep), (str(folder.resolve()), 3))
        self.assertTrue(self.app.state.backup._thread.is_alive())

    def test_keep_awake_follows_the_settings(self):
        self.assertIsNotNone(self.app.state.awake)
        old = self.app.state.awake
        self.change({"keep_awake": False})
        self.assertIsNone(self.app.state.awake)
        self.assertFalse(old._thread.is_alive())

    def test_deleting_still_purges_backups_turned_off_since(self):
        cid = self.client.post("/api/conversations", json={"title": "Secret-cfg-77", "workdir": str(self.tmp)}
                               ).json()["conversation"]["id"]
        from datetime import date
        from aihub import backup
        backup.backup_now(self.db_path, self.tmp / "sauvegardes", today=date(2020, 1, 1))
        self.change({"backup_enabled": False})
        self.client.delete(f"/api/conversations/{cid}")
        self.assertNotIn(b"Secret-cfg-77", (self.tmp / "sauvegardes" / "aihub-2020-01-01.db").read_bytes())


class FixedSettingsTests(TempDirMixin, unittest.TestCase):
    @unittest.skipIf(TestClient is None, "web dependencies missing")
    def test_without_a_file_nothing_can_change(self):
        app = create_app(str(self.make_tempdir() / "hub.db"), command=FAKE_COMMAND)
        with TestClient(app) as client:
            self.assertFalse(client.get("/api/options").json()["settings_editable"])
            r = client.patch("/api/config", json={"changes": {"quota_warning": 70}})
            self.assertEqual(r.status_code, 400)
            self.assertIn("fixés au lancement", r.json()["detail"])


class PasswordConfigTests(WebConfigTestCase):
    password = secrets.token_urlsafe(18)  # a test value made for this run

    def test_a_change_needs_the_password(self):
        self.assertTrue(self.client.get("/api/config").json()["password_needed"])
        r = self.change({"quota_warning": 70}, password="pas le bon mot de passe")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "bad_password"))
        self.assertFalse(self.config_path.exists())  # nothing written
        self.assertEqual(self.change({"quota_warning": 70}).status_code, 200)
        self.assertEqual(config.load(self.config_path).quota_warning, 70)

    def test_wrong_passwords_count_for_the_lockout(self):
        for _ in range(5):
            self.change({"quota_warning": 70}, password="faux faux faux")
        r = self.change({"quota_warning": 70})  # the right one, but locked
        self.assertEqual(r.status_code, 429)

    def test_session_rules_apply_at_once(self):
        self.change({"max_failures": 3, "lock_minutes": 2, "idle_days": 3})
        policy = self.app.state.settings()
        self.assertEqual((policy.max_failures, policy.lock_minutes, policy.idle_days), (3, 2, 3))
        for _ in range(3):
            self.client.post("/api/auth/login", json={"password": "faux faux faux"})
        self.assertEqual(self.client.post("/api/auth/login", json={"password": self.password}).status_code, 429)


class OverrideTests(WebConfigTestCase):
    def extra(self):
        settings = config.validate({"obsidian_vault": str(self.vault)}, base=config.load(self.config_path))
        return {"settings": settings, "overridden": ["obsidian_vault"]}

    def test_launch_options_are_not_written_to_the_file(self):
        data = self.client.get("/api/config").json()
        self.assertEqual(data["overridden"], ["obsidian_vault"])
        self.change({"quota_warning": 70})
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsNone(saved["obsidian_vault"])  # the option stays an option
        self.assertEqual(saved["quota_warning"], 70)
        self.assertEqual(self.app.state.settings().obsidian_vault, str(self.vault.resolve()))  # still in force


class CliConfigTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        try:
            import uvicorn  # noqa: F401
        except ImportError:
            self.skipTest("web dependencies missing")
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.output = []

    def serve(self, *argv):
        with mock.patch("uvicorn.run") as run:
            code = cli.main(["--db", self.db_path, "serve", "--sans-mot-de-passe", *argv],
                            out=lambda text="", end="\n": self.output.append(text + end), command=FAKE_COMMAND)
        return code, run

    def test_the_file_next_to_the_base_is_read(self):
        config.save(self.tmp / "config.json", config.validate({"quota_warning": 66, "backup_keep": 4}))
        code, run = self.serve()
        self.assertEqual(code, 0)
        app = run.call_args.args[0]
        self.assertEqual(app.state.settings().quota_warning, 66)
        self.assertEqual(app.state.backup.keep, 4)
        self.assertIn(f"Réglages : {self.tmp.resolve() / 'config.json'}", "".join(self.output))

    def test_options_override_for_the_run(self):
        vault = self.make_tempdir()
        code, run = self.serve("--obsidian", str(vault), "--garder", "2", "--laisser-dormir")
        app = run.call_args.args[0]
        s = app.state.settings()
        self.assertEqual((s.obsidian_vault, s.backup_keep, s.keep_awake), (str(vault.resolve()), 2, False))
        self.assertIn("fixés au lancement : backup_keep, keep_awake, obsidian_vault", "".join(self.output))
        self.assertFalse((self.tmp / "config.json").exists())  # nothing written by a launch

    def test_a_bad_option_or_file_stops_the_launch(self):
        code, run = self.serve("--obsidian", str(self.tmp / "absent"))
        self.assertEqual(code, 1)
        self.assertIn("Réglage refusé", "".join(self.output))
        run.assert_not_called()
        (self.tmp / "config.json").write_text("{ cassé", encoding="utf-8")
        code, run = self.serve()
        self.assertEqual(code, 1)
        run.assert_not_called()


class QuotaThresholdTests(unittest.TestCase):
    def test_threshold_moves_the_warning(self):
        from aihub.providers import claude_code
        reply = {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 72}}}
        self.assertEqual(claude_code.parse_usage(reply).status, "allowed")  # default: 80 %
        self.assertEqual(claude_code.parse_usage(reply, 0.70).status, "allowed_warning")


if __name__ == "__main__":
    unittest.main()
