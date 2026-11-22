import copy
import tempfile
import unittest
from pathlib import Path
from homebrew_harness.runs import RunStore,RunSpec


class BudgetConfiguration(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.store=RunStore(Path(tmp.name));self.addCleanup(self.store.close)
        state=self.store.claim(self.store.create(RunSpec('fixed work',total_seconds=30,retries=1))['id'])
        self.header={'run_id':state['id'],'owner':state['owner'],'generation':state['generation']}
        self.store.update(self.header,{'status':'paused','cleanup_complete':True,'test_runs':7})
        self.run=self.store.get(state['id'])

    def test_configuration_changes_only_selected_budgets_and_preserves_history_and_deadline(self):
        before=copy.deepcopy(self.run)
        changes={'total_seconds':None,'retries':None,'repeat_limit':None}
        receipt=self.store.configure_budgets(self.run['id'],changes,self.run['revision'],'one')
        after=self.store.get(self.run['id'])
        self.assertIsNone(after['spec']['retries']);self.assertEqual(after['test_runs'],7)
        self.assertEqual(after['deadline_at'],before['deadline_at']);self.assertEqual(after['test_grants'],before['test_grants'])
        self.assertEqual(after['spec']['prompt'],before['spec']['prompt'])
        self.assertEqual(self.store.configure_budgets(self.run['id'],changes,self.run['revision'],'one'),receipt)
        with self.assertRaises(ValueError):self.store.configure_budgets(self.run['id'],{'write_paths':['outside']},after['revision'],'bad')
        self.assertEqual(self.store.get(self.run['id']),after)

    def test_running_or_unsettled_work_cannot_be_reconfigured(self):
        self.store.update(self.header,{'cleanup_complete':False})
        current=self.store.get(self.run['id'])
        with self.assertRaises(ValueError):self.store.configure_budgets(current['id'],{'retries':None},current['revision'],'one')
