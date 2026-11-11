"""Actual Docker PR execution keeps cleanup ownership through stop and retry."""

import json
import os
import sys
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.runtime.test_interaction import eventually
from tests.services.communication import test_flow as fixture

CLI = os.environ.get("HARNESS_TEST_CONTAINER_CLI")
IMAGE = os.environ.get("HARNESS_TEST_CONTAINER_IMAGE")


@unittest.skipUnless(CLI and IMAGE, "requires explicit real Docker PR test policy")
class ContainerCleanup(unittest.TestCase):
    setUp = fixture.CommunicationFlow.setUp
    start = fixture.CommunicationFlow.start
    discussion = fixture.CommunicationFlow.discussion

    def test_stop_delete_failure_retains_lease_then_retries_actual_removal(self):
        command = "import time; print('started',flush=True); time.sleep(30)"
        registry = self.root / "container-tests.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "slow-pr",
                            "argv": [sys.executable, "-c", command],
                            "timeout": 40,
                        }
                    ],
                    "execution": {
                        "backend": "container",
                        "policy": {
                            "cli": CLI,
                            "image": IMAGE,
                            "argv": ["/usr/local/bin/python", "-c", command],
                            "native_argv": [sys.executable, "-c", command],
                        },
                    },
                }
            )
        )
        self.host.config = replace(
            self.host.config,
            settings=replace(self.host.config.settings, test_config=registry),
        )
        self.peer.issue(pr=True)
        service = self.start(required_tests=["slow-pr"], prepare_seconds=60)
        c = service.runtime.maintenance.communication
        runner = eventually(
            lambda: (
                c.test_runner
                if c.test_runner and c.test_runner.backend.active
                else None
            ),
            30,
        )
        backend = runner.backend
        release = threading.Event()
        self.addCleanup(release.set)
        original = backend._remove

        def remove(*args, **kwargs):
            if not release.is_set():
                return False
            return original(*args, **kwargs)

        with patch.object(backend, "_remove", side_effect=remove):
            service.stop()
            eventually(lambda: service.runtime.shutdown.error, 10)
            self.assertFalse(service.wait_closed(0))
            self.assertIs(c.test_runner, runner)
            self.assertTrue(c.leases.held("communication-review"))
            self.assertTrue(backend.pending)
            release.set()
            service.close()
            self.assertTrue(service.wait_closed(15))
        self.assertFalse(backend.pending)
        self.assertTrue(backend.wait_closed(0))
        self.assertFalse(c.leases.held("communication-review"))
