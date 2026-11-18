"""Real Docker input, selected tests and parent cancellation, explicitly enabled."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.execution.operations import Operations
from homebrew_harness.execution.bash_policy import BashPolicy
from homebrew_harness.execution.bash_runner import ContainerShellRunner
from homebrew_harness.execution.execution_backend import ContainerBackend, ContainerPolicy
from homebrew_harness.execution.test_runner import TestRecipe, TestRunner
from homebrew_harness.execution.test_selection import LAUNCHER
from homebrew_harness.tools.shell import run_bash
from homebrew_harness.security.permissions import PermissionDecision

IMAGE=os.getenv('HARNESS_DOCKER_IMAGE','')
CLI=os.getenv('HARNESS_DOCKER_CLI','/usr/bin/docker')


@unittest.skipUnless(IMAGE and sys.platform=='linux','requires an explicit image and Linux Docker controller')
class InteractiveDocker(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='harness-interactive-docker-')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.manager=Operations(self.root,'container:owner')
        self.addCleanup(lambda:self.manager.wait_closed(5))
        self.addCleanup(self.manager.close)

    def test_container_bash_receives_input_and_confirms_cleanup(self):
        runner=ContainerShellRunner(BashPolicy(mode='container',cli=CLI,image=IMAGE))
        self.addCleanup(lambda:runner.wait_closed(5))
        self.addCleanup(runner.close)
        handle=self.manager.start('bash',{},lambda:run_bash('read -r answer; printf "answer=%s" "$answer"',self.root,runner=runner))
        deadline=time.monotonic()+5
        while not self.manager.get(handle.id)['capabilities']['can_send_input'] and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertTrue(self.manager.get(handle.id)['capabilities']['can_send_input'])
        with patch('homebrew_harness.security.permissions.check_permission',return_value=PermissionDecision('allow','fixture')):
            self.manager.input(handle.id,'accepted\n','input-one')
        self.assertTrue(handle.wait(5,until='settled').condition_met, self.manager.get(handle.id))
        result=handle.result()
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual(result['stdout'],'answer=accepted')
        self.assertTrue(result['execution']['isolation']['cleanup_complete'])

    def test_container_fixed_launcher_supports_selected_and_full_tests(self):
        tests=self.root/'tests';tests.mkdir();(tests/'__init__.py').write_text('')
        (tests/'test_example.py').write_text('import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): pass\n def test_bad(self): self.fail("full suite fixture")\n')
        argv=(sys.executable,'-I','-c',LAUNCHER)
        backend=ContainerBackend(ContainerPolicy(CLI,IMAGE,argv,argv))
        runner=TestRunner(self.root,[TestRecipe('unit',argv,selector='unittest')],backend=backend)
        self.addCleanup(lambda:runner.wait_closed(5));self.addCleanup(runner.close)
        runner.approve('unit')
        selected=runner.run('unit',selection={'targets':['tests.test_example.Sample.test_ok']})
        self.assertEqual(selected['status'],'passed',selected)
        self.assertEqual(selected['test_scope'],'selected')
        self.assertFalse(runner.verify('unit',self.root)['ok'])
        full=runner.run('unit')
        self.assertEqual(full['status'],'failed',full)
        self.assertTrue(full['isolation']['cleanup_complete'])
