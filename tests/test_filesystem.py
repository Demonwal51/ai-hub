import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from aihub import filesystem, isolation, db, store, obsidian
from .helpers import TempDirMixin
from .test_sharing import H, SharingTestCase


class FilesystemTest(TempDirMixin, unittest.TestCase):
    def test_new_vault_is_empty_and_existing_folder_is_not_changed(self):
        root = self.make_tempdir()
        result = obsidian.create_vault(str(root / 'Notes neuves'), roots=[root])
        self.assertTrue(result['created'])
        self.assertEqual(list((root / 'Notes neuves').iterdir()), [root / 'Notes neuves' / '.obsidian'])
        note = root / 'Notes neuves' / 'Note.md'
        note.write_text('Ne pas modifier', encoding='utf-8')
        with self.assertRaises(store.StoreError):
            obsidian.create_vault(str(root / 'Notes neuves'), roots=[root])
        self.assertEqual(note.read_text(encoding='utf-8'), 'Ne pas modifier')
        with self.assertRaises(store.StoreError):
            obsidian.create_vault(str(root.anchor))

    def test_create_parents_empty_file_and_existing_directory(self):
        root = self.make_tempdir()
        folder = root / 'project' / 'src'
        self.assertTrue(filesystem.create(str(folder), 'directory', roots=[root])['created'])
        self.assertFalse(filesystem.create(str(folder), 'directory', roots=[root])['created'])
        created = filesystem.create('nested/result.txt', 'file', base=folder, roots=[root])
        self.assertTrue(created['created'])
        self.assertEqual(Path(created['path']).read_bytes(), b'')

    def test_existing_file_is_never_truncated_or_replaced_by_directory(self):
        root = self.make_tempdir()
        original = root / 'existing.txt'
        original.write_bytes(b'preserve this content')
        for kind in ('file', 'directory'):
            with self.assertRaises(store.StoreError):
                filesystem.create(str(original), kind, roots=[root])
            self.assertEqual(original.read_bytes(), b'preserve this content')

    def test_roots_traversal_absolute_child_and_reserved_names_are_refused(self):
        root, outside = self.make_tempdir(), self.make_tempdir()
        for path, base in ((str(outside/'blocked'), None), ('../escape', root),
                           (str(outside/'blocked'), root), ('CON.txt', root),
                           ('name:stream', root), ('trail.', root)):
            with self.subTest(path=path), self.assertRaises(store.StoreError):
                filesystem.create(path, 'file', base=base, roots=[root])
        self.assertFalse((outside/'blocked').exists())

    def test_missing_parents_are_not_created_without_choice(self):
        root = self.make_tempdir()
        with self.assertRaises(store.StoreError):
            filesystem.create(str(root/'missing'/'file.txt'), 'file', roots=[root], parents=False)
        self.assertFalse((root/'missing').exists())

    def test_symlink_or_windows_junction_cannot_redirect_creation(self):
        root, outside = self.make_tempdir(), self.make_tempdir()
        link = root / 'link'
        if os.name == 'nt':
            result = subprocess.run(['cmd','/d','/c','mklink','/J',str(link),str(outside)],
                                    capture_output=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(store.StoreError):
            filesystem.create(str(link/'escape.txt'), 'file', roots=[root])
        self.assertFalse((outside/'escape.txt').exists())


class FilesystemWebTest(SharingTestCase):
    def test_owner_creates_vault_without_changing_active_configuration(self):
        before = self.owner.get('/api/config').json()['settings']['obsidian_vault']
        folder = self.workdir / 'Nouveau coffre'
        result = self.owner.post('/api/obsidian/vaults', json={'path':str(folder),'password':self.password}, headers=H)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertTrue((folder / '.obsidian').is_dir())
        self.assertEqual(self.owner.get('/api/config').json()['settings']['obsidian_vault'], before)
        self.assertEqual(self.owner.post('/api/obsidian/vaults', json={'path':str(folder),'password':self.password}, headers=H).status_code, 400)

    def test_vault_creation_requires_owner_password_and_csrf(self):
        folder = self.workdir / 'Refuse'
        guest, uid = self.guest('vault-admin')
        self.owner.patch(f'/api/users/{uid}', json={'role':'admin'}, headers=H)
        self.assertEqual(guest.post('/api/obsidian/vaults', json={'path':str(folder)}, headers=H).status_code, 403)
        self.assertEqual(self.owner.post('/api/obsidian/vaults', json={'path':str(folder),'password':'wrong'}, headers=H).status_code, 401)
        self.assertEqual(self.owner.post('/api/obsidian/vaults', json={'path':str(folder),'password':self.password}).status_code, 403)
        self.assertFalse(folder.exists())

    def create(self, path, **extra):
        return self.owner.post('/api/filesystem/create', json={'path':str(path), 'password':self.password, **extra}, headers=H)

    def test_owner_creates_workdir_then_conversation_and_empty_file(self):
        folder = self.workdir / 'new project'
        result = self.create(folder)
        self.assertEqual(result.status_code, 200, result.text)
        cid = self.conversation(workdir=str(folder))
        result = self.create('src/new.txt', kind='file', conversation_id=cid)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((folder/'src/new.txt').read_bytes(), b'')

    def test_password_and_trusted_role_required_before_creation(self):
        folder = self.workdir / 'refused'
        result = self.owner.post('/api/filesystem/create', json={'path':str(folder),'password':'wrong'}, headers=H)
        self.assertEqual(result.status_code, 401)
        guest, uid = self.guest()
        with mock.patch.object(filesystem, 'create') as write:
            result = guest.post('/api/filesystem/create', json={'path':str(folder)}, headers=H)
            self.assertEqual(result.status_code, 403)
            write.assert_not_called()
        self.assertFalse(folder.exists())

    def test_administrator_confirms_with_their_own_password(self):
        admin, uid = self.guest('admin-test')
        self.assertEqual(self.owner.patch(f'/api/users/{uid}', json={'role':'admin'}, headers=H).status_code, 200)
        folder = self.workdir/'admin-created'
        result = admin.post('/api/filesystem/create', json={'path':str(folder),'password':'un bon mot de passe'}, headers=H)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(folder.is_dir())

    def test_role_revoked_during_password_verification_cannot_create(self):
        from aihub import auth
        admin, uid = self.guest('revoked-admin')
        self.owner.patch(f'/api/users/{uid}', json={'role':'admin'}, headers=H)
        original = auth.Auth.verify
        def revoke_then_verify(authority, conn, *args, **kwargs):
            conn.execute("UPDATE users SET role = 'invite' WHERE id = ?", (uid,))
            return original(authority, conn, *args, **kwargs)
        folder = self.workdir/'revoked'
        with mock.patch.object(auth.Auth, 'verify', revoke_then_verify):
            result = admin.post('/api/filesystem/create', json={'path':str(folder),'password':'un bon mot de passe'}, headers=H)
        self.assertEqual(result.status_code, 403, result.text)
        self.assertFalse(folder.exists())

    def test_api_applies_configured_roots_before_writing(self):
        from aihub import auth
        from aihub.web.app import create_app
        from .helpers import FAKE_COMMAND
        from fastapi.testclient import TestClient
        limited = create_app(self.db_path, command=FAKE_COMMAND, env=self.env,
                             allowed_roots=[self.workdir], auth_policy=auth.Policy(), run_scheduler=False)
        with TestClient(limited) as client:
            self.assertEqual(client.post('/api/auth/login', json={'password':self.password}).status_code, 200)
            outside = self.make_tempdir()/'outside'
            response = client.post('/api/filesystem/create', json={'path':str(outside),'password':self.password}, headers=H)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertFalse(outside.exists())

    def test_scope_pause_and_csrf_cannot_be_bypassed(self):
        cid = self.conversation()
        result = self.create('../escape', kind='file', conversation_id=cid)
        self.assertEqual(result.status_code, 400)
        self.owner.post(f'/api/conversations/{cid}/isolation/pause', headers=H)
        result = self.create('blocked.txt', kind='file', conversation_id=cid)
        self.assertEqual(result.status_code, 400)
        self.assertFalse((self.workdir/'blocked.txt').exists())
        result = self.owner.post('/api/filesystem/create', json={'path':str(self.workdir/'csrf'),'password':self.password})
        self.assertEqual(result.status_code, 403)
        self.assertFalse((self.workdir/'csrf').exists())
