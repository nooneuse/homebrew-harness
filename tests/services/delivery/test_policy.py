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

    def test_ci_counts_default_to_advisory_and_explicit_budgets_remain_bounded(self):
        raw = {k: v for k, v in self.spec.items() if k not in {'ci_fix_limit', 'ci_fix_warning'}}
        default = validate(raw)
        self.assertIsNone(default['ci_fix_limit'])
        self.assertEqual(default['ci_fix_warning'], 2)
        for limit in (None, 0, 2, 10, 11, 200):
            with self.subTest(limit=limit):
                self.assertEqual(validate({**raw, 'ci_fix_limit': limit})['ci_fix_limit'], limit)
        for limit in (True, -1, 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                validate({**raw, 'ci_fix_limit': limit})
        self.assertIsNone(validate({**raw, 'ci_fix_warning': None})['ci_fix_warning'])
        for warning in (True, 0, 2.5):
            with self.subTest(warning=warning), self.assertRaises(ValueError):
                validate({**raw, 'ci_fix_warning': warning})

    def test_automatic_pr_text_requires_a_boolean_and_pr_preauthorization(self):
        raw = {k: v for k, v in self.spec.items() if k != "auto_publish_pr_text"}
        # Keep omitted settings out of persisted policy fingerprints.
        self.assertNotIn("auto_publish_pr_text", validate(raw))
        self.assertIs(
            validate({**raw, "auto_publish_pr_text": False})["auto_publish_pr_text"],
            False,
        )
        for actions in ([], ["publish_branch"]):
            with self.subTest(actions=actions), self.assertRaisesRegex(
                ValueError, "ensure_change_request"
            ):
                validate({**raw, "auto_actions": actions, "auto_publish_pr_text": True})
        enabled = {**raw, "auto_actions": ["ensure_change_request"]}
        self.assertIs(
            validate({**enabled, "auto_publish_pr_text": True})["auto_publish_pr_text"],
            True,
        )
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({**enabled, "auto_publish_pr_text": value})

    def test_closing_keyword_opt_in_is_boolean_and_omission_does_not_change_policy(self):
        from homebrew_harness.services.protocol import fingerprint

        raw = {k: v for k, v in self.spec.items() if k != "allow_closing_keywords"}
        compiled = validate(raw)
        self.assertNotIn("allow_closing_keywords", compiled)
        for value in (True, False):
            declared = validate({**raw, "allow_closing_keywords": value})
            self.assertIs(declared.pop("allow_closing_keywords"), value)
            self.assertEqual(fingerprint(compiled), fingerprint(declared))
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "allow_closing_keywords"):
                validate({**raw, "allow_closing_keywords": value})

    def test_review_publication_and_github_guard_closing_keywords_by_default(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from homebrew_harness.platforms.effects import EffectFailure
        from homebrew_harness.platforms.github.delivery import GithubDelivery
        from homebrew_harness.services import job_results

        publication = {"title": "Restore the value", "body": "Fixes #1"}
        review = {"text": "Verified", "findings": [], "publication": publication}
        plan = {**publication, "correlation": "1", "branch": "harness/example"}
        for flag in ({}, {"allow_closing_keywords": False}, {"allow_closing_keywords": True}):
            with self.subTest(flag=flag):
                spec = {**self.spec, **flag}
                store = SimpleNamespace(delivery=SimpleNamespace(spec=spec))
                adapter = GithubDelivery(spec, {"api_url": "http://127.0.0.1:9", "api_token": "synthetic"})
                try:
                    with patch.object(adapter, "pages", return_value=[]) as pages:
                        if flag.get("allow_closing_keywords"):
                            self.assertEqual(job_results.review(store, {"kind": "review"}, review), review)
                            self.assertEqual(job_results.publication(store, {}, publication), publication)
                            self.assertIsNone(adapter.ensure_change_request(plan, reconcile=True))
                            pages.assert_called_once()
                        else:
                            with self.assertRaisesRegex(ValueError, "implicitly close issues"):
                                job_results.review(store, {"kind": "review"}, review)
                            with self.assertRaisesRegex(ValueError, "implicitly close issues"):
                                job_results.publication(store, {}, publication)
                            with self.assertRaisesRegex(EffectFailure, "not_executed") as blocked:
                                adapter.ensure_change_request(plan, reconcile=True)
                            self.assertIn("implicitly close issues", str(blocked.exception.__cause__))
                            pages.assert_not_called()
                finally:
                    adapter.close()

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
        real_dir = self.base / "real-creds"
        real_dir.mkdir(mode=0o700)
        nested = real_dir / "private.env"
        nested.write_bytes(path.read_bytes())
        nested.chmod(0o600)
        link_dir = self.base / "link-creds"
        link_dir.symlink_to(real_dir)
        linked_spec = validate(
            {**self.spec, "mode": "isolated", "credentials_file": str(link_dir / "private.env")}
        )
        with self.assertRaisesRegex(ValueError, "symlink"):
            read_values(linked_spec, {}, (self.workspace, self.control))

    def test_declared_ca_file_env_rejects_missing_value_at_delivery_startup(self):
        from homebrew_harness.platforms.github.delivery import GithubDelivery

        spec = validate({**self.spec, "ca_file_env": "CA"})
        values = {
            "REMOTE": str(self.remote),
            "API": "http://127.0.0.1:9",
            "TOKEN": "synthetic",
            "AUTHOR": "fixture",
            "EMAIL": "fixture@invalid",
        }
        with self.assertRaisesRegex(ValueError, "Platform CA configuration is missing"):
            read_values(spec, values, (self.workspace, self.control))
        with self.assertRaisesRegex(ValueError, "Platform CA configuration is missing"):
            GithubDelivery(
                spec,
                {
                    "api_url": "http://127.0.0.1:9",
                    "api_token": "synthetic",
                    "ca_file": None,
                },
                roots=(self.workspace, self.control),
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
