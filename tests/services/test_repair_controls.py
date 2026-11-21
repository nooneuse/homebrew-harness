"""Explicit repair extensions use the same SDK, CLI and grouped controls."""

import unittest
from unittest.mock import Mock
from homebrew_harness.cli import parser
from homebrew_harness.contracts import SDKError
from homebrew_harness.services.cli import operate
from tests.runtime.test_interaction import eventually
from tests.services import test_maintenance as fixture


class RepairControlSyntax(unittest.TestCase):
    def test_cli_batch_marks_only_explicit_extra_attempt_members(self):
        service = Mock()
        args = parser().parse_args(['service', 'batch', 'service-fixture', 'decide', 'batch-fixture',
            '--approve', 'ci-one', 'ordinary', '--allow-extra-attempt', 'ci-one'])
        operate(service, args)
        self.assertEqual(service.decide_batch.call_args.args[1], [
            {'id': 'ci-one', 'decision': 'approve', 'allow_extra_attempt': True},
            {'id': 'ordinary', 'decision': 'approve'}])

    def test_cli_extra_member_must_also_be_approved(self):
        service = Mock()
        args = parser().parse_args(['service', 'batch', 'service-fixture', 'decide', 'batch-fixture',
            '--deny', 'ci-one', '--allow-extra-attempt', 'ci-one'])
        with self.assertRaises(ValueError):
            operate(service, args)
        service.decide_batch.assert_not_called()


class RepairControlAuthority(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def test_extra_flag_cannot_grant_unrelated_work_authority(self):
        service = self.start(auto=False)
        work = service.work.submit('Review this scoped work.')
        request = eventually(lambda: next(iter(service.requests()), None), 15)
        before = service.store.work(work.id)
        with self.assertRaises(SDKError):
            service.decide(request, 'approve', allow_extra_attempt=True, decision_id='wrong-authority-kind')
        self.assertEqual(service.store.work(work.id), before)
        self.assertEqual(service.requests()[0].ref, request.ref)
