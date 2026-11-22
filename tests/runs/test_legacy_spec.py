import unittest
from homebrew_harness.runs import RunSpec
from homebrew_harness.runs.requests import digest
from homebrew_harness.runs.test_authorization import scope, valid
from tests.runs import test_p2_authorization as fixture


class LegacyRunSpec(unittest.TestCase):
    setUp=fixture.TestGrantTests.setUp
    setup_run=fixture.TestGrantTests.setup_run
    claim=fixture.TestGrantTests.claim

    def test_real_member_test_scope_uses_the_accepted_legacy_spec(self):
        control,session,_=self.setup_run(one=True)
        old=dict(control.state['spec'])
        for key in ('runtime_limits','repeat_warning','retry_warning','total_warning_seconds'):old.pop(key,None)
        self.store.update(control.header,{'spec':old})
        control.refresh();control.spec=RunSpec.from_dict(control.state['spec'])
        task,cwd,execution=self.claim(control,session,'alpha')
        context=scope(control,'check',cwd,execution)
        self.assertEqual(context['spec'],digest(old))
        self.assertTrue(valid(self.store,control.state,context))
        changed={**control.state,'spec':{**old,'write_paths':['different']}}
        self.assertFalse(valid(self.store,changed,context))
