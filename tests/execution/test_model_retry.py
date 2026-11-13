"""Transport retry counts actual attempts and respects caller cancellation/deadlines."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from homebrew_harness.execution.model_gateway import (
    ModelGateway, GatewayLimits, Deadline, ModelCancelled, ModelDeadlineExceeded,
)
from homebrew_harness.execution.model_protocol import ProviderError
from homebrew_harness.execution.model_retry import request, arequest, RetryBoundary, retry_policy
from tests.support.clients import reply


class ModelRetries(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.create=Mock(side_effect=[ProviderError('server_error',status_code=503),reply('Done')])
        self.client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.gateway=ModelGateway(self.client,workdir=self.root)
        self.addCleanup(lambda:(self.gateway.close(),self.gateway.wait_idle(2)))
        self.params={'model':'fixture','messages':[{'role':'user','content':'Answer'}],'max_tokens':32}

    def test_sync_and_async_count_only_one_successful_response(self):
        attempts=[]
        with self.gateway.scope(deadline=Deadline.after(2)):
            result=request(self.gateway,on_attempt=attempts.append,**self.params)
        self.assertEqual(result.text,'Done');self.assertEqual(attempts,[0,1])
        self.create.side_effect=[ProviderError('transport'),reply('Async done')]
        attempts=[]
        async def run():
            with self.gateway.scope(deadline=Deadline.after(2)):
                return await arequest(self.gateway,on_attempt=attempts.append,**self.params)
        self.assertEqual(asyncio.run(run()).text,'Async done')
        self.assertEqual(attempts,[0,1]);self.assertEqual(self.create.call_count,4)

    def test_cancel_after_failure_does_not_retry_old_input(self):
        cancelled=threading.Event()
        def model(**kwargs):
            cancelled.set();raise ProviderError('transport')
        self.create.side_effect=model
        with self.gateway.scope(deadline=Deadline.after(2),cancel_event=cancelled):
            with self.assertRaises(ModelCancelled):request(self.gateway,**self.params)
        self.assertEqual(self.create.call_count,1)

    def test_explicit_context_overflow_returns_to_the_context_controller(self):
        self.create.side_effect=ProviderError('context_overflow',status_code=400)
        with self.assertRaises(ProviderError):
            request(self.gateway,**self.params)
        self.assertEqual(self.create.call_count,1)

    def test_caller_attempt_budget_prevents_an_extra_request(self):
        def budget(attempt):
            if attempt:raise RetryBoundary('No call budget remains')
        with self.gateway.scope(deadline=Deadline.after(2)):
            with self.assertRaises(RetryBoundary):request(self.gateway,on_attempt=budget,**self.params)
        self.assertEqual(self.create.call_count,1)

    def test_exhausted_attempts_do_not_disable_next_request(self):
        self.create.side_effect=[ProviderError('server_error',status_code=503)]*3+[reply('Recovered')]
        with self.gateway.scope(deadline=Deadline.after(2)):
            with self.assertRaises(ProviderError):request(self.gateway,**self.params)
            self.assertEqual(request(self.gateway,**self.params).text,'Recovered')
        self.assertEqual(self.create.call_count,4);self.assertFalse(self.gateway.blocked)

    def test_retry_without_a_caller_deadline_supports_sync_and_async(self):
        self.assertEqual(request(self.gateway,**self.params).text,'Done')
        self.create.side_effect=[ProviderError('transport'),reply('Async done')]
        self.assertEqual(asyncio.run(arequest(self.gateway,**self.params)).text,'Async done')
        self.assertEqual(self.create.call_count,4)

    def test_nested_retry_policy_does_not_multiply_attempts(self):
        with retry_policy(1):
            with self.assertRaises(ProviderError):
                request(self.gateway,**self.params)
        self.assertEqual(self.create.call_count,1)
        self.assertEqual(request(self.gateway,**self.params).text,'Done')

    def test_session_loop_inherits_the_outer_run_retry_policy(self):
        from homebrew_harness import HarnessSession, Capabilities
        session=HarnessSession(self.gateway,'fixture',self.root,
            capabilities=Capabilities(),shutdown_timeout=0.05)
        self.addCleanup(lambda:session.wait_closed(3))
        self.addCleanup(session.close)
        with retry_policy(1):
            with self.assertRaises(ProviderError):
                session.run_user('Answer this question')
        self.assertEqual(self.create.call_count,1)

    def test_request_timeout_waits_for_actual_receipt_before_retry(self):
        released,finished=threading.Event(),threading.Event()
        self.addCleanup(released.set)
        timer=threading.Timer(0.12,released.set)
        self.addCleanup(timer.cancel)
        gateway=ModelGateway(self.client,GatewayLimits(request_timeout=0.04),workdir=self.root)
        self.addCleanup(lambda:(gateway.close(),gateway.wait_idle(2)))
        def model(**kwargs):
            if self.create.call_count==1:
                released.wait(1)
                finished.set()
                return reply('Late answer')
            self.assertTrue(finished.is_set())
            return reply('Current answer')
        self.create.side_effect=model
        timer.start()
        with gateway.scope(deadline=Deadline.after(2)):
            self.assertEqual(request(gateway,**self.params).text,'Current answer')
        self.assertEqual(self.create.call_count,2)
        self.assertFalse(gateway._state.inflight)

    def test_caller_deadline_does_not_start_another_unfinished_request(self):
        released=threading.Event()
        self.addCleanup(released.set)
        gateway=ModelGateway(self.client,GatewayLimits(request_timeout=0.02),workdir=self.root)
        self.addCleanup(lambda:(gateway.close(),gateway.wait_idle(2)))
        def model(**kwargs):
            released.wait(1)
            return reply('Late answer')
        self.create.side_effect=model
        try:
            with gateway.scope(deadline=Deadline.after(0.08)):
                with self.assertRaises(ModelDeadlineExceeded) as raised:
                    request(gateway,**self.params)
            self.assertEqual(raised.exception.source,'caller')
            self.assertEqual(self.create.call_count,1)
            self.assertTrue(gateway._state.inflight)
        finally:
            released.set()

    def test_no_caller_deadline_returns_timeout_with_unfinished_receipt(self):
        released=threading.Event()
        self.addCleanup(released.set)
        gateway=ModelGateway(self.client,GatewayLimits(request_timeout=0.02),workdir=self.root)
        self.addCleanup(lambda:(gateway.close(),gateway.wait_idle(2)))
        def model(**kwargs):
            released.wait(1)
            return reply('Late answer')
        self.create.side_effect=model
        try:
            with self.assertRaises(ModelDeadlineExceeded) as raised:
                request(gateway,**self.params)
            self.assertEqual(raised.exception.source,'request')
            self.assertEqual(self.create.call_count,1)
            self.assertTrue(gateway._state.inflight)
        finally:
            released.set()
