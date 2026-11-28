"""Bootstrap existing issues and advertise the actual discussion read contract."""

import unittest
from types import SimpleNamespace

from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.platforms.github.repository import GithubRepository, stamp
from homebrew_harness.services.job_contracts import JobContract


class IssueBootstrapTests(unittest.TestCase):
    def repository(self):
        calls = []
        row = {"id": 71, "number": 1, "title": "Windows support", "body": "Discuss the phases", "state": "open", "updated_at": "2026-11-13T04:25:45Z", "user": {"id": 7}}
        def request(method, path, deadline, *, params):
            calls.append((path, dict(params)))
            return ([row] if path.endswith('/issues') and 'since' not in params else []), {}
        return GithubRepository({'provider':'github','instance':'primary','project':'example/project'}, SimpleNamespace(request=request)), calls

    def test_bootstrap_can_discover_existing_issue_without_epoch_filter(self):
        repo, calls = self.repository()
        page = repo.poll({}, Deadline.after(5))
        self.assertEqual([event.subject.number for event in page.events], [1])
        self.assertNotIn('since', calls[0][1])
        repo.poll(page.cursor, Deadline.after(5))
        self.assertNotIn('since', calls[1][1])

    def test_incremental_poll_preserves_watermark_overlap(self):
        repo, calls = self.repository()
        repo.poll({'watermark':2000000000}, Deadline.after(5))
        self.assertEqual(calls[0][1]['since'], stamp(2000000000-60))

    def test_discussion_instruction_names_supported_reads_and_ids(self):
        contract = JobContract(None, {'kind':'discussion'})
        instruction = contract.instruction(SimpleNamespace(skills={}))
        self.assertIn("kind='discussions', identity=job.target", instruction)
        self.assertIn("kind='discussion_events', identity=job.target", instruction)
        self.assertIn("kind='events'", instruction)
        self.assertIn('next_offset', instruction)
        self.assertIn('not event IDs', instruction)

    def test_recovery_instruction_without_input(self):
        contract = JobContract(None, {"kind": "recovery_plan"})
        instruction = contract.instruction(SimpleNamespace(skills={}))
        self.assertIn("Propose an evidence-based recovery plan", instruction)
        self.assertNotIn("The operator explicitly requested applying", instruction)

    def test_ci_triage_instruction_without_store(self):
        contract = JobContract(None, {"kind": "ci_triage"})
        instruction = contract.instruction(SimpleNamespace(skills={}))
        self.assertIn("Read fixed candidate code", instruction)
        self.assertNotIn("next_action=assess_integration", instruction)

    def test_own_pr_instruction_preserves_followup_for_each_state(self):
        for pr_state, delivery_state, expected in (
            ("open", "published", "next_step=repair"),
            ("closed", "published", "Do not submit a code review"),
            ("open", "delivered", "Do not submit a code review"),
        ):
            with self.subTest(pr_state=pr_state, delivery_state=delivery_state):
                contract = JobContract(None, {"kind": "discussion", "input": {
                    "own_pr": {"delivery_state": delivery_state},
                    "observed_pr": {"state": pr_state},
                }})
                self.assertIn(expected, contract.instruction(SimpleNamespace(skills={})))
