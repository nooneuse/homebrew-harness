"""A real MCP transport keeps peer calls alive when one execution is cancelled."""

import json
import threading
import unittest

from homebrew_harness.execution.operations import Operations
from homebrew_harness.tools.mcp.runtime import MCPRuntime, ServerSpec
from tests.tools.mcp.test_runtime import MCPTransportTests


class InteractiveMCP(unittest.TestCase):
    setUpClass = classmethod(MCPTransportTests.setUpClass.__func__)
    setUp = MCPTransportTests.setUp
    records = MCPTransportTests.records

    def test_cancel_one_long_call_keeps_same_connection_usable(self):
        spec=ServerSpec('shared','streamable_http',url=self.http_url,
                        headers={'Authorization':'Bearer fixture-auth-value'})
        runtime=MCPRuntime({'shared':spec},workdir=self.root)
        self.addCleanup(runtime.close)
        runtime.connect('shared')
        bindings=runtime.snapshot()[1]
        delay=bindings['mcp__shared__delay']
        version=bindings['mcp__shared__get_version']
        connection=delay.connection
        manager=Operations(self.root,'mcp:owner')
        self.addCleanup(manager.close)
        handle=manager.start('mcp__shared__delay',{'seconds':2},lambda:runtime.call(delay,{'seconds':2}),read_only=True)
        self.assertFalse(handle.wait(.05,until='settled').condition_met)
        before=json.loads(runtime.call(version,{}))
        self.assertFalse(before['isError'])
        handle.stop()
        self.assertTrue(handle.wait(3,until='settled').condition_met)
        after=json.loads(runtime.call(version,{}))
        self.assertFalse(after['isError'])
        self.assertIs(runtime.snapshot()[1]['mcp__shared__get_version'].connection,connection)
        self.assertFalse(connection.stopping)
        self.assertEqual(handle.snapshot().state,'cancelled')
