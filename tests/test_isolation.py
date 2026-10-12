"""Isolation ceilings and snapshot boundaries, independent of an installed engine."""
import unittest
from pathlib import Path
from unittest import mock

from aihub import db, isolation, sandbox, store
from .helpers import TempDirMixin
from .test_sharing import H, SharingTestCase, wait_idle


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.pid = store.create_project(self.conn, 'isolé')
        self.cid = store.create_conversation(self.conn, 'conversation', project_id=self.pid)

    def test_pause_blocks_host_and_vm_and_preserves_account_identity_after_resume(self):
        for isolated in (False, True):
            policy = isolation.save(self.conn, {'isolated': isolated}, conversation_id=self.cid)
            conv = dict(store.get_conversation(self.conn, self.cid), workdir='.')
            before = isolation.for_run(self.conn, conv)
            identity = before.identity('codex') if before else None
            isolation.suspend(self.conn, self.cid)
            with self.assertRaisesRegex(sandbox.ProviderError, 'suspendus'):
                isolation.for_run(self.conn, conv)
            isolation.suspend(self.conn, self.cid, False)
            after = isolation.for_run(self.conn, conv)
            self.assertEqual(after.identity('codex') if after else None, identity)

    def test_pause_preserves_inheritance_and_explicit_preferences(self):
        for isolated in (False, True):
            self.conn.execute('DELETE FROM execution_policies WHERE conversation_id = ?', (self.cid,))
            isolation.save(self.conn, {'isolated': isolated}, project_id=self.pid)
            isolation.suspend(self.conn, self.cid)
            conv = store.get_conversation(self.conn, self.cid)
            self.assertTrue(isolation.effective(self.conn, conv)['paused'])
            self.assertIsNone(isolation.get(self.conn, conversation_id=self.cid))
            isolation.save(self.conn, {'isolated': isolated, 'network': 'none'}, project_id=self.pid)
            isolation.suspend(self.conn, self.cid, False)
            self.assertEqual(isolation.effective(self.conn, conv)['network'], 'none')
            self.assertIsNone(isolation.get(self.conn, conversation_id=self.cid))

    def test_invalid_pause_is_rejected(self):
        for value in ('false', 1, None):
            with self.assertRaises(store.StoreError):
                isolation.check({'paused': value})

    def test_untrusted_user_cannot_launch_on_host(self):
        from aihub import auth
        uid = auth.create_user(self.conn, 'restricted', 'test-password-long', role='membre')['id']
        conv = store.get_conversation(self.conn, self.cid)
        with self.assertRaisesRegex(sandbox.ProviderError, 'espace isolé'):
            isolation.for_run(self.conn, conv, account_id=uid)
        isolation.save(self.conn, {'isolated': True}, conversation_id=self.cid)
        self.assertIsInstance(isolation.for_run(self.conn, conv, account_id=uid), sandbox.SandboxExecution)
        with self.assertRaisesRegex(sandbox.ProviderError, 'indisponible'):
            isolation.for_run(self.conn, conv, account_id=999999)

    def test_recreated_conversation_has_a_new_empty_space_identity(self):
        isolation.save(self.conn, {'isolated': True}, conversation_id=self.cid)
        conv = {**dict(store.get_conversation(self.conn, self.cid)), 'workdir': '.'}
        before = isolation.for_run(self.conn, conv).identity('codex')
        self.assertEqual(isolation.for_run(self.conn, conv).identity('codex'), before)
        store.delete_conversation(self.conn, self.cid)
        recreated = store.create_conversation(self.conn, 'conversation', project_id=self.pid)
        self.assertEqual(recreated, self.cid)
        isolation.save(self.conn, {'isolated': True}, conversation_id=recreated)
        conv = {**dict(store.get_conversation(self.conn, recreated)), 'workdir': '.'}
        self.assertNotEqual(isolation.for_run(self.conn, conv).identity('codex'), before)

    def test_reused_user_id_never_reuses_a_logged_in_space(self):
        from aihub import auth
        first = auth.create_user(self.conn, 'first', 'password-long-enough', role='membre')['id']
        isolation.save(self.conn, {'isolated': True}, conversation_id=self.cid)
        conv = {**dict(store.get_conversation(self.conn, self.cid)), 'workdir': '.'}
        before = isolation.for_run(self.conn, conv, account_id=first).identity('codex')
        self.assertEqual(isolation.for_run(self.conn, conv, account_id=first).identity('codex'), before)
        auth.delete_user(self.conn, first)
        second = auth.create_user(self.conn, 'second', 'password-long-enough', role='membre')['id']
        self.assertEqual(second, first)  # reproduce SQLite's reuse, not just different ids
        after = isolation.for_run(self.conn, conv, account_id=second).identity('codex')
        self.assertNotEqual(before, after)
        auth.set_disabled(self.conn, second, True)
        with self.assertRaisesRegex(sandbox.ProviderError, 'indisponible'):
            isolation.for_run(self.conn, conv, account_id=second)

    def test_inheritance_and_no_expansion(self):
        parent = isolation.save(self.conn, {'isolated': True, 'files': 'read', 'paths': ['docs'],
                                            'network': 'none'}, project_id=self.pid)
        conv = store.get_conversation(self.conn, self.cid)
        self.assertEqual(isolation.effective(self.conn, conv), parent)
        for change in ({'isolated': False}, {'files': 'write'}, {'paths': ['outside']},
                       {'network': 'ai'}, {'memory': 'project'}):
            with self.subTest(change=change), self.assertRaises(store.StoreError):
                isolation.save(self.conn, {**parent, **change}, conversation_id=self.cid)
        isolation.save(self.conn, {**parent, 'paths': ['docs/guide.md']}, conversation_id=self.cid)
        self.assertEqual(isolation.effective(self.conn, conv)['paths'], ['docs/guide.md'])
        isolation.save(self.conn, {**parent, 'files': 'none', 'paths': []}, project_id=self.pid)
        reduced = isolation.effective(self.conn, conv)
        self.assertEqual((reduced['files'], reduced['paths']), ('none', []))

    def test_bad_paths_are_refused(self):
        for path in ('../secret', '/etc/passwd', 'C:/data', 'a\\..\\b', '.', 'x\nsecret'):
            with self.subTest(path=path), self.assertRaises(store.StoreError):
                isolation.check({'isolated': True, 'files': 'read', 'paths': [path]})

    def test_leaving_or_deleting_project_keeps_isolation(self):
        isolation.save(self.conn, {'isolated': True}, project_id=self.pid)
        store.move_conversation(self.conn, self.cid, None)
        self.assertTrue(isolation.effective(self.conn, store.get_conversation(self.conn, self.cid))['isolated'])
        second = store.create_conversation(self.conn, 'second', project_id=self.pid)
        store.delete_project(self.conn, self.pid)
        self.assertTrue(isolation.effective(self.conn, store.get_conversation(self.conn, second))['isolated'])

    def test_return_to_host_resets_vm_bypass(self):
        isolation.save(self.conn, {'isolated': True}, conversation_id=self.cid)
        store.set_permission_mode(self.conn, self.cid, 'bypass', confirm_bypass=True)
        isolation.save(self.conn, {'isolated': False}, conversation_id=self.cid)
        self.assertEqual(store.get_conversation(self.conn, self.cid)['permission_mode'], 'manual')


