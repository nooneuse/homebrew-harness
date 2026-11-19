import json
import os
import stat
import threading
import unittest
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.model_gateway import Deadline, ModelCancelled
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.tools.files import run_delete
from tests.tools import test_files as fixture
from tests.support.clients import fake_client, reply, call


class DeleteFileTests(unittest.TestCase):
    setUp = fixture.FileToolTests.setUp
    invoke = fixture.FileToolTests.invoke

    def test_delete_uses_the_read_version_and_reports_missing_separately(self):
        path = '目录/empty file.txt'
        self.invoke('write_file', path=path, content='')
        version = self.invoke('read_file', path=path)['version']
        result = self.invoke('delete_file', path=path, expected_version=version)
        self.assertTrue(result['deleted'])
        self.assertEqual(result['previous_version'], version)
        self.assertFalse((self.workdir/path).exists())
        self.assertEqual(self.invoke('delete_file', path=path), {
            'path': path, 'deleted': False, 'already_absent': True,
        })

    def test_changed_file_is_not_deleted(self):
        target = self.workdir/'file.txt'
        target.write_text('before')
        version = self.invoke('read_file', path='file.txt')['version']
        target.write_text('after')
        result = self.invoke('delete_file', path='file.txt', expected_version=version)
        self.assertIn('File changed', result['error'])
        self.assertEqual(target.read_text(), 'after')

    def test_large_binary_removal_does_not_read_a_content_snapshot(self):
        target = self.workdir/'large.bin'
<<<<<<< 44b0af9717f2a641ffc1e4acec8aa82686f1c94c
        with target.open('wb') as stream:
            stream.truncate(65*1024*1024)
=======
        # The registered test runner applies RLIMIT_FSIZE=64,000,000 bytes, so the
        # fixture must stay below it. The regression is size-independent: deletion
        # must never call SafeFile.snapshot (patched below to raise on any call).
        with target.open('wb') as stream:
            stream.truncate(48*1024*1024)
>>>>>>> d1dd8171a8bb0c53197d34e3d89c7c23bf488f2a
        with patch.object(SafeFile, 'snapshot', side_effect=AssertionError('Deletion must not read file bytes.')):
            self.assertTrue(json.loads(run_delete('large.bin', self.workdir))['deleted'])

    def test_replaced_leaf_is_rechecked_immediately_before_removal(self):
        target = self.workdir/'file.txt';target.write_text('before')
        original = SafeFile.unlink
        def changed(owner, *args, **kwargs):
            target.write_text('replacement')
            return original(owner, *args, **kwargs)
        with patch.object(SafeFile, 'unlink', changed):
            result = self.invoke('delete_file', path='file.txt')
        self.assertIn('File changed', result['error'])
        self.assertEqual(target.read_text(), 'replacement')

    def test_directories_symlinks_outside_and_protected_paths_are_rejected(self):
        (self.workdir/'directory').mkdir()
        (self.workdir/'inside.txt').write_text('keep')
        (self.workdir/'inside-link').symlink_to(self.workdir/'inside.txt')
        (self.workdir/'outside-link').symlink_to(self.outside/'outside.txt')
        protected = ['.env', '.git/HEAD', '.harness/private.txt']
        for name in protected:
            path=self.workdir/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('keep')
        for name in ['directory','inside-link','outside-link','../workspace-peer/outside.txt',*protected]:
            with self.subTest(path=name):
                self.assertIn('error', self.invoke('delete_file',path=name))
        self.assertTrue((self.workdir/'directory').is_dir())
        self.assertTrue((self.workdir/'inside-link').is_symlink())
        self.assertEqual((self.workdir/'inside.txt').read_text(),'keep')
        self.assertEqual((self.outside/'outside.txt').read_text(),'keep me')
        for name in protected:self.assertEqual((self.workdir/name).read_text(),'keep')

    def test_cancellation_before_acceptance_keeps_the_file(self):
        (self.workdir/'file.txt').write_text('keep')
        cancel=threading.Event();cancel.set()
        with control_scope(Deadline.after(5),cancel_events=(cancel,)),self.assertRaises(ModelCancelled):
            run_delete('file.txt',self.workdir)
        self.assertEqual((self.workdir/'file.txt').read_text(),'keep')

    def test_read_only_plan_does_not_offer_or_execute_deletion(self):
        (self.workdir/'file.txt').write_text('keep')
        client,_=fake_client([
            reply(calls=[call('delete',name='delete_file',arguments=json.dumps({'path':'file.txt'}))]),
            reply('Read-only investigation plan.'),
        ])
        with HarnessSession(client,'fixture',self.workdir) as session:
            session.run_user('/plan')
            session.run_user('Investigate removing the file.')
            self.assertEqual((self.workdir/'file.txt').read_text(),'keep')
            tools=client.chat.completions.create.call_args_list[0].kwargs['tools']
            self.assertNotIn('delete_file',{t['function']['name'] for t in tools})

    def test_post_removal_failure_retains_unknown_and_stops_the_batch(self):
        (self.workdir/'file.txt').write_text('remove')
        client,_=fake_client([reply(calls=[
            call('delete',name='delete_file',arguments=json.dumps({'path':'file.txt'})),
            call('next',name='write_file',arguments=json.dumps({'path':'next.txt','content':'must not execute'})),
        ])])
        session=HarnessSession(client,'fixture',self.workdir)
        self.addCleanup(lambda:session.wait_closed(5));self.addCleanup(session.close)
        fsync=os.fsync;failed=[];inode=self.workdir.stat().st_ino
        def fail(fd):
            info=os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino==inode and not (self.workdir/'file.txt').exists() and not failed:
                failed.append(True);raise OSError('fixture removal durability')
            return fsync(fd)
        with patch('os.fsync',fail),self.assertRaises(RecoveryRequired):
            session.run_user('Delete the file.')
        self.assertFalse((self.workdir/'file.txt').exists())
        self.assertFalse((self.workdir/'next.txt').exists())
        output=json.loads(next(m['content'] for m in session.messages if m.get('tool_call_id')=='delete'))
        self.assertEqual(output['outcome'],'unknown')
        self.assertTrue(output['details']['removed'])
        self.assertFalse(output['details']['durability_confirmed'])
        self.assertEqual(len(session.recovery_operations()),1)
