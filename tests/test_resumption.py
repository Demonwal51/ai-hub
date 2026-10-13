import json
import unittest

from aihub import db, isolation, resumption, store
from .helpers import read_log, wait_for_log_key
from .test_orchestration import OrchestrationTestCase, subtask
from .test_web import WebTestCase
from .test_sharing import H, SharingTestCase


class PlanResumeTests(OrchestrationTestCase):
    def interrupted(self):
        self.use_plan(max_parallel=1, subtasks=[subtask('A', files=['a.txt']), subtask('B', depends_on=[1], files=['b.txt'])])
        pid = self.ready_plan()
        store.start_plan(self.conn, pid)
        tasks = store.get_plan(self.conn, pid)['subtasks']
        store.start_subtask(self.conn, tasks[0]['id'])
        store.finish_subtask(self.conn, tasks[0]['id'], 'ok', result='A est terminée.')
        store.start_subtask(self.conn, tasks[1]['id'])
        scope = resumption.scope(self.conn, store.get_conversation(self.conn, self.cid))
        self.conn.execute('UPDATE subtasks SET checkpoint = ?, session_id = ?, session_scope = ? WHERE id = ?',
                          ('B : première étape déjà faite.', 'saved-session', scope, tasks[1]['id']))
        store.recover_interrupted(self.conn)
        return pid, tasks

    def test_resume_after_restart_skips_completed_tasks_and_keeps_checkpoint(self):
        pid, tasks = self.interrupted()
        plan = store.get_plan(self.conn, pid)
        self.assertTrue(plan['resumable'])
        self.assertEqual(plan['subtasks'][1]['checkpoint'], 'B : première étape déjà faite.')
        store.resume_plan(self.conn, pid)
        self.env['FAKE_CLAUDE_SCENARIO'] = 'text'
        status, events, _ = self.execute(pid)
        self.assertEqual(status, 'done')
        self.assertEqual([e['subtask_id'] for e in events if e['type'] == 'subtask_started'], [tasks[1]['id']])
        finished = store.get_plan(self.conn, pid)['subtasks']
        self.assertEqual((finished[0]['result'], finished[0]['attempts']), ('A est terminée.', 1))
        self.assertEqual((finished[1]['status'], finished[1]['attempts']), ('ok', 2))
        runs = [json.loads(p.read_text(encoding='utf-8')) for p in self.logs.glob('run-*.json')]
        resumed = next(r for r in runs if '--resume' in r['args'])
        self.assertIn('première étape déjà faite', resumed['user_message']['message']['content'])

    def test_missing_provider_session_falls_back_to_checkpoint(self):
        pid, tasks = self.interrupted()
        store.resume_plan(self.conn, pid)
        self.env.update(FAKE_CLAUDE_SCENARIO='text', FAKE_LOST_SESSION='1')
        self.assertEqual(self.execute(pid)[0], 'done')
        self.assertEqual(store.get_plan(self.conn, pid)['subtasks'][1]['status'], 'ok')

    def test_cancelled_draft_cannot_bypass_validation_by_resuming(self):
        pid, _ = self.propose()
        store.stop_plan(self.conn, pid)
        self.assertFalse(store.get_plan(self.conn, pid)['resumable'])
        with self.assertRaises(store.StoreError):
            store.resume_plan(self.conn, pid)

    def test_other_open_plan_and_changed_workdir_are_refused(self):
        pid, _ = self.interrupted()
        store.update_conversation(self.conn, self.cid, workdir=str(self.make_tempdir()))
        with self.assertRaisesRegex(store.StoreError, 'dossier'):
            store.resume_plan(self.conn, pid)
        store.update_conversation(self.conn, self.cid, workdir=str(self.workdir))
        self.propose()
        with self.assertRaises(store.Busy):
            store.resume_plan(self.conn, pid)

    def test_only_interrupted_synthesis_can_be_resumed_without_replaying_tasks(self):
        pid = self.ready_plan()
        store.start_plan(self.conn, pid)
        while ready := store.ready_subtasks(self.conn, pid):
            for task in ready:
                store.start_subtask(self.conn, task['id'])
                store.finish_subtask(self.conn, task['id'], 'ok', result='Résultat acquis.')
        store.finish_plan(self.conn, pid)
        mid = store.start_request(self.conn, self.cid, self.chef)
        store.mark_message_running(self.conn, mid)
        scope = resumption.scope(self.conn, store.get_conversation(self.conn, self.cid))
        store.finish_message(self.conn, mid, 'stopped', content='Début de synthèse.', session_id='synthesis-session', session_scope=scope)
        store.set_plan_synthesis(self.conn, pid, mid)
        store.resume_plan(self.conn, pid)
        self.env['FAKE_CLAUDE_SCENARIO'] = 'text'
        status, events, _ = self.execute(pid)
        self.assertEqual(status, 'done')
        self.assertFalse(any(event['type'] == 'subtask_started' for event in events))
        self.assertNotEqual(store.get_plan(self.conn, pid)['synthesis_message_id'], mid)

    def test_access_context_invalidates_subtask_sessions(self):
        pid, tasks = self.interrupted()
        before = resumption.scope(self.conn, store.get_conversation(self.conn, self.cid))
        isolation.save(self.conn, {'isolated':False,'memory':'project'}, conversation_id=self.cid)
        self.assertNotEqual(resumption.scope(self.conn, store.get_conversation(self.conn, self.cid)), before)
        self.assertIsNone(store.get_plan(self.conn, pid)['subtasks'][1]['session_id'])