class IsolationWebTest(SharingTestCase):
    def test_pause_stops_an_active_job_and_survives_a_new_connection(self):
        cid = self.conversation()
        self.app.state.jobs.env['FAKE_CLAUDE_SCENARIO'] = 'slow'
        response = self.owner.post(f'/api/conversations/{cid}/messages', json={'prompt': 'go'}, headers=H)
        self.assertEqual(response.status_code, 202, response.text)
        response = self.owner.post(f'/api/conversations/{cid}/isolation/pause', headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        wait_idle(self.owner, cid)
        conn = db.connect(self.db_path)
        try:
            self.assertTrue(isolation.suspended(conn, cid))
        finally:
            conn.close()
        self.assertEqual(self.owner.post(f'/api/conversations/{cid}/messages', json={'prompt': 'again'}, headers=H).status_code, 400)

    def test_preparing_a_login_does_not_block_pause_or_start_after_it(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from aihub import accounts
        cid = self.conversation(isolation={'isolated': True})
        entered, release = threading.Event(), threading.Event()
        def prepare(context, provider, **kwargs):
            context.provider = provider
            entered.set()
            release.wait(10)
            return True
        with mock.patch.object(sandbox.SandboxExecution, 'prepare', prepare), \
             mock.patch.object(accounts.Accounts, 'cancel_all', return_value=True), \
             mock.patch.object(accounts.Accounts, 'start_login') as start:
            with ThreadPoolExecutor(max_workers=2) as pool:
                login = pool.submit(self.owner.post, f'/api/conversations/{cid}/sandbox/login', json={'provider':'claude_code'}, headers=H)
                try:
                    self.assertTrue(entered.wait(5))
                    stop = pool.submit(self.owner.post, f'/api/conversations/{cid}/isolation/pause', headers=H)
                    self.assertEqual(stop.result(timeout=5).status_code, 200)
                finally:
                    release.set()
                self.assertEqual(login.result(timeout=5).status_code, 400)
                start.assert_not_called()

    def test_pause_is_owner_only_durable_and_resume_restores_access(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, 'write', conversation_id=cid)
        route = f'/api/conversations/{cid}/isolation'
        self.assertEqual(guest.post(route + '/pause', headers=H).status_code, 403)
        self.assertEqual(self.owner.post(route + '/pause', headers=H).status_code, 200)
        self.assertTrue(self.owner.get(route, headers=H).json()['policy']['paused'])
        self.assertTrue(self.owner.get(route, headers=H).json()['inherited'])
        self.owner.put(route, json={'isolated': False}, headers=H)
        self.assertTrue(self.owner.get(route, headers=H).json()['policy']['paused'])
        with mock.patch('aihub.providers.claude_code.ClaudeCodeRun.run') as launch:
            response = self.owner.post(f'/api/conversations/{cid}/messages', json={'prompt': 'go'}, headers=H)
            self.assertEqual(response.status_code, 400, response.text)
            launch.assert_not_called()
        self.assertEqual(guest.post(route + '/resume', headers=H).status_code, 403)
        self.assertEqual(self.owner.post(route + '/resume', headers=H).status_code, 200)
        self.assertFalse(self.owner.get(route, headers=H).json()['policy'].get('paused'))

    def test_diagnostic_unavailable_engine_has_action_without_raw_errors(self):
        with mock.patch('aihub.sandbox.find_cli', side_effect=sandbox.ProviderError('private path and secret')):
            response = self.owner.get('/api/diagnostics', headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        checks = response.json()['checks']
        self.assertFalse(checks[-1]['ok'])
        self.assertTrue(checks[-1]['action'])
        self.assertNotIn('private path', response.text)
        self.assertNotIn('secret', response.text)

    def test_disabled_or_deleted_users_have_their_work_stopped(self):
        guest, uid = self.guest()
        with mock.patch.object(self.app.state.jobs, 'stop_user') as stop:
            response = self.owner.patch(f'/api/users/{uid}', json={'disabled': True}, headers=H)
            self.assertEqual(response.status_code, 200, response.text)
            stop.assert_called_once_with(uid)
        with mock.patch.object(self.app.state.jobs, 'stop_user') as stop:
            response = self.owner.delete(f'/api/users/{uid}', headers=H)
            self.assertEqual(response.status_code, 200, response.text)
            stop.assert_called_once_with(uid)

    def test_guest_host_request_is_refused_before_any_launch(self):
        guest, uid = self.guest()
        cid = self.conversation()
        self.share(uid, 'write', conversation_id=cid)
        with mock.patch('aihub.providers.claude_code.ClaudeCodeRun.run') as host:
            response = guest.post(f'/api/conversations/{cid}/messages', json={'prompt': 'analyse'}, headers=H)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn('espace isolé', response.json()['detail'])
            host.assert_not_called()
        self.assertEqual(guest.get(f'/api/conversations/{cid}', headers=H).json()['messages'], [])

    def test_project_ceiling_cannot_be_expanded_by_conversation(self):
        profile = {'isolated': True, 'network': 'none'}
        response = self.owner.post('/api/projects', json={'name': 'Protégé', 'isolation': profile}, headers=H)
        self.assertEqual(response.status_code, 201, response.text)
        pid = response.json()['project']['id']
        cid = self.conversation(project_id=pid)
        detail = self.owner.get(f'/api/conversations/{cid}', headers=H).json()['conversation']
        self.assertTrue(detail['isolation']['isolated'])
        response = self.owner.put(f'/api/conversations/{cid}/isolation', json={'isolated': False}, headers=H)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(self.owner.get(f'/api/conversations/{cid}/isolation', headers=H).json()['inherited'])

    def test_unavailable_engine_never_runs_the_host_provider(self):
        cid = self.conversation(isolation={'isolated': True})
        with mock.patch('aihub.sandbox.find_cli', side_effect=sandbox.ProviderError('moteur indisponible')), \
                mock.patch('aihub.providers.claude_code.ClaudeCodeRun.run') as host:
            response = self.owner.post(f'/api/conversations/{cid}/messages', json={'prompt': 'analyse'}, headers=H)
            self.assertEqual(response.status_code, 202, response.text)
            wait_idle(self.owner, cid)
            host.assert_not_called()
        messages = self.owner.get(f'/api/conversations/{cid}', headers=H).json()['messages']
        self.assertEqual(messages[-1]['status'], 'error')
        self.assertIn('moteur indisponible', messages[-1]['error'])


class LocalOnlySecurityTest(TempDirMixin, unittest.TestCase):
    def test_cross_site_posts_and_rebinding_hosts_are_refused(self):
        from fastapi.testclient import TestClient
        from aihub.web.app import create_app
        from tests.helpers import FAKE_COMMAND
        app = create_app(str(self.make_tempdir() / 'local.db'), command=FAKE_COMMAND, local_only=True,
                         run_scheduler=False)
        with TestClient(app, base_url='http://127.0.0.1:8766', client=('127.0.0.1', 50000)) as client:
            response = client.post('/api/conversations', json={'title':'untrusted','workdir':str(self.make_tempdir())})
            self.assertEqual(response.status_code,403)
            self.assertEqual(response.json()['code'],'csrf')
            response = client.get('/api/options', headers={'Host':'attacker.test:8766'})
            self.assertEqual(response.status_code,403)
            self.assertEqual(response.json()['code'],'local_host')
            self.assertEqual(client.get('/api/auth/session').status_code,200)


class SnapshotTest(TempDirMixin, unittest.TestCase):
    def test_only_selection_is_copied_and_original_is_independent(self):
        root = self.make_tempdir()
        source, destination = root / 'source', root / 'copy'
        (source / 'docs').mkdir(parents=True)
        (source / 'docs/guide.md').write_text('original', encoding='utf-8')
        (source / '.env').write_text('never copy', encoding='utf-8')
        (source / 'unselected.txt').write_text('private', encoding='utf-8')
        manifest = sandbox.copy_selection(source, destination, ['docs', '.env'])
        self.assertEqual(list(manifest), ['docs/guide.md'])
        self.assertFalse((destination / '.env').exists())
        self.assertFalse((destination / 'unselected.txt').exists())
        (destination / 'docs/guide.md').write_text('changed', encoding='utf-8')
        self.assertEqual((source / 'docs/guide.md').read_text(encoding='utf-8'), 'original')

    def test_symlink_cannot_escape_selection(self):
        root = self.make_tempdir()
        source = root / 'source'
        source.mkdir()
        (root / 'outside').write_text('private', encoding='utf-8')
        try:
            (source / 'link').symlink_to(root / 'outside')
        except OSError:
            self.skipTest('symlinks require Windows privileges')
        with self.assertRaises(sandbox.ProviderError):
            sandbox.copy_selection(source, root / 'copy', ['link'])


if __name__ == '__main__':
    unittest.main()
