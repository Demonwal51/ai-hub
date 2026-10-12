"""Linux integration: safe Exec quoting and no system-wide installation."""
import argparse
import os
import unittest
from unittest import mock

from aihub import linux, updater
from .helpers import TempDirMixin


class LinuxTest(TempDirMixin, unittest.TestCase):
    def test_paths_and_percent_are_quoted_for_desktop_exec(self):
        root = self.make_tempdir() / 'AI Hub 100%'
        root.mkdir()
        entry = linux.desktop_entry(root)
        self.assertIn('100%%', entry.split('Exec=')[1])
        self.assertIn('"-m" "aihub.linux" "launch"', entry)
        self.assertEqual(linux.desktop_quote('$x`"\\%'), '"\\$x\\`\\"\\\\%%"')

    def test_autostart_is_per_user_and_removable_without_touching_other_apps(self):
        root = self.make_tempdir()
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root)}):
            args = argparse.Namespace(action='activer', db=str(root / 'data/aihub.db'), options=['--', '--port', '8768'])
            out = []
            linux.startup(args, out.append)
            path = root / 'autostart/ai-hub.desktop'
            self.assertIn('"--port" "8768"', path.read_text(encoding='utf-8'))
            args.action = 'etat'
            linux.startup(args, out.append)
            self.assertIn('activé', out[-1])
            other = path.parent / 'other.desktop'
            other.write_text('other', encoding='utf-8')
            args.action = 'desactiver'
            linux.startup(args, out.append)
            self.assertFalse(path.exists())
            self.assertTrue(other.exists())

    def test_linux_detachment_never_passes_windows_flags(self):
        with mock.patch.object(updater.os, 'name', 'posix'):
            self.assertEqual(updater.detached_options(), {'start_new_session': True})


if __name__ == '__main__':
    unittest.main()
