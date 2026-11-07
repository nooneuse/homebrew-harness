"""Short histories retain facts; pressured histories preserve protocol and pointers."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from homebrew_harness.context.compaction import ContextCompactor
from tests.context.test_compaction import batch
from tests.support.clients import fake_client


class PressureCompactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.client, _ = fake_client([])
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_fifty_short_messages_do_not_drop_middle_fact(self):
        c = ContextCompactor(self.client, "fixture", self.root)
        history = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "unique-middle-fact" if i == 20 else "short",
            }
            for i in range(50)
        ]
        prepared = c.prepare(history, "Continue")
        self.assertTrue(any("unique-middle-fact" in m["content"] for m in prepared))
        self.assertEqual(len(prepared), 51)
        self.client.chat.completions.create.assert_not_called()
        self.assertFalse((self.root / ".harness/transcripts").exists())

    def test_pressure_snip_keeps_whole_tool_batches_and_index(self):
        c = ContextCompactor(
            self.client,
            "fixture",
            self.root,
            context_chars=10000,
            trigger_messages=20,
            target_messages=10,
        )
        history = [{"role": "user", "content": "Continue"}]
        for i in range(15):
            history.extend(batch(str(i), ["x" * 800]))
        prepared = c.snip_compact(history, "Continue")
        c.groups(prepared)
        marker = next(
            m for m in prepared if m["content"] and "Exact tool calls" in m["content"]
        )
        self.assertIn("denials index:", marker["content"])
        self.assertLess(len(prepared), len(history))
        self.client.chat.completions.create.assert_not_called()

    def test_tools_contribute_to_pressure_without_changing_permissions(self):
        c = ContextCompactor(
            self.client,
            "fixture",
            self.root,
            context_chars=10000,
            trigger_messages=20,
            target_messages=10,
        )
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "message"}
            for i in range(30)
        ]
        self.assertEqual(len(c.snip_compact(history, "Continue")), 31)
        tools = [{"description": "x" * 8000}]
        self.assertLess(
            len(c.snip_compact(history, "Continue", tools=tools)), len(history)
        )
