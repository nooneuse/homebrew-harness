"""Real terminal sessions verify model commands and blank-chat transitions."""

import json
import os
import select
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.integration.test_native_model_protocols import ProtocolService
from tests.integration.test_model_backends import chat


@unittest.skipIf(os.name == 'nt', 'Requires a POSIX terminal.')
class TerminalCommands(unittest.TestCase):
    def scenario(self, explicit):
        import pty
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();work=root/'work';work.mkdir();env=root/'model.env'
            server=ProtocolService([chat('first-terminal-answer'),chat('second-terminal-answer')])
            env.write_text('OPENAI_API_KEY=terminal-secret\nOPENAI_BASE_URL='+server.url+'\nOPENAI_MODEL=fixture\n');env.chmod(0o600)
            config=root/'host.json';config.write_text(json.dumps({'env_file':str(env),'model_planning':False,'prefer_session':True}))
            argv=['-m','homebrew_harness','session','--env-file',str(env)] if explicit else ['-m','homebrew_harness','--host-config',str(config)]
            pid,fd=pty.fork()
            if pid==0:
                os.chdir(work)
                os.execv(sys.executable,[sys.executable,'-I','-B',*argv])
            captured=b''
            def wait(marker, start=0):
                nonlocal captured
                end=time.monotonic()+20
                while marker not in captured[start:]:
                    if time.monotonic()>end:self.fail('Terminal missed expected output: '+marker.decode())
                    ready,_,_=select.select([fd],[],[],.1)
                    if ready:
                        try:captured+=os.read(fd,65536)
                        except OSError:self.fail('Terminal exited before '+marker.decode())
            def send(text):
                start=len(captured);os.write(fd,text.encode()+b'\n');return start
            try:
                wait(b'agent> ')
                start=send('/help');wait(b'/reasoning [',start)
                start=send('/model other');wait(b'"model": "other"',start)
                start=send('/reasoning high');wait(b'"reasoning": "high"',start)
                start=send('First task');wait(b'first-terminal-answer',start)
                time.sleep(.2)
                start=send('/new')
                wait(b'agent> ',start)
                start=send('Second task');wait(b'second-terminal-answer',start)
                self.assertEqual(server.requests[-1]['model'],'other')
                self.assertEqual(server.requests[-1]['reasoning_effort'],'high')
                self.assertFalse(any('first-terminal-answer' in str(m) for m in server.requests[-1]['messages']))
                self.assertNotIn(b'terminal-secret',captured)
                send('q')
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

    def test_explicit_session_commands(self):
        self.scenario(True)

    def test_default_entry_commands(self):
        self.scenario(False)
