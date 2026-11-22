import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from homebrew_harness.execution.test_runner import TestRunner, TestRecipe, digest
from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.verification import applicable, basis


class LegacyContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'source.txt').write_text('fixed input')
        self.recipe = TestRecipe('unit', (sys.executable,'-c','print("ok")'), input_globs=('source.txt',))
        self.runner = TestRunner(self.root,[self.recipe]); self.addCleanup(self.runner.close)
        self.runner.backend = SimpleNamespace(name='container',fingerprint=lambda:'fixed-isolation')
        legacy = digest({'recipe': self.recipe.fingerprint(legacy_output=True), 'backend': 'fixed-isolation'})
        self.row = {'run_id':'test_legacy','recipe':'unit','fingerprint':legacy,'inputs':snapshot(self.root,('source.txt',)),
                    'execution_backend':'container','test_scope':'full','selection':None,'role':'lead','executed_at':10,
                    'status':'passed','exit_code':0,'source_stable':True,
                    'isolation':{'cleanup_complete':True,'os_isolation_verified':True}}
        self.row['verification_basis'] = basis(self.row)

    def test_older_stricter_output_receipt_is_reused_without_new_execution_or_authority(self):
        old = copy.deepcopy(self.row)
        result = applicable([self.row],self.runner,'unit',self.root)
        self.assertTrue(result['ok']); self.assertEqual(result['receipt_ref'],'test_legacy')
        self.assertEqual(result['executed_at'],10); self.assertEqual(result['compatibility']['rule'],'legacy_implicit_output_limit')
        self.assertNotEqual(self.runner._fingerprint(self.recipe),self.row['fingerprint'])
        self.assertEqual(self.runner.runs,0); self.assertEqual(self.row,old)

    def test_changes_to_inputs_isolation_hard_output_or_newer_failure_still_prevent_reuse(self):
        (self.root/'source.txt').write_text('changed')
        self.assertFalse(applicable([self.row],self.runner,'unit',self.root)['ok'])
        (self.root/'source.txt').write_text('fixed input')
        self.runner.backend.fingerprint=lambda:'changed-isolation'
        self.assertFalse(applicable([self.row],self.runner,'unit',self.root)['ok'])
        self.runner.backend.fingerprint=lambda:'fixed-isolation'
        from dataclasses import replace
        self.runner.recipes['unit']=replace(self.recipe,hard_output_bytes=100)
        self.assertFalse(applicable([self.row],self.runner,'unit',self.root)['ok'])
        self.runner.recipes['unit']=self.recipe
        failed={**self.row,'run_id':'test_new','executed_at':20,'status':'failed','exit_code':1,
                'verification_basis':{**self.row['verification_basis'],'contract':self.runner._fingerprint(self.recipe)}}
        result=applicable([self.row,failed],self.runner,'unit',self.root)
        self.assertFalse(result['ok']);self.assertEqual(result['reason'],'newer_failure')
