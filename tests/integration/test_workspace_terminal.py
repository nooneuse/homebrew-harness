"""Actual terminal restoration and exit aliases through both interactive entries."""

import json
import os
import select
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from homebrew_harness.storage.durable_runtime import DurableStore
from tests.integration.test_native_model_protocols import ProtocolService
from tests.integration.test_model_backends import chat


@unittest.skipIf(os.name == 'nt', 'Requires a POSIX terminal.')
class WorkspaceTerminal(unittest.TestCase):
    def scenario(self, explicit):
        import pty
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();work=root/'work';work.mkdir()
            server=ProtocolService([chat('archived-terminal-answer'),chat('continued-terminal-answer')])
            env=root/'model.env';env.write_text('OPENAI_API_KEY=terminal-fixture\nOPENAI_BASE_URL='+server.url+'\nOPENAI_MODEL=fixture\n');env.chmod(0o600)
            config=root/'host.json';config.write_text(json.dumps({'env_file':str(env),'model_planning':False,'prefer_session':True}))
            argv=['-m','homebrew_harness','session','--env-file',str(env)] if explicit else ['-m','homebrew_harness','--host-config',str(config)]
            pid,fd=pty.fork()
            if pid==0:
                os.chdir(work);os.execv(sys.executable,[sys.executable,'-I','-B',*argv])
            captured=b''
            def wait(marker,start=0):
                nonlocal captured
                end=time.monotonic()+20
                while marker not in captured[start:]:
                    if time.monotonic()>end:self.fail('Terminal missed '+marker.decode()+'; '+captured[-1500:].decode(errors='replace'))
                    ready,_,_=select.select([fd],[],[],.1)
                    if ready:
                        try:captured+=os.read(fd,65536)
                        except OSError:self.fail('Terminal closed before '+marker.decode())
            def send(text):
                start=len(captured);os.write(fd,text.encode()+b'\n');return start
            try:
                wait(b'agent> ')
                for text,marker in [('/pwd',b'"cwd":'),('/memory',b'"records":'),('/hooks',b'permission_hook'),('/ps',b'"ps":'),('/agents',b'"agents":')]:
                    start=send(text);wait(marker,start)
                self.assertEqual(len(server.requests),0)
                start=send('First task');wait(b'archived-terminal-answer',start)
                time.sleep(.2)
                rows=DurableStore(work).items('checkpoints')
                sid=next(value['session_id'] for key,value in rows.items() if key.startswith('lead:') and value.get('intent',{}).get('original')=='First task')
                start=send('/new');wait(b'agent> ',start)
                start=send('/resume list');wait(b'session:'+sid.encode(),start)
                start=send('/resume session:'+sid)
                wait(b'agent> ' if explicit else b'Session restored;',start)
                self.assertEqual(len(server.requests),1)
                start=send('Continue with the saved context');wait(b'continued-terminal-answer',start)
                self.assertTrue(any('archived-terminal-answer' in str(m) for m in server.requests[-1]['messages']))
                self.assertNotIn(b'terminal-fixture',captured)
                send('/exit' if explicit else '/quit')
                end=time.monotonic()+10
                while time.monotonic()<end:
                    done,status=os.waitpid(pid,os.WNOHANG)
                    if done:
                        self.assertEqual(os.waitstatus_to_exitcode(status),0);pid=None;break
                    time.sleep(.05)
                self.assertIsNone(pid)
            finally:
                if pid is not None:
                    try:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                    except (ProcessLookupError,ChildProcessError):pass
                os.close(fd);server.close()

    def test_default_restore_and_quit(self):
        self.scenario(False)

    def test_explicit_restore_and_exit(self):
        self.scenario(True)
