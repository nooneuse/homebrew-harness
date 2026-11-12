"""Real terminal init, planning, fork and execution for both entry points."""

import json
import os
import re
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


@unittest.skipIf(os.name=='nt','Requires a POSIX terminal.')
class ConversationTerminal(unittest.TestCase):
    def scenario(self, explicit):
        import pty
        write={'path':'output.py','content':'value = 1\n'}
        server=ProtocolService([chat('# Project guidance\nUse explicit functions.'),chat('Parent plan: create output.py.'),chat('Child plan: create output.py and inspect it.'),chat(tool=write),chat('terminal-executed')])
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();work=root/'work';work.mkdir()
            env=root/'model.env';env.write_text('OPENAI_API_KEY=terminal-fixture\nOPENAI_BASE_URL='+server.url+'\nOPENAI_MODEL=fixture\n');env.chmod(0o600)
            config=root/'host.json';config.write_text(json.dumps({'env_file':str(env),'model_planning':False,'prefer_session':True}))
            argv=['-m','homebrew_harness','session','--env-file',str(env)] if explicit else ['-m','homebrew_harness','--host-config',str(config)]
            pid,fd=pty.fork()
            if pid==0:
                os.chdir(work);os.execv(sys.executable,[sys.executable,'-I','-B',*argv])
            captured=b''
            def wait(marker,start=0):
                nonlocal captured
                end=time.monotonic()+25
                while marker not in captured[start:]:
                    if time.monotonic()>end:self.fail('Terminal missed '+marker.decode()+'; '+captured[-1700:].decode(errors='replace'))
                    ready,_,_=select.select([fd],[],[],.1)
                    if ready:
                        try:captured+=os.read(fd,65536)
                        except OSError:self.fail('Terminal closed before '+marker.decode())
            def send(text):
                start=len(captured);os.write(fd,text.encode()+b'\n');return start
            try:
                wait(b'agent> ')
                start=send('/init');wait(b'"published": true',start)
                self.assertTrue((work/'AGENTS.md').exists())
                if not explicit:
                    end=time.monotonic()+10
                    while time.monotonic()<end:
                        store=DurableStore(work);sid=store.get_meta('latest_session')
                        if store.get('session_meta',sid,{}).get('close_complete'):break
                        time.sleep(.03)
                start=send('/plan');wait(b'plan',start)
                start=send('Create output.py');wait(b'Plan revision ',start)
                self.assertFalse((work/'output.py').exists())
                start=send('/fork Compare another approach')
                if not explicit:wait(b'Forked Session ',start)
                wait(b'Child plan:',start);wait(b'Plan revision 1',start)
                self.assertTrue(any('Parent plan:' in str(m) for m in server.requests[2]['messages']))
                self.assertTrue(any('explicit functions' in str(m) for m in server.requests[2]['messages']))
                self.assertFalse((work/'output.py').exists())
                start=send('/plan apply 1');wait(b'terminal-executed',start)
                self.assertEqual((work/'output.py').read_text(),'value = 1\n')
                self.assertNotIn(b'terminal-fixture',captured)
                self.assertEqual(len(server.requests),5)
                send('/quit')
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

    def test_default_entry(self):
        self.scenario(False)

    def test_explicit_session(self):
        self.scenario(True)
