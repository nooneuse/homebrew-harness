"""Long service reasoning remains explicitly bounded and configurable."""
import tempfile
import unittest
from pathlib import Path
from homebrew_harness import ServiceSpec


class ReasoningLimits(unittest.TestCase):
    def test_self_maintenance_capacity_and_finite_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = str(Path(directory).resolve())
            spec = ServiceSpec(workspace, profile="repository_maintenance", maintenance={"job_seconds": 2700})
            self.assertIsNone(spec.maintenance["job_turns"])
            self.assertEqual(spec.maintenance["job_call_warning"], 40)
            self.assertEqual(spec.maintenance["job_seconds"], 2700)
            configured = ServiceSpec(workspace, profile="repository_maintenance", maintenance={"job_turns": 512})
            self.assertEqual(configured.maintenance["job_turns"], 512)
            for invalid in (0, True, 1.5):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    ServiceSpec(workspace, profile="repository_maintenance", maintenance={"job_turns": invalid})
