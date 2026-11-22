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

    def test_missing_or_foreign_page_is_not_accepted_as_complete_evidence(self):
        result=collect(self.runtime,self.work)
        record=self.maintenance.get('evidence',result['id'])
        page=record['receipt_pages'][0]
        with self.maintenance.db.write() as conn:
            value=self.maintenance.get('evidence',page,conn);value['work_id']='work_'+'b'*32
            self.maintenance.put(conn,'evidence',value)
        with self.assertRaises(ValueError):receipts(self.runtime,self.work)
