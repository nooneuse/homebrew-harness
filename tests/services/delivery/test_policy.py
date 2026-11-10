"""Delivery policy rejects unsafe declarations before touching external services."""

import os
import subprocess
import sys
import unittest
import time

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.services.delivery.spec import validate, read_values
from tests.services.delivery import test_git as fixture


class PolicyTests(unittest.TestCase):
    setUp = fixture.GitTests.setUp
    run_git = fixture.GitTests.run_git

    def test_connection_values_use_external_references(self):
        for values in (
            {"mode": []},
            {"auto_actions": [["publish_target"]]},
            {"credentials_file": 7},
            {"target_branch": "main/"},
            {"target_branch": "harness/main"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate({**self.spec, **values})

    def test_local_mode_rejects_private_credentials_and_workspace_remote(self):
        values = {
            "REMOTE": str(self.remote),
            "API": "http://127.0.0.1:9",
            "TOKEN": "real-secret",
            "AUTHOR": "fixture",
            "EMAIL": "fixture@invalid",
        }
        with self.assertRaises(ValueError):
            read_values(self.spec, values, (self.workspace, self.control))
        values.update(TOKEN="synthetic", REMOTE=str(self.workspace / "remote.git"))
        with self.assertRaises(ValueError):
            read_values(self.spec, values, (self.workspace, self.control))

    def test_isolated_values_are_dedicated_owner_only_and_not_in_development(self):
        path = self.base / "private.env"
        path.write_text(
            "REMOTE="
            + str(self.remote)
            + "\nAPI=http://127.0.0.1:9\nTOKEN=private-fixture\nAUTHOR=fixture\nEMAIL=fixture@invalid\n"
        )
        spec = validate(
            {**self.spec, "mode": "isolated", "credentials_file": str(path)}
        )
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_values(spec, {}, (self.workspace, self.control))
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            read_values(
                spec, {"OTHER_TOKEN": "private-fixture"}, (self.workspace, self.control)
            )
        self.assertEqual(
            read_values(spec, {}, (self.workspace, self.control))["api_token"],
            "private-fixture",
        )

    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptor locks")
    def test_live_child_retains_execution_lease_after_parent_descriptor_close(self):
        owner = ExecutionLeases(self.control)
        contender = ExecutionLeases(self.control)
        self.addCleanup(owner.close)
        self.addCleanup(contender.close)
        owner.acquire("delivery-git")
        fd = owner.descriptor("delivery-git")
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"],
            stdin=subprocess.PIPE,
            pass_fds=(fd,),
        )
        try:
            owner.close()
            with self.assertRaises(ValueError):
                contender.acquire("delivery-git")
            child.communicate(timeout=5)
            contender.acquire("delivery-git")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(3)

    def test_git_timeout_stops_helper_and_releases_parent_lease(self):
        from homebrew_harness.execution.model_gateway import (
            Deadline,
            ModelDeadlineExceeded,
        )
        from homebrew_harness.services.delivery.git import GitBackend

        wrapper = self.base / "slow-git"
        wrapper.write_text("#!/bin/sh\nexec /bin/sleep 20\n")
        wrapper.chmod(0o755)
        backend = GitBackend(
            self.control, {**self.spec, "git_executable": str(wrapper)}, self.values
        )
        self.addCleanup(backend.close)
        started = time.monotonic()
        with self.assertRaises(ModelDeadlineExceeded):
            backend.run([], deadline=Deadline.after(0.1))
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(backend.processes)
        self.assertFalse(backend.leases.held("delivery-git"))
