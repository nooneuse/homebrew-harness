"""Opt-in P3 delivery with actual Docker candidate execution and private values."""

import json
import os
import sys
import unittest

from homebrew_harness.execution.test_runner import _UNITTEST
from tests.services.delivery import test_flow as fixture
from tests.runtime.test_interaction import eventually

IMAGE = os.getenv("HARNESS_DOCKER_IMAGE", "")
CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


@unittest.skipUnless(
    IMAGE and sys.platform == "linux",
    "requires explicit Linux Docker controller and image",
)
class DeliveryIsolationTests(unittest.TestCase):
    setUp = fixture.FlowTests.setUp
    run_git = fixture.FlowTests.run_git
    start = fixture.FlowTests.start
    pending_target = fixture.FlowTests.pending_target

    def test_candidate_cannot_read_delivery_credentials_or_host_control(self):
        credentials = self.base / "publish.env"
        secret = "private-delivery-fixture-value"
        credentials.write_text(
            "REMOTE="
            + str(self.remote)
            + "\nAPI="
            + self.peer.url
            + "\nTOKEN="
            + secret
            + "\nAUTHOR=fixture\nEMAIL=fixture@invalid\n"
        )
        credentials.chmod(0o600)
        (self.workspace / "tests/test_value.py").write_text(
            "import unittest,os\nfrom pathlib import Path\nclass Value(unittest.TestCase):\n"
            '    def test_value(self):\n        self.assertGreaterEqual(int(Path("src/value.txt").read_text()),1)\n'
            "    def test_private_boundary(self):\n"
            "        self.assertNotIn(" + repr(secret) + ", os.environ.values())\n"
            "        for path in "
            + repr(
                [
                    str(credentials),
                    str(self.control / ".harness/runtime/database/runtime.sqlite3"),
                    "/var/run/docker.sock",
                ]
            )
            + ":\n"
            "            with self.assertRaises(OSError):\n                Path(path).read_bytes()\n"
        )
        self.run_git("add", "tests", cwd=self.workspace)
        self.run_git("commit", "-qm", "private boundary test", cwd=self.workspace)
        self.run_git("push", str(self.remote), "main", cwd=self.workspace)
        registry = json.loads(self.tests.read_text())
        registry["execution"] = {
            "backend": "container",
            "policy": {
                "cli": CLI,
                "image": IMAGE,
                "native_argv": ["{python}", "-I", "-c", _UNITTEST],
                "argv": ["/usr/local/bin/python", "-I", "-c", _UNITTEST],
                "memory_mb": 128,
                "pids": 32,
            },
        }
        self.tests.write_text(json.dumps(registry))
        service = self.start(
            mode="isolated",
            credentials_file=str(credentials),
            required_checks=[],
            check_app_ids=[],
            merge={"mode": "manual", "poll_seconds": 0.1},
        )
        work = service.work.submit("Fix the scoped value.")
        eventually(
            lambda: any(
                r["state"] == "merge_ready"
                for r in service.runtime.maintenance.delivery.rows("changesets")
            ),
            45,
        )
        row = service.runtime.maintenance.delivery.store.get(
            "changesets", work.snapshot().details["changeset_id"]
        )
        self.assertTrue(row["quality"])
        self.assertTrue(
            all(
                r.get("isolation", {}).get("os_isolation_verified")
                for r in row["quality"]
            ),
            row["quality"],
        )
        self.assertFalse(any(secret in json.dumps(m) for m in self.peer.model_requests))
        self.peer.merge_pull(1)
        eventually(lambda: work.snapshot().state == "delivered", 20)
