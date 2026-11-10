"""Opt-in actual Docker verification for a new runtime candidate and handoff."""

import os
import sys
import unittest

from tests.services import test_service_upgrade as fixture


@unittest.skipUnless(
    os.getenv("HARNESS_DOCKER_IMAGE") and sys.platform == "linux",
    "requires explicit Linux Docker controller",
)
class OperationsIsolationTests(unittest.TestCase):
    setUp = fixture.UpgradeTests.setUp
    host = fixture.UpgradeTests.host
    scenario = fixture.UpgradeTests.scenario

    def test_container_verified_runtime_enters_parked_probation_and_executes(self):
        self.assertEqual(
            self.scenario("confirm", validation_mode="isolated")["text"],
            "After upgrade",
        )
