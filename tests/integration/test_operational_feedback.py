"""Actionable failures survive execution, persistence and public projections."""
import asyncio
import errno
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.execution.model_registry import load_model_registry
from homebrew_harness.platforms.http import JsonTransport, RemoteFailure
from homebrew_harness.platforms.effects import EffectFailure
from homebrew_harness.runtime.errors import boundary_error, error_info
from homebrew_harness.runtime.hooks import HookRegistry, HookError
from homebrew_harness.runtime.runtime_control import CloseProgress
from homebrew_harness.security.data_boundary import register_private_values
from homebrew_harness.services.delivery.runtime import Delivery
from homebrew_harness.services.delivery.store import DeliveryStore
from homebrew_harness.services.store import ServiceStore
from tests.services import test_maintenance as maintenance_fixture
from tests.execution.test_test_runner import Fixture as TestFixture


class DiagnosticTests(unittest.TestCase):
    def test_configuration_explains_version_and_json_location_without_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); workspace = root / "workspace"; workspace.mkdir()
            config = root / "models.json"
            for body, wanted in [('{"version": 99}', 'version 2'), ('{\n invalid', 'Invalid model configuration JSON')]:
                config.write_text(body)
                factory = Mock()
                with self.assertRaises(ValueError) as raised:
                    load_model_registry(config, workspace, client_factory=factory, environment={})
                info = error_info(raised.exception)
                self.assertIn(wanted, info['message'])
                if body.startswith('{\n'):
                    self.assertEqual(info['details']['line'], 2)
                factory.assert_not_called()
                self.assertNotIn(str(root), json.dumps(info))

    def test_private_extensions_stay_opaque_and_os_error_keeps_errno(self):
        private = 'synthetic-private-diagnostic'
        register_private_values(private)
        self.assertNotIn(private, json.dumps(error_info(RuntimeError(private))))
        known = boundary_error(ValueError, 'invalid_fixture', 'Bad field: ' + private, field='profile')
        self.assertIn('Bad field', error_info(known)['message'])
        self.assertNotIn(private, json.dumps(error_info(known)))
        info = error_info(OSError(errno.ENOSPC, 'custom ' + private, private))
        self.assertEqual(info['details']['errno'], errno.ENOSPC)
        self.assertIn('space', info['message'])
        self.assertNotIn(private, json.dumps(info))

    def test_cleanup_repeats_are_bounded_and_recovery_does_not_release_other_steps(self):
        progress = CloseProgress()
        for _ in range(80):
            progress.step('checkpoint', Mock(side_effect=OSError(errno.ENOSPC, 'full')))
        status = progress.snapshot()
        self.assertEqual(len(status['history']), 1)
        self.assertEqual(status['errors'][0]['occurrences'], 80)
        self.assertEqual(status['errors'][0]['error_info']['details']['errno'], errno.ENOSPC)
        progress.step('transport', Mock(side_effect=RuntimeError('opaque')))
        progress.step('checkpoint', lambda: None)
        self.assertEqual([r['component'] for r in progress.snapshot()['errors']], ['transport'])
        self.assertNotIn('transport', progress.snapshot()['completed_steps'])

    def test_fault_coalescing_ignores_request_ids_but_keeps_status_changes(self):
        from homebrew_harness.runtime.errors import failure_record
        first = failure_record(RemoteFailure(403, {'x-request-id': 'first'}))
        second = failure_record(RemoteFailure(403, {'x-request-id': 'second'}), first)
        self.assertEqual(second['occurrences'], 2)
        self.assertEqual(second['error_info']['details']['platform_request_id'], 'second')
        changed = failure_record(RemoteFailure(429, {'x-request-id': 'third'}), second)
        self.assertEqual(changed['occurrences'], 1)

    def test_hook_warning_is_coalesced_and_control_error_keeps_cause(self):
        registry = HookRegistry()
        failure = boundary_error(ValueError, 'fixture_hook', 'Output destination unavailable.')
        callback = Mock(side_effect=failure)
        registry.register_hook('PostToolUse', callback)
        output = io.StringIO()
        with redirect_stderr(output):
            registry.trigger_hooks('PostToolUse'); registry.trigger_hooks('PostToolUse')
        self.assertEqual(output.getvalue().count('[hook warning]'), 1)
        self.assertEqual(registry.snapshot()['failures'][0]['occurrences'], 2)
        registry.register_hook('PostToolUse', Mock(side_effect=failure))
        with redirect_stderr(output):
            registry.trigger_hooks('PostToolUse')
        self.assertEqual(output.getvalue().count('[hook warning]'), 2)
        self.assertEqual(len(registry.snapshot()['failures']), 2)
        registry.register_hook('Stop', callback)
        with self.assertRaises(HookError) as raised:
            registry.trigger_hooks('Stop')
        self.assertIn('Output destination', error_info(raised.exception)['message'])

    def test_goal_contract_reports_the_field_without_echoing_the_reply(self):
        from homebrew_harness.orchestration.goal_loop import PromptGoalEvaluator, GoalEvaluatorError
        from tests.support.clients import fake_client, reply
        client, _ = fake_client([reply(json.dumps({'ok': 'not-a-boolean', 'impossible': False,
                                                  'reason': 'synthetic', 'evidence_ids': []}))])
        evaluator = PromptGoalEvaluator(client, 'fixture')
        try:
            with self.assertRaises(GoalEvaluatorError) as raised:
                evaluator.evaluate({})
            info = error_info(raised.exception)
            self.assertEqual(info['details']['field'], 'ok')
            self.assertIn('Boolean', info['message'])
            self.assertNotIn('not-a-boolean', info['message'])
        finally:
            evaluator.client.close()

    def test_platform_status_survives_error_body_failure(self):
        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(2)
                yield b'never'
        for content in [b'{"message":"Missing permission","secret":"not-for-diagnostics"}', SlowBody()]:
            transport = JsonTransport('http://127.0.0.1', 'fixture')
            transport.loop.run_until_complete(transport.client.aclose())
            def respond(request):
                options = {'content': content} if isinstance(content, bytes) else {'stream': content}
                return httpx.Response(403, headers={'x-request-id': 'fixture-id', 'retry-after': '30'}, **options)
            transport.client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://127.0.0.1')
            try:
                with self.assertRaises(RemoteFailure) as raised:
                    transport.request('POST', '/fixture', Deadline.after(0.5))
                info = error_info(raised.exception)
                self.assertEqual(info['details']['status_code'], 403)
                self.assertEqual(info['details']['platform_request_id'], 'fixture-id')
                self.assertNotIn('not-for-diagnostics', json.dumps(info))
                if isinstance(content, bytes):
                    self.assertIn('Missing permission', info['message'])
            finally:
                transport.close()


