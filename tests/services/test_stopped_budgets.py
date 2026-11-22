import tempfile
import unittest
from pathlib import Path
from homebrew_harness.services.store import ServiceStore
from homebrew_harness.services.spec import ServiceSpec


class StoppedBudgets(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        control=root/'control';work=root/'work';control.mkdir();work.mkdir()
        self.store=ServiceStore(control)
        self.meta=self.store.create(ServiceSpec(str(work),run_defaults={'total_seconds':30,'retries':1}),{}, {},'one')

    def test_stopped_owner_budget_change_is_guarded_idempotent_and_keeps_permissions(self):
        changes={'run_defaults':{'total_seconds':None,'retries':None},'config':{'max_turns':None}}
        result=self.store.configure_stopped_budgets(self.meta['id'],changes,self.meta['revision'],'one')
        current=self.store.get(self.meta['id']);self.assertIsNone(current['spec']['run_defaults']['retries'])
        self.assertEqual(current['spec']['workspace'],self.meta['spec']['workspace']);self.assertEqual(current['binding'],{})
        self.assertEqual(self.store.configure_stopped_budgets(self.meta['id'],changes,self.meta['revision'],'one'),result)
        with self.assertRaises(ValueError):self.store.configure_stopped_budgets(self.meta['id'],{'run_defaults':{'write_paths':['elsewhere']}},current['revision'],'bad')
        self.assertEqual(self.store.get(self.meta['id']),current)

    def test_live_service_cannot_be_reconfigured(self):
        current=self.store.claim(self.meta['id'],{})
        with self.assertRaises(ValueError):self.store.configure_stopped_budgets(self.meta['id'],{'run_defaults':{'retries':None}},current['revision'],'bad')
