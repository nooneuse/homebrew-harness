import copy
import unittest
from homebrew_harness.runs.spec import RunSpec
from homebrew_harness.services.maintenance_store import MaintenanceStore
from homebrew_harness.services.protocol import fingerprint
from homebrew_harness.services.spec import ServiceSpec
from homebrew_harness.services.store import ServiceStore
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

    def test_legacy_renewal_allows_only_implicit_attention_difference(self):
        service = ServiceStore(self.control)
        scope = {"write_paths": ["src"], "tools": ["read_file", "write_file"]}
        spec = ServiceSpec(
            str(self.workspace),
            profile="repository_maintenance",
            maintenance={
                "scopes": {
                    "code": scope,
                    "explicit": {**scope, "tools": [*scope["tools"], "request_attention"]},
                },
            },
        )
        meta = service.create(spec, {}, {}, "legacy-scope")
        header = service.claim(meta["id"], {})

        saved_spec = copy.deepcopy(header["spec"])
        saved_policy = saved_spec["maintenance"]
        saved_policy["scopes"]["code"]["tools"].remove("request_attention")
        header = service.update(header, spec=saved_spec)
        legacy = MaintenanceStore(service, header, saved_policy)
        existing = []
        for case, scope_name, narrower_scope, allowed in (
            ("legacy attention only", "code", {}, True),
            ("additional write tool", "code", {"tools": ["read_file"]}, False),
            ("additional write path", "code", {"write_paths": []}, False),
            ("attention in saved scope", "explicit", {}, False),
        ):
            with service.db.write() as conn:
                work = service.enqueue(conn, legacy.id, "Existing accepted task.", case)
                work.update(
                    state="in_progress",
                    triage={**self.peer.triage, "scope": scope_name},
                    triage_revision=0,
                    run_id="run_" + "a" * 32,
                    run_spec={**work["run_spec"], **scope, **narrower_scope},
                    authority={
                        "scope": scope_name,
                        "policy": fingerprint(saved_policy),
                        "requirements_revision": 0,
                        "expires_at": 0,
                        "revoked": False,
                    },
                )
                legacy.put(conn, "work", work)
                existing.append((case, work, allowed))

        header = service.get(meta["id"])
        store = MaintenanceStore(service, header, ServiceSpec(**header["spec"]).maintenance)
        self.assertIn("request_attention", store.scopes["code"]["tools"])
        for case, work, allowed in existing:
            with self.subTest(case=case):
                with service.db.write() as conn:
                    persisted = store.get("work", work["id"], conn)
                    request = store.work_request(conn, persisted, persisted["triage"], renew=True)
                payload = {
                    "id": request["id"],
                    "revision": request["revision"],
                    "decision": "approve",
                }
                if not allowed:
                    with (
                        self.assertRaisesRegex(ValueError, "Renewal cannot widen"),
                        service.db.write() as conn,
                    ):
                        store.decide(conn, payload, "renew")
                    self.assertEqual(service.work(work["id"]), work)
                    continue

                with service.db.write() as conn:
                    result = store.decide(conn, payload, "renew")
                renewed = service.work(work["id"])
                self.assertEqual(result["state"], "approved")
                self.assertTrue(store.valid_authority(renewed))
                self.assertEqual(renewed["run_id"], work["run_id"])
                self.assertEqual(renewed["run_spec"], work["run_spec"])
