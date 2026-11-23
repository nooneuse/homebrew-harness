import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from homebrew_harness.services.store import ServiceStore
from homebrew_harness.services.spec import ServiceSpec
from homebrew_harness.services.maintenance_store import MaintenanceStore
from homebrew_harness.services.evidence import collect,receipts
from homebrew_harness.runs import RunStore,RunSpec
from homebrew_harness.runtime.limits import limits_scope


class ReceiptPages(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        control=root/'control';source=root/'source';control.mkdir();source.mkdir()
        self.service=ServiceStore(control)
        meta=self.service.create(ServiceSpec(str(source)),{}, {},'fixture')
        header=self.service.claim(meta['id'],{})
        self.native=RunStore(source);self.addCleanup(self.native.close)
        run=self.native.create(RunSpec('fixture'))
        self.work={'id':'work_'+'a'*32,'run_id':run['id'],'run_history':[],'requirements_revision':0}
        self.maintenance=MaintenanceStore(self.service,header,{})
        self.runtime=SimpleNamespace(id=meta['id'],store=self.service,header=header,maintenance=SimpleNamespace(store=self.maintenance),
            child_for=lambda work:SimpleNamespace(run_store=self.native,store=self.native.runtime))
        db=self.native.runtime.backend()
        with db.write() as conn:
            for i in range(115):
                db._put(conn,'test_receipts','test_'+str(i),{'run_id':'test_'+str(i),'recipe':'unit' if i<114 else 'other',
                    'scope':{'run_id':run['id']},'status':'failed','exit_code':1,'source_stable':True,'executed_at':i+1})

    def test_collection_beyond_one_hundred_preserves_every_owned_receipt(self):
        with limits_scope({'evidence_page_rows':17}):result=collect(self.runtime,self.work)
        self.assertEqual(result['receipt_count'],115);self.assertTrue(result['receipts_complete'])
        rows=receipts(self.runtime,self.work);self.assertEqual(len(rows),115)
        self.assertEqual(len(receipts(self.runtime,self.work,recipe='unit')),114)
        record=self.maintenance.get('evidence',result['id'])
        self.assertEqual(len(record['receipt_pages']),7)
        other={**self.work,'id':'work_'+'b'*32}
        self.assertEqual(receipts(self.runtime,other),[])

    def test_filtered_pages_keep_valid_full_and_focused_evidence_separately(self):
        selection = {'targets': ['tests.unit']}
        requested = {'version': 1, 'inputs': 'content', 'backend': 'container', 'role': 'lead',
                     'contract': 'current', 'selection': selection, 'test_scope': 'selected'}
        db = self.native.runtime.backend()
        with db.write() as conn:
            for i, run_id, status, selector, stable in [
                (116, 'full_failed', 'failed', None, True),
                (117, 'focused_pass', 'passed', selection, True),
                (118, 'later_disjoint_failure', 'failed', {'targets': ['tests.other']}, True),
                (119, 'unstable_full_pass', 'passed', None, False),
                (120, 'full_pass', 'passed', None, True),
                (121, 'unstable_full_failure', 'failed', None, False),
                (122, 'unstable_later_pass', 'passed', None, False),
            ]:
                observed = {**requested, 'selection': selector, 'test_scope': 'selected' if selector else 'full'}
                db._put(conn, 'test_receipts', run_id, {
                    'run_id': run_id, 'recipe': 'unit', 'status': status, 'executed_at': i,
                    'exit_code': 0 if status == 'passed' else 1, 'source_stable': stable,
                    'isolation': {'cleanup_complete': True, 'os_isolation_verified': True},
                    'scope': {'run_id': self.work['run_id']}, 'verification_basis': observed,
                })
        with limits_scope({'evidence_page_rows': 3}):
            collect(self.runtime, self.work)
            rows = receipts(self.runtime, self.work, recipe='unit', basis=requested, contracts=['current'])
        self.assertEqual({row['run_id'] for row in rows}, {'full_failed', 'full_pass', 'focused_pass'})

    def test_missing_or_foreign_page_is_not_accepted_as_complete_evidence(self):
        result=collect(self.runtime,self.work)
        record=self.maintenance.get('evidence',result['id'])
        page=record['receipt_pages'][0]
        with self.maintenance.db.write() as conn:
            value=self.maintenance.get('evidence',page,conn);value['work_id']='work_'+'b'*32
            self.maintenance.put(conn,'evidence',value)
        with self.assertRaises(ValueError):receipts(self.runtime,self.work)
