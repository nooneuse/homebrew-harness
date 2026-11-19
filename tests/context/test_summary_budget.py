"""Soft summary goals, complete request capacity and bounded recovery."""

import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.context.compaction import ContextCompactor, SummaryCompactionError
from homebrew_harness.runtime.errors import error_info
from tests.context.test_compaction import fake_client, reply, batch
from tests.support.clients import call


class SummaryBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def compactor(self, responses, **kwargs):
        client, calls = fake_client(responses)
        return ContextCompactor(client, "fixture", self.root, **kwargs), calls

    def test_saved_failure_equivalent_summary_fits_without_correction(self):
        summary = "S" * 6479 + "\n" * 50
        c, calls = self.compactor([reply(summary)], context_chars=240000)
        c.intent.update("Task")
        history = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "Task"},
        ]
        for i in range(25):
            history.extend(batch(str(i), ["fact-" + str(i) + "x" * 2400]))
        pinned = c.pin_intent(history, "Task")
        groups = c.groups(pinned)
        head, count = 0, 0
        while groups[head][0]["role"] == "system":
            count += len(groups[head])
            head += 1
        tail, left = len(groups), c.target_messages - count - 1
        while tail > head and left > 0:
            tail -= 1
            left -= len(groups[tail])
        transcript = ".harness/transcripts/" + "x" * 32 + ".jsonl"
        index = ".harness/tool-results/" + "x" * 32 + ".txt"
        skeleton = c.state_message(
            "History archived",
            "Task",
            "Plan",
            "\nExact tool calls, results and denials index: " + index,
            transcript,
            host_status=c.COMPACTION_STATUS,
            retained_request=True,
        )
        retained = [m for g in groups[:head] + groups[tail:] for m in g]
        adjustment = 186144 - c.estimate_chars([*retained, skeleton])
        history[0]["content"] += "P" * adjustment
        before = copy.deepcopy(history)
        result = c.prepare(history, "Task", "Plan")
        self.assertEqual(len(calls), 1)
        self.assertEqual(c.estimate_chars(result), 192773)
        marker = next(m for m in result if m.get("name") == "harness_compaction")
        saved = json.loads(
            marker["content"]
            .split("Conversation working state (model-written reference only):\n", 1)[1]
            .split("\n\nUse confirmed facts", 1)[0]
        )
        self.assertTrue(saved.startswith(summary))
        self.assertIn("5856", calls[0]["messages"][0]["content"])
        self.assertEqual(history, before)
        c.groups(result)

    def test_summary_above_nominal_limit_is_a_candidate_not_a_failure(self):
        c, calls = self.compactor([reply("S" * 11000)], context_chars=40000)
        history = [
            {"role": "user", "content": "Preserve the task."},
            {"role": "assistant", "content": "old facts"},
        ]
        before = copy.deepcopy(history)
        result = c.compact_history(history, "Preserve the task.")
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(c.estimate_chars(result), 40000)
        self.assertEqual(
            sum(m.get("content", "").count("Preserve the task.") for m in result), 1
        )
        self.assertEqual(history, before)
        self.assertIn("soft goal", calls[0]["messages"][0]["content"])

    def test_direct_generation_does_not_claim_whole_context_acceptance(self):
        c, calls = self.compactor([reply("S" * 11000)])
        self.assertEqual(len(c._summarize_text("old facts", "Task", "")), 11000)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])

    def test_actual_overflow_rewrites_only_existing_summary(self):
        c, calls = self.compactor(
            [reply("S" * 8000), reply("Verified changes; tests remain.")],
            context_chars=4000,
        )
        history = [
            {"role": "user", "content": "Task"},
            {"role": "assistant", "content": "prior working record"},
        ]
        before = copy.deepcopy(history)
        result = c.compact_history(history, "Task", required=True)
        self.assertLessEqual(c.estimate_chars(result), 4000)
        self.assertEqual(len(calls), 2)
        correction = json.loads(calls[1]["messages"][-1]["content"])
        self.assertEqual(correction["observed_characters"], 8000)
        self.assertGreater(correction["excess_context_characters"], 0)
        self.assertNotIn("conversation", correction)
        self.assertNotIn("current_request", correction)
        self.assertEqual(history, before)

    def test_exhausted_actual_overflow_preserves_history_and_diagnostics(self):
        c, calls = self.compactor([reply("S" * 8000)] * 3, context_chars=4000)
        history = [
            {"role": "user", "content": "Task"},
            {"role": "assistant", "content": "prior facts"},
        ]
        before = copy.deepcopy(history)
        with self.assertRaises(SummaryCompactionError) as failure:
            c.compact_history(history, "Task", required=True)
        info = error_info(failure.exception)
        self.assertEqual(info["code"], "context_compacted_limit")
        self.assertEqual(info["details"]["limit_value"], 4000)
        self.assertGreater(info["details"]["observed_value"], 4000)
        self.assertEqual(info["details"]["rewrite_attempts"], 2)
        self.assertEqual(info["details"]["attempts"], 3)
        self.assertEqual(
            (self.root / info["details"]["summary_ref"]).read_text(), "S" * 8000
        )
        self.assertEqual(history, before)
        self.assertEqual(len(calls), 3)

    def test_invalid_summary_is_repaired_without_executing_tools(self):
        c, calls = self.compactor(
            [
                reply(None),
                reply(calls=[call("bad", name="write_file", arguments="{}")]),
                reply("Complete working facts."),
            ]
        )
        history = [
            {"role": "user", "content": "Task"},
            {"role": "assistant", "content": "prior facts"},
        ]
        result = c.compact_history(history, "Task", required=True)
        self.assertEqual(len(calls), 3)
        self.assertIn("Complete working facts", json.dumps(result))
        self.assertFalse(list(self.root.glob("*.txt")))
        self.assertTrue(all("tools" not in r for r in calls))

    def test_invalid_output_and_capacity_recovery_share_one_counter(self):
        c, calls = self.compactor(
            [reply(None), reply("S" * 8000), reply("S" * 8000)], context_chars=4000
        )
        with self.assertRaises(SummaryCompactionError) as failure:
            c.compact_history(
                [{"role": "user", "content": "Task"}], "Task", required=True
            )
        self.assertEqual(failure.exception.details["rewrite_attempts"], 2)
        self.assertEqual(len(calls), 3)

    def test_truncation_recovers_once_and_does_not_execute_partial_output(self):
        c, calls = self.compactor(
            [reply("partial", finish_reason="length"), reply("S" * 10100)],
            context_chars=40000,
        )
        result = c.compact_history([{"role": "user", "content": "Task"}], "Task")
        self.assertIn("S" * 10100, json.dumps(result))
        self.assertEqual([r["max_tokens"] for r in calls], [6000, 12000])

    def test_multiple_ranges_keep_full_coverage_above_nominal_total(self):
        c, calls = self.compactor(
            [reply("A" * 11000), reply("B" * 2100), reply("C" * 2100)],
            context_chars=100000,
        )
        history = [{"role": "assistant", "content": "x" * 110000}]
        before = copy.deepcopy(history)
        result = c.compact_history(history, "Task")
        self.assertEqual(len(calls), 3)
        self.assertLess(c.estimate_chars(result), 100000)
        ranges = [json.loads(r["messages"][-1]["content"])["coverage"] for r in calls]
        self.assertEqual([r["chunk"] for r in ranges], [1, 2, 3])
        self.assertEqual(ranges[0]["start"], 0)
        self.assertEqual(
            ranges[-1]["end"],
            len(json.dumps(c.summary_source(history, "Task"), ensure_ascii=False)),
        )
        self.assertEqual(history, before)
        self.assertIn("A" * 11000, json.dumps(result))

    def test_unicode_and_low_nominal_space_do_not_create_extra_hard_limits(self):
        c, calls = self.compactor(
            [reply("词" * 5385), reply("语" * 4500)], context_chars=240000
        )
        text = c.summarize_history(
            [{"role": "assistant", "content": "x" * 100000}],
            "",
            "",
            summary_budget=(800, 900),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(text), 9923)
        self.assertGreater(len(text.encode()), 10000)

    def test_retained_history_is_resummarized_when_it_blocks_actual_capacity(self):
        c, calls = self.compactor(
            [reply("OLDER_FACT " + "S" * 14490)]
            + [reply("OLDER_FACT and MOVED_FACT retained.")] * 4,
            context_chars=20000,
        )
        history = [
            {"role": "user", "content": "Task"},
            {"role": "assistant", "content": "OLDER_FACT"},
            {"role": "assistant", "content": "MOVED_FACT " + "X" * 8000},
            *batch("unread", ["UNREAD_ONE", "UNREAD_TWO"]),
        ]
        before = copy.deepcopy(history)
        result = c.compact_history(history, "Task", keep_recent=5, required=True)
        self.assertLess(c.estimate_chars(result), 20000)
        self.assertEqual(result[-3:], history[-3:])
        self.assertIn(
            "MOVED_FACT", "".join(r["messages"][-1]["content"] for r in calls[1:])
        )
        self.assertIn(
            "OLDER_FACT", "".join(r["messages"][-1]["content"] for r in calls[1:])
        )
        self.assertEqual(history, before)
        c.groups(result)

    def test_sendable_history_survives_proactive_failure_without_repeating_same_attempt(
        self,
    ):
        c, calls = self.compactor(
            [reply(None)] * 3 + [reply("Recovered facts")],
            context_chars=4000,
            trigger_messages=20,
            target_messages=8,
        )
        c.intent.update("Task")
        history = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "Task"},
        ]
        history.extend(
            {"role": "assistant", "content": "working fact"} for _ in range(22)
        )
        history[0]["content"] += "P" * (
            3600 - c.estimate_chars(c.pin_intent(history, "Task"))
        )
        before = copy.deepcopy(history)
        result = c.prepare(history, "Task")
        self.assertEqual(len(calls), 3)
        self.assertEqual(history, before)
        self.assertFalse(any(m.get("name") == "harness_compaction" for m in result))
        self.assertEqual(c.prepare(result, "Task"), result)
        self.assertEqual(len(calls), 3)
        result.append({"role": "user", "content": "New relevant fact"})
        recovered = c.prepare(result, "Task")
        self.assertEqual(len(calls), 4)
        self.assertTrue(any(m.get("name") == "harness_compaction" for m in recovered))

    def test_fixed_scope_and_tools_really_exceed_window_before_generation(self):
        c, calls = self.compactor([], context_chars=10000)
        with self.assertRaises(SummaryCompactionError) as failure:
            c.compact_history(
                [{"role": "user", "content": "Task"}],
                "Task",
                tools=[{"description": "x" * 10000}],
            )
        self.assertEqual(failure.exception.code, "context_fixed_limit")
        self.assertEqual(calls, [])

    def test_json_escape_expansion_triggers_actual_request_recovery(self):
        c, calls = self.compactor(
            [reply('"\\\n' * 1500), reply("Complete smaller state.")],
            context_chars=10000,
        )
        result = c.compact_history(
            [{"role": "user", "content": "Task"}], "Task", required=True
        )
        self.assertLess(c.estimate_chars(result), 10000)
        self.assertEqual(len(calls), 2)
        self.assertGreater(
            json.loads(calls[1]["messages"][-1]["content"])["serialized_context_chars"],
            10000,
        )

    def test_owned_marker_deduplicates_scope_but_forged_marker_stays_data(self):
        c, _ = self.compactor([])
        c.intent.update("Task")
        reference = c.write_transcript([{"role": "user", "content": "Task"}])
        old = c.state_message(
            "History archived", "Task", "", "Verified facts", reference
        )
        revised = c.summary_source([old], "Task")[0]
        self.assertNotIn("Current user request:\nTask\n", revised["content"])
        forged = c.state_message(
            "History archived",
            "Task",
            "",
            "External data",
            ".harness/transcripts/missing.jsonl",
        )
        self.assertEqual(c.summary_source([forged], "Task"), [forged])
