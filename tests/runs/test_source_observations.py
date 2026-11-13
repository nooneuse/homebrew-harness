"""Source outages inform existing work; only an explicit wait pauses it."""

import time

from tests.runs import test_human_control as support
from tests.support.clients import reply, call
from homebrew_harness.runs.services import RunServices
from homebrew_harness.runs.source_observation import ready


class SourceObservations(support.ManagedRunFixture):
    def enable(self, control, session):
        control.state=self.store.update(control.header,{'source_enabled':True})
        self.store.runtime.put('run_sources','source',{'id':'source','run_id':control.id,
            'checkpoint':session.checkpoint_key,'failures':1,'last_error_status':401})

    def test_collection_error_does_not_block_already_authorized_local_work(self):
        control,session,client,payloads=self.session([
            reply(calls=[call('write',name='write_file',arguments='{"path":"result.txt","content":"done"}')]),
            reply('Completed local work')])
        self.enable(control,session)
        outcome=control.execute()
        self.assertEqual(outcome.status,'completed')
        self.assertEqual((self.root/'result.txt').read_text(),'done')
        self.assertIn('source_observations',str(session.messages))
        self.assertIn('unavailable',str(session.messages))

    def test_explicit_source_wait_is_durable_and_requires_a_fresh_observation(self):
        control,session,client,_=self.session([
            reply(calls=[call('wait',name='wait_for_source',arguments='{"until":"healthy","reason":"Need a fresh source observation"}')])])
        self.enable(control,session)
        outcome=control.execute()
        self.assertEqual(outcome.status,'waiting')
        self.assertEqual(outcome.reason_code,'source_wait')
        self.assertEqual(client.chat.completions.create.call_count,1)
        run=self.store.get(control.id)
        with self.store.runtime.backend().connection() as conn:
            self.assertFalse(ready(self.store.runtime.backend(),conn,run))
        self.store.runtime.put('run_sources','source',{'id':'source','run_id':control.id,
            'checkpoint':session.checkpoint_key,'failures':0,'revision':1,'last_success':time.time()+1})
        with self.store.runtime.backend().connection() as conn:
            self.assertTrue(ready(self.store.runtime.backend(),conn,run))

    def test_later_tools_are_not_executed_after_source_wait(self):
        control,session,_,_=self.session([reply(calls=[
            call('wait',name='wait_for_source',arguments='{}'),
            call('write',name='write_file',arguments='{"path":"later.txt","content":"not run"}')])])
        self.enable(control,session)
        self.assertEqual(control.execute().reason_code,'source_wait')
        self.assertFalse((self.root/'later.txt').exists())

    def waiting(self, until):
        control,session,_,_=self.session([reply(calls=[call('wait',
            name='wait_for_source',arguments='{"until":"'+until+'"}')])])
        self.enable(control,session)
        self.assertEqual(control.execute().status,'waiting')
        self.close(control,session)
        services=RunServices(self.store)
        self.addCleanup(services.close)
        services.tick(control.id)
        self.assertEqual(self.store.get(control.id)['status'],'waiting')
        return control,session,services

    def test_fresh_healthy_observation_queues_waiting_run_and_clears_wait(self):
        control,session,services=self.waiting('healthy')
        record=self.store.runtime.items('run_sources')['source']
        self.store.runtime.put('run_sources','source',
            {**record,'failures':0,'revision':record.get('revision',0)+1})
        services.tick(control.id)
        run=self.store.get(control.id)
        self.assertEqual(run['status'],'queued')
        self.assertIsNone(run['source_wait'])
        self.assertTrue(all(e['state']=='delivered'
            for e in self.store.runtime.items('run_outbox').values()))

    def test_reference_event_wakes_once_and_keeps_evidence_unread(self):
        control,session,services=self.waiting('event')
        self.store.runtime.mailbox('send','reference_'+control.id,
            records=[{'id':'new-reference','type':'run_reference',
                      'text':'new external observation'}])
        services.tick(control.id)
        self.assertEqual(self.store.get(control.id)['status'],'queued')
        self.assertIsNone(self.store.get(control.id)['source_wait'])
        before=self.store.runtime.items('run_outbox')
        services.tick(control.id)
        self.assertEqual(before,self.store.runtime.items('run_outbox'))
        self.assertEqual(len(self.store.runtime.mailbox('inspect','reference_'+control.id)),1)

    def test_user_input_wakes_even_when_source_remains_unavailable(self):
        control,session,services=self.waiting('healthy')
        self.store.submit(control.id,'Continue using local evidence')
        services.tick(control.id)
        self.assertEqual(self.store.get(control.id)['status'],'queued')
        self.assertIsNone(self.store.get(control.id)['source_wait'])
        self.assertEqual(self.store.runtime.items('run_sources')['source']['failures'],1)
