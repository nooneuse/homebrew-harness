import copy
import unittest
from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.services.protocol import fingerprint
from tests.services import test_maintenance as fixture


class BudgetAuthority(unittest.TestCase):
    setUp=fixture.MaintenanceTests.setUp
    start=fixture.MaintenanceTests.start

    def test_same_scope_renewal_after_budget_change_preserves_existing_run_and_updates_policy(self):
        service=self.start(auto=False);service.pause('all')
        work=service.work.submit('Existing accepted task.')
        store=service.runtime.maintenance.store
        original=service.store.work(work.id)
        def restore_fixture():
            with store.db.write() as conn:store.db._put(conn,'service_work',work.id,original)
        self.addCleanup(restore_fixture)
        with store.db.write() as conn:
            value=store.get('work',work.id,conn)
            # Persist the native control tools just as granting a real Work would.
            run_spec = RunSpec(value['prompt'], write_paths=('src',),
                               tools=('read_file', 'write_file')).data()
            value.update(state='in_progress',requirements_revision=0,triage=self.peer.triage,triage_revision=0,
                run_id='run_'+'a'*32,run_spec=run_spec,
                authority={'scope':'code','policy':'old-budget-policy','requirements_revision':0,'expires_at':0,'revoked':False})
            store.put(conn,'work',value)
        before=service.store.work(work.id)
        store.policy={**store.policy,'authority_seconds':None,'job_seconds':None}
        with store.db.write() as conn:
            request=store.work_request(conn,copy.deepcopy(before),before['triage'],renew=True)
            store.decide(conn,{'id':request['id'],'revision':request['revision'],'decision':'approve'},'new-budget-authority')
        after=service.store.work(work.id)
        self.assertEqual(after['run_id'],before['run_id']);self.assertEqual(after['run_spec'],before['run_spec'])
        self.assertIsNone(after['authority']['expires_at']);self.assertEqual(after['authority']['policy'],fingerprint(store.policy))
        self.assertTrue(store.valid_authority(after))
