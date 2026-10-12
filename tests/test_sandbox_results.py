"""Copied work: explicit apply, conflicts, backups and hostile output boundaries."""
import hashlib
import io
import json
import tarfile
import unittest
import zipfile
from unittest import mock

from aihub import sandbox, sandbox_results as results, store
from .helpers import TempDirMixin


class ResultsTest(TempDirMixin, unittest.TestCase):
    def fixture(self, access='write'):
        directory = self.make_tempdir()
        source = directory / 'original'
        (source / 'docs').mkdir(parents=True)
        (source / 'docs/a.txt').write_text('original', encoding='utf-8')
        context = sandbox.SandboxExecution(str(directory / 'test.db'), {'id': 1, 'workdir': str(source)},
                                           {'isolated': True, 'files': access, 'paths': ['docs'] if access != 'none' else [],
                                            'network': 'none', 'memory': 'own'})
        root, selection = results.locations(context, 'codex')
        root.mkdir(parents=True)
        selection.mkdir()
        before = sandbox.copy_selection(source, selection, context.policy['paths'])
        (root / 'manifest.json').write_text(json.dumps(before), encoding='utf-8')
        return context, source, root, selection

    def test_review_archive_and_explicit_apply_with_backup(self):
        context, source, root, selection = self.fixture()
        (selection / 'docs/a.txt').write_text('changed', encoding='utf-8')
        (selection / 'docs/new.txt').write_text('new', encoding='utf-8')
        self.assertEqual((source / 'docs/a.txt').read_text(encoding='utf-8'), 'original')
        changes = results.review(context, 'codex')
        self.assertEqual([c['kind'] for c in changes], ['modified', 'new'])
        self.assertIn('-original', results.preview(context, 'codex', 'docs/a.txt')['diff'])
        self.assertIn('+changed', results.preview(context, 'codex', 'docs/a.txt')['diff'])
        with zipfile.ZipFile(io.BytesIO(results.download(context, 'codex'))) as archive:
            self.assertEqual(archive.read('docs/a.txt'), b'changed')
        applied = results.apply(context, 'codex', {c['name']: c['sha256'] for c in changes})
        self.assertEqual((source / 'docs/a.txt').read_text(encoding='utf-8'), 'changed')
        from pathlib import Path
        self.assertEqual((Path(applied['backup']) / 'docs/a.txt').read_text(encoding='utf-8'), 'original')
        self.assertEqual(results.review(context, 'codex'), [])

    def test_original_conflict_refuses_every_change(self):
        context, source, root, selection = self.fixture()
        (selection / 'docs/a.txt').write_text('copy', encoding='utf-8')
        (selection / 'docs/new.txt').write_text('new', encoding='utf-8')
        (source / 'docs/a.txt').write_text('human edit', encoding='utf-8')
        changes = results.review(context, 'codex')
        with self.assertRaisesRegex(store.StoreError, 'original a changé'):
            results.apply(context, 'codex', {c['name']: c['sha256'] for c in changes})
        self.assertFalse((source / 'docs/new.txt').exists())
        self.assertEqual((source / 'docs/a.txt').read_text(encoding='utf-8'), 'human edit')

    def test_copy_outside_selection_cannot_be_applied(self):
        context, source, root, selection = self.fixture()
        (selection / 'outside.txt').write_text('generated', encoding='utf-8')
        digest = hashlib.sha256(b'generated').hexdigest()
        with self.assertRaisesRegex(store.StoreError, 'dépasse'):
            results.apply(context, 'codex', {'outside.txt': digest})
        self.assertFalse((source / 'outside.txt').exists())

    def test_deletion_requires_confirmation_and_is_backed_up(self):
        context, source, root, selection = self.fixture()
        (selection / 'docs/a.txt').unlink()
        with self.assertRaisesRegex(store.StoreError, 'confirmez'):
            results.apply(context, 'codex', {'docs/a.txt': None})
        result = results.apply(context, 'codex', {'docs/a.txt': None}, confirm_delete=True)
        self.assertFalse((source / 'docs/a.txt').exists())
        from pathlib import Path
        self.assertEqual((Path(result['backup']) / 'docs/a.txt').read_text(encoding='utf-8'), 'original')

    def test_untrusted_tar_link_never_writes_outside_capture(self):
        context, source, root, selection = self.fixture('none')
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            link = tarfile.TarInfo('escape')
            link.type = tarfile.SYMTYPE; link.linkname = str(source)
            archive.addfile(link)
        process = mock.Mock(stdout=io.BytesIO(output.getvalue()), returncode=0)
        process.wait.return_value = 0; process.poll.return_value = 0
        with mock.patch.object(context, 'prepare', return_value=context), mock.patch.object(context, 'spawn', return_value=process), \
                mock.patch.object(context, 'finish'):
            with self.assertRaisesRegex(store.StoreError, 'invalide'):
                results.collect(context, 'codex')
        self.assertEqual((source / 'docs/a.txt').read_text(encoding='utf-8'), 'original')


if __name__ == '__main__':
    unittest.main()