class ServiceFeedbackTests(unittest.TestCase):
    setUp = maintenance_fixture.MaintenanceTests.setUp
    start = maintenance_fixture.MaintenanceTests.start

    def service_store(self):
        service = self.start(job_attempts=1)
        service.pause('all')
        return service, service.runtime.maintenance.store

    def test_preflight_failure_and_rejection_remain_queryable_after_reopen(self):
        service, ms = self.service_store()
        store = DeliveryStore(service.runtime, ms, {})
        action = {'id': 'fixture-action', 'kind': 'publish_branch', 'changeset_id': 'fixture-change',
                  'state': 'queued', 'revision': 0, 'attempts': 0, 'plan': {}, 'generation': service.runtime.header['generation']}
        with ms.db.write() as conn:
            ms.put(conn, 'actions', action)
            ms.put(conn, 'changesets', {'id': 'fixture-change', 'work_id': None, 'candidate': {'commit': 'fixture'}, 'branch': 'fixture'})
        delivery = object.__new__(Delivery); delivery.store = store; delivery.git = Mock()
        delivery.git.verify.side_effect = boundary_error(ValueError, 'candidate_unavailable', 'Candidate checkout is unavailable.')
        with patch.object(store, 'current', return_value=None):
            delivery.execute(action)
        saved = store.get('actions', action['id'])
        self.assertEqual(saved['state'], 'queued')
        self.assertEqual(saved['attempts'], 0)
        self.assertGreater(saved['next_attempt'], time.time())
        self.assertEqual(saved['error_info']['details']['phase'], 'preflight')
        delivery.git.publish.assert_not_called()
        accepted = {**saved, 'state': 'started', 'attempts': 1}
        with ms.db.write() as conn:
            ms.put(conn, 'actions', accepted)
        store.settle_action(accepted, 'failed', error=EffectFailure('rejected', status=403))
        service.stop(); self.assertTrue(service.wait_closed(8))
        record = ServiceStore(self.control).runtime.get('service_actions', action['id'])
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['error_info']['details']['status_code'], 403)
        self.assertEqual(record['error_info']['details']['outcome'], 'rejected')
        self.assertEqual(self.peer.model_requests, [])

    def test_resolving_one_phase_preserves_manual_pause_and_other_faults(self):
        service, _ = self.service_store()
        runtime = service.runtime
        error = boundary_error(ValueError, 'fixture_tick', 'Fixture tick unavailable.')
        runtime.report_failure('service', error, phase='tick')
        runtime.report_failure('delivery', OSError(errno.ENOSPC, 'full'))
        meta = runtime.store.get(service.id)
        runtime.phase_succeeded('tick', meta)
        meta = ServiceStore(self.control).get(service.id)
        self.assertTrue(meta['runtime_failures']['service:tick']['resolved_at'])
        self.assertNotIn('resolved_at', meta['runtime_failures']['delivery:loop'])
        self.assertEqual(meta['state'], 'paused')
        self.assertEqual(meta['scope'], 'all')
        self.assertFalse(service.health()['ready'])
        from homebrew_harness import RemoteService
        with RemoteService.from_root(self.control) as remote:
            faults = remote.snapshot().details['runtime_failures']
            self.assertEqual(faults['delivery:loop']['error_info']['details']['errno'], errno.ENOSPC)
            self.assertTrue(faults['service:tick']['resolved_at'])

    def test_service_loop_resolves_its_actual_failure_after_a_successful_tick(self):
        from tests.runtime.test_interaction import eventually
        service, _ = self.service_store()
        runtime = service.runtime
        attempts = []
        def tick():
            attempts.append(True)
            if len(attempts) == 1:
                raise boundary_error(ValueError, 'fixture_tick', 'Temporary tick failure.')
        with patch.object(runtime, 'tick', side_effect=tick):
            eventually(lambda: runtime.store.get(service.id).get('runtime_failures', {}).get('service:tick', {}).get('resolved_at'), 5)
        meta = runtime.store.get(service.id)
        self.assertEqual(meta['state'], 'paused')
        self.assertEqual(meta['scope'], 'all')
        self.assertIsNone(meta.get('error'))
        self.assertIn('Temporary tick failure', meta['runtime_failures']['service:tick']['error_detail'])
        self.assertEqual(self.peer.model_requests, [])

    def test_waiting_for_operator_excludes_outdated_and_expired_requests(self):
        service, ms = self.service_store()
        with ms.db.write() as conn:
            ms.put(conn, 'decisions', {'id': 'outdated', 'state': 'pending', 'kind': 'discussion_requirements',
                                      'discussion_id': 'unavailable', 'work_id': None, 'revision': 0, 'created_at': time.time()})
        before = ms.get('decisions', 'outdated')
        self.assertFalse(service.requests())
        health = service.health()
        self.assertFalse(health['waiting_for_operator'])
        self.assertEqual(health['decisions']['unavailable'], 1)
        self.assertEqual(health['decisions']['unavailable_examples'][0]['reason'], 'communication_not_configured')
        self.assertEqual(ms.get('decisions', 'outdated'), before)
        work = service.work.submit('Await a host choice', request_id='fixture-work')
        saved = service.runtime.store.work(work.id)
        common = {'kind': 'work_information', 'state': 'pending', 'work_id': work.id,
                  'requirements_revision': saved['requirements_revision'], 'revision': 0,
                  'candidate': {'plan': 'Choose a direction'}, 'created_at': time.time()}
        with ms.db.write() as conn:
            ms.put(conn, 'decisions', {**common, 'id': 'expired', 'expires_at': time.time() - 1})
            ms.put(conn, 'decisions', {**common, 'id': 'current', 'expires_at': time.time() + 60})
        health = service.health()
        self.assertTrue(health['waiting_for_operator'])
        self.assertEqual(health['decisions']['actionable'], 1)
        self.assertEqual(health['decisions']['unavailable'], 2)
        self.assertEqual([r.ref.id for r in service.requests()], ['current'])

    def test_current_job_and_source_errors_have_consistent_safe_details(self):
        service, store = self.service_store()
        receipt = service.converse('Fixture question', request_id='feedback')
        job = store.claim_job()
        self.assertEqual(job['id'], receipt['job_id'])
        store.finish(job, error=boundary_error(ValueError, 'service_result_invalid', 'Expected nonempty text.', field='text'))
        result = service.job(job['id'])
        self.assertIn('nonempty text', result['error_info']['message'])
        self.assertIn('nonempty text', result['error_detail'])
        store.source_error('fixture', {'cursor': {}}, RemoteFailure(403, detail={'message': 'Missing permission'}), 30)
        source = next(s for s in service.health()['sources'] if s['id'].endswith(':fixture'))
        self.assertIn('Missing permission', source['reason'])
        self.assertEqual(source['diagnostic_ref']['record'], 'sources')
        self.assertEqual(self.peer.model_requests, [])


class TestRunnerFeedback(unittest.TestCase):
    setUp = TestFixture.setUp
    runner = TestFixture.runner
    invoke = TestFixture.invoke

    def test_backend_startup_failure_is_not_a_test_assertion_failure(self):
        runner = self.runner()
        with patch.object(runner.backend, 'launch', side_effect=boundary_error(ValueError, 'execution_backend', 'Required backend image is unavailable.')):
            receipt = self.invoke(runner)
        self.assertEqual(receipt['status'], 'error')
        self.assertIsNone(receipt['exit_code'])
        self.assertIn('backend image', receipt['error'])
        self.assertEqual(receipt['error_info']['details']['phase'], 'launch')
        self.assertFalse(receipt['isolation']['started'])
