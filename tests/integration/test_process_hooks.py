import json
import tempfile
import time
import unittest
from pathlib import Path

from homebrew_harness.runtime.hooks import HookRegistry, ToolEvent, create_hooks
from homebrew_harness.execution.operations import Operations
from homebrew_harness.execution.hook_process import ProcessHook
from homebrew_harness.tools.dispatch import execute_tool
from homebrew_harness.execution.model_protocol import ToolInvocation
from homebrew_harness.tools.registry import TOOL_HANDLERS
from tests.support.process_hooks import slow_hook, inspect_hook


class ProcessHooks(unittest.TestCase):
    def test_process_hook_round_trip_preserves_untrusted_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            hook=ProcessHook(inspect_hook)
            self.assertIsNone(hook(ToolEvent('read_file',{'data':{'kind':'tool_event','value':7}},Path(directory),'test')))

    def test_long_hook_is_observable_and_cancelled_before_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'source.txt').write_text('source')
            hooks=create_hooks();hooks.register_hook('PreToolUse',slow_hook,execution='process')
            manager=Operations(root,'hook:owner')
            try:
                result=json.loads(execute_tool(ToolInvocation('read','invoke_tool',json.dumps({'tool':'read_file','arguments':{'path':'source.txt'},'execution':{'wait_seconds':0}})),root,hooks=hooks,handlers={**TOOL_HANDLERS,'invoke_tool':lambda **kw:None},operations=manager))
                identity=result['execution_id']
                deadline=time.monotonic()+5
                while not (root/'hook-started').exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue((root/'hook-started').exists())
                manager.stop(identity)
                self.assertTrue(manager.handle(identity).wait(3,until='settled').condition_met)
                self.assertEqual(manager.get(identity)['state'],'cancelled')
                self.assertNotIn('source',str(manager.get(identity)['result']))
            finally:
                manager.close();self.assertTrue(manager.wait_closed(3))

    def test_closure_requires_explicit_cooperative_or_process_adapter(self):
        with self.assertRaisesRegex(ValueError,'module-level'):
            ProcessHook(lambda *args: None)
