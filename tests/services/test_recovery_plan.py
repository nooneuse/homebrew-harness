import json
import unittest
from homebrew_harness.services.job_contracts import JobContract
from tests.services import test_maintenance as fixture
from tests.runtime.test_interaction import eventually


class RecoveryPlan(unittest.TestCase):
    setUp=fixture.MaintenanceTests.setUp
    start=fixture.MaintenanceTests.start

    def test_native_plan_keeps_same_paused_work_and_never_launches_a_run(self):
        service=self.start(auto=False);service.pause('all')
        work=service.work.submit('Preserve the existing feature.')
        work.pause()
        before=service.store.work(work.id)
        result=work.plan_recovery('Develop on the recorded branch; defer target integration.',revision=before['revision'],request_id='plan-one')
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(work.plan_recovery('Develop on the recorded branch; defer target integration.',revision=before['revision'],request_id='plan-one'),result)
        service.resume()
        row=eventually(lambda: service.runtime.maintenance.store.job_get(result['job_id']) if service.runtime.maintenance.store.job_get(result['job_id'])['state']=='completed' else None,20)
        current=service.store.work(work.id)
        self.assertTrue(current['pause_requested']);self.assertEqual(current['run_id'],before['run_id'])
        self.assertEqual(current['requirements_revision'],before['requirements_revision'])
        self.assertFalse(current['recovery_plan']['execution_authorized'])
        self.assertIn('text',row['result'])
        request=next(r for r in self.peer.model_requests if 'recovery_plan' in json.dumps(r))
        self.assertIn('recorded branch',json.dumps(request))
        self.assertNotIn('write_file',{t['function']['name'] for t in request['tools']})

    def test_recovery_job_cannot_read_another_works_evidence(self):
        service=self.start(auto=False);service.pause('all')
        work=service.work.submit('Preserve existing work.');work.pause()
        other=service.work.submit('Unrelated work.')
        result=work.plan_recovery(request_id='one')
        job=service.runtime.maintenance.store.job_get(result['job_id'])
        contract=JobContract(service.runtime.maintenance.store,job)
        contract.authorize('work',work.id)
        with self.assertRaises(ValueError):contract.authorize('work',other.id)
