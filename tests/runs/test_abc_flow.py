import importlib.util
import tempfile
import unittest
from pathlib import Path


class ABCDEFlowTests(unittest.TestCase):
    def test_real_process_attention_approval_restart_source_stages_and_goal_flow(self):
        path = Path(__file__).resolve().parents[2] / "examples/managed_run_demo.py"
        spec = importlib.util.spec_from_file_location("managed_run_demo", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as base:
            result = module.run_demo(Path(base))
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["cleanup_complete"])
            self.assertTrue(result["supervisor_restart"])
            self.assertEqual(result["attention_answers"], 1)
            self.assertEqual(result["test_approvals_consumed"], 4)
            self.assertEqual(result["test_attempts"], 7)
            self.assertEqual(result["source_applied"], 2)
            self.assertEqual(result["old_head_ignored"], 1)
            self.assertGreaterEqual(result["notifications"], 6)
            self.assertLessEqual(result["model_http_calls"], 80)
            self.assertEqual(result["paid_model_calls"], 0)
            self.assertTrue(result["proof_survived_restart"])
            self.assertTrue(result["invalidated_proof_reverified"])
            self.assertTrue(result["original_constraints_preserved"])
            self.assertEqual(result["stages_completed"], 5)
            self.assertEqual(result["stage_submissions"], 8)
            self.assertEqual(result["rejected_submissions"], 2)
            self.assertEqual(result["stage_handoffs"], 6)
            self.assertEqual(result["model_handoffs"], 1)
            self.assertEqual(result["member_recoveries"], 1)
            self.assertEqual(result["members"], 2)
            self.assertEqual(result["transfers"], 3)
            self.assertTrue(result["backup_verified"])
            self.assertTrue(result["exported"])
            self.assertTrue(result["dependent_inputs_verified"])
            self.assertTrue(result["cooperative_input_verified"])
            self.assertTrue(result["lead_direction_verified"])
            self.assertTrue(result["same_model_resume_verified"])
            self.assertTrue(result["cancelled_task_abandoned"])
            self.assertTrue(result["new_run_after_cancel"])
            self.assertTrue(result["terminal_finalized"])
            self.assertTrue(result["request_timeout_verified"])
            self.assertTrue(result["member_test_renewal_verified"])
            self.assertTrue(result["large_repository_verified"])
            self.assertEqual(result["test_grant_renewals"], 1)
