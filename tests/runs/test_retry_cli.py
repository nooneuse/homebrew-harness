import unittest
from homebrew_harness.cli import parser
from homebrew_harness.services.protocol import validate_method


class RetryCLITests(unittest.TestCase):
    def test_retry_command_parses_explicit_window_and_request_identity(self):
        args = parser().parse_args(["run", "retry", "run_" + "a" * 32, "--seconds", "20", "--request-id", "one"])
        self.assertEqual(args.command, "retry")
        self.assertEqual(args.seconds, 20)
        self.assertEqual(args.request_id, "one")

    def test_service_retry_control_has_only_its_declared_fields(self):
        validate_method("work_retry", {"id": "work_" + "a" * 32, "revision": 1, "seconds": 20})
        with self.assertRaises(ValueError):
            validate_method("work_retry", {"id": "work_" + "a" * 32, "revision": 1, "approve": True})