class MessageResumeTests(WebTestCase):
    def stopped(self):
        self.use_scenario('hang')
        cid = self.new_conversation()
        self.send(cid, 'Continue le projet Boréale.')
        wait_for_log_key(self.log, 'child_pid')
        running = self.conversation(cid)['messages'][-1]
        self.assertTrue(running['session_id'])
        self.assertTrue(running['session_scope'])
        self.client.post(f'/api/conversations/{cid}/stop')
        reply = self.wait_idle(cid)['messages'][-1]
        return cid, reply

    def test_partial_reply_and_session_are_saved_before_stop_and_reused(self):
        cid, reply = self.stopped()
        self.assertIn('je travaille', reply['content'])
        self.use_scenario('text')
        result = self.client.post(f"/api/messages/{reply['id']}/resume")
        self.assertEqual(result.status_code, 202, result.text)
        data = self.wait_idle(cid)
        self.assertEqual(data['messages'][-1]['status'], 'ok')
        log = read_log(self.log)
        self.assertIn('--resume', log['args'])
        self.assertIn('je travaille', log['user_message']['message']['content'])
        self.assertEqual(data['messages'][-3]['id'], reply['id'])
        self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/resume").status_code, 400)

    def test_changed_permissions_start_fresh_and_keep_current_mode(self):
        cid, reply = self.stopped()
        self.client.patch(f'/api/conversations/{cid}', json={'permission_mode':'plan'})
        self.use_scenario('text')
        self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/resume").status_code, 202)
        self.wait_idle(cid)
        self.assertNotIn('--resume', read_log(self.env['FAKE_CLAUDE_LOG'])['args'])
        self.assertEqual(self.conversation(cid)['conversation']['permission_mode'], 'plan')

    def test_paused_space_refuses_resume(self):
        cid, reply = self.stopped()
        self.client.post(f'/api/conversations/{cid}/isolation/pause')
        self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/resume").status_code, 400)

    def test_resume_keeps_original_attachment_association(self):
        self.use_scenario('hang')
        cid = self.new_conversation()
        conn = db.connect(self.db_path)
        try:
            from aihub import attachments
            item = attachments.save(conn, self.make_tempdir(), cid, None, 'note.txt', b'Contexte original')
        finally:
            conn.close()
        self.client.post(f'/api/conversations/{cid}/messages', json={'prompt':'travaille','attachments':[item['id']]})
        wait_for_log_key(self.log, 'child_pid')
        self.client.post(f'/api/conversations/{cid}/stop')
        data = self.wait_idle(cid)
        reply = data['messages'][-1]
        request_id = data['messages'][-2]['id']
        self.use_scenario('text')
        self.assertEqual(self.client.post(f"/api/messages/{reply['id']}/resume").status_code, 202)
        self.wait_idle(cid)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(conn.execute('SELECT message_id FROM attachments WHERE id = ?', (item['id'],)).fetchone()[0], request_id)
        finally:
            conn.close()
        self.assertIn('Contexte original', read_log(self.log)['user_message']['message']['content'])


class ResumeSharingTests(SharingTestCase):
    def test_read_only_refused_and_writer_never_resumes_another_persons_session(self):
        cid = self.conversation()
        conn = db.connect(self.db_path)
        try:
            conv = store.get_conversation(conn, cid)
            owner_id = conn.execute('SELECT id FROM users WHERE is_owner = 1').fetchone()[0]
            store.add_user_message(conn, cid, 'objectif', owner_id)
            mid = store.start_request(conn, cid, conv['agent_id'])
            store.mark_message_running(conn, mid)
            store.finish_message(conn, mid, 'stopped', content='Une étape faite.', session_id='private-owner-session',
                                 session_scope=resumption.scope(conn, conv, owner_id))
        finally:
            conn.close()
        guest, uid = self.guest()
        self.share(uid, 'read', conversation_id=cid)
        self.assertEqual(guest.post(f'/api/messages/{mid}/resume', headers=H).status_code, 403)
        self.share(uid, 'write', conversation_id=cid)
        self.assertEqual(guest.post(f'/api/messages/{mid}/resume', headers=H).status_code, 400)  # an invite cannot execute on the host
        self.owner.patch(f'/api/users/{uid}', json={'role':'admin'}, headers=H)
        response = guest.post(f'/api/messages/{mid}/resume', headers=H)
        self.assertEqual(response.status_code, 202, response.text)
        from .test_web import wait_until
        wait_until(lambda: not self.owner.get(f'/api/conversations/{cid}').json()['running'])
        self.assertNotIn('--resume', read_log(self.env['FAKE_CLAUDE_LOG'])['args'])


class PlanResumeWebTests(WebTestCase):
    scenario = 'auto'

    def test_running_subtask_checkpoint_survives_stop_and_can_resume(self):
        self.env['FAKE_PLAN'] = json.dumps({'summary':'essai', 'max_parallel':1, 'subtasks':[
            {'title':'Long', 'description':'attends longtemps', 'kind':'code', 'depends_on':[], 'files':['l.txt']}]})
        cid = self.new_conversation(mode='orchestration', conductor_model='opus')
        self.send(cid, 'fais')
        plan = self.wait_idle(cid)['plans'][0]
        agent = self.client.post('/api/agents', json={'model':'sonnet'}).json()['agent']['id']
        self.client.post(f"/api/plans/{plan['id']}/assign-all", json={'agent_id':agent})
        self.client.post(f"/api/plans/{plan['id']}/launch")
        from .test_web import wait_until
        checkpoint = wait_until(lambda: (task if (task := self.conversation(cid)['plans'][0]['subtasks'][0])['checkpoint'] else None))
        self.assertIn('je travaille', checkpoint['checkpoint'])
        self.assertTrue(checkpoint['session_id'])
        self.client.post(f'/api/conversations/{cid}/stop')
        stopped = self.wait_idle(cid)['plans'][0]
        self.assertTrue(stopped['resumable'])
        self.assertIn('je travaille', stopped['subtasks'][0]['checkpoint'])
        self.use_scenario('text')
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/resume").status_code, 202)
        self.assertEqual(self.wait_idle(cid)['plans'][0]['status'], 'done')

    def test_failed_plan_is_resumed_and_duplicate_or_cancelled_draft_is_refused(self):
        cid = self.new_conversation(mode='orchestration', conductor_model='opus')
        self.send(cid, 'prépare')
        plan = self.wait_idle(cid)['plans'][0]
        agent = self.client.post('/api/agents', json={'model':'sonnet'}).json()['agent']['id']
        self.client.post(f"/api/plans/{plan['id']}/assign-all", json={'agent_id':agent})
        self.use_scenario('quota')
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/launch").status_code, 202)
        data = self.wait_idle(cid)
        self.assertEqual(data['plans'][0]['status'], 'error')
        self.use_scenario('text')
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/resume").status_code, 202)
        self.assertEqual(self.wait_idle(cid)['plans'][0]['status'], 'done')
        self.assertEqual(self.client.post(f"/api/plans/{plan['id']}/resume").status_code, 400)

        self.use_scenario('auto')
        self.send(cid, 'autre plan')
        draft = self.wait_idle(cid)['plans'][-1]
        self.client.post(f"/api/plans/{draft['id']}/cancel")
        self.assertEqual(self.client.post(f"/api/plans/{draft['id']}/resume").status_code, 400)


class PromptTests(unittest.TestCase):
    def test_resume_does_not_forward_raw_provider_authentication_errors(self):
        text = resumption.prompt('objectif', 'travail partiel', 'incorrect API key: PRIVATE_TEST_KEY')
        self.assertNotIn('PRIVATE_TEST_KEY', text)
        self.assertIn('travail partiel', text)
