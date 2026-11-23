import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, ServiceSpec
from tests.services.delivery.fixture import Peer
from tests.runtime.test_interaction import eventually

# These waits observe several native process/Git phases. Keep their execution,
# authorization and verification budgets explicit and unchanged.


class FlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.workspace = self.base / "project"
        self.workspace.mkdir()
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)
        self.git = shutil.which("git")
        (self.workspace / "src").mkdir()
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests/__init__.py").write_text("")
        (self.workspace / "src/value.txt").write_text("0\n")
        (self.workspace / "tests/test_value.py").write_text(
            'import unittest\nfrom pathlib import Path\nclass Value(unittest.TestCase):\n    def test_value(self):\n        self.assertGreaterEqual(int(Path("src/value.txt").read_text()),1)\n'
        )
        for args in [
            ("init", "-q"),
            ("add", "src", "tests"),
            ("commit", "-qm", "baseline"),
            ("branch", "-M", "main"),
        ]:
            self.run_git(*args, cwd=self.workspace)
        self.remote = self.base / "remote.git"
        self.run_git("clone", "--bare", str(self.workspace), str(self.remote))
        self.peer = Peer(self.git, self.remote)
        self.addCleanup(self.peer.close)
        self.env = self.base / "model.env"
        self.env.write_text(
            "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
            + self.peer.url
            + "/v1\nREMOTE="
            + str(self.remote)
            + "\nAPI="
            + self.peer.url
            + "\nTOKEN=synthetic\nAUTHOR=fixture\nEMAIL=fixture@invalid\n"
        )
        self.tests = self.base / "tests.json"
        self.tests.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "unit-tests",
                            "builtin": "unittest",
                            "description": "Fixed value regression",
                            "roles": ["lead"],
                            "input_globs": ["src/**", "tests/**"],
                            "locked_globs": ["tests/**"],
                            "timeout": 10,
                            "max_runs": 10,
                            "authorization": "allow",
                        }
                    ],
                }
            )
        )
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(
                env_file=self.env, test_config=self.tests, shutdown_seconds=5
            ),
        )
        self.addCleanup(self.host.close)

    def run_git(self, *args, cwd=None):
        return subprocess.check_output(
            [
                self.git,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=cwd or self.base,
            stderr=subprocess.DEVNULL,
        )

    def start(self, *, operations=None, maintenance=None, **delivery):
        spec = ServiceSpec(
            str(self.workspace),
            profile="repository_maintenance",
            run_defaults={
                "total_seconds": 60,
                "request_seconds": 5,
                "episode_model_calls": 4,
                "write_paths": ["src"],
                "tools": ["read_file", "write_file"],
                "shutdown_seconds": 3,
            },
            maintenance={
                "scopes": {
                    "code": {
                        "write_paths": ["src"],
                        "tools": ["read_file", "write_file"],
                    }
                },
                "auto_categories": ["bug"],
                "job_seconds": 20,
                "report_seconds": 3600,
                **(maintenance or {}),
            },
            operations=operations or {},
            delivery={
                "mode": "local",
                "git_executable": self.git,
                "provider": "github",
                "instance": "primary",
                "project": "example/project",
                "remote_env": "REMOTE",
                "api_url_env": "API",
                "api_token_env": "TOKEN",
                "author_name_env": "AUTHOR",
                "author_email_env": "EMAIL",
                "required_tests": ["unit-tests"],
                "require_review": True,
                "auto_ci_fix": False,
                "required_checks": ["CI"],
                "check_app_ids": [11],
                "auto_actions": ["publish_branch", "ensure_change_request"],
                "ci_poll_seconds": 0.1,
                "merge": {"mode": "service_approved", "poll_seconds": 0.1},
                **delivery,
            },
        )
        service = self.host.services.start(spec)
        self.addCleanup(lambda: (service.close(), service.wait_closed(15)))
        return service

    def pending_target(self, service):
        row = next(
            (
                r
                for r in reversed(
                    service.runtime.maintenance.delivery.rows("changesets")
                )
                if service.store.work(r["work_id"]).get("changeset_id") == r["id"]
            ),
            None,
        )
        if row and row["state"] in {"failed", "quality_failed", "superseded"}:
            raise AssertionError(
                "Delivery state: "
                + str(
                    {
                        k: row.get(k)
                        for k in ("state", "error", "error_detail", "review")
                    }
                )
            )
        if service.runtime.maintenance.error:
            raise AssertionError(
                "Maintenance worker: " + service.runtime.maintenance.error
            )
        if service.runtime.maintenance.delivery.error:
            raise AssertionError(
                "Delivery worker: " + service.runtime.maintenance.delivery.error
            )
        return next(
            (r for r in service.requests() if r.kind == "delivery_authority"), None
        )

    def test_fixed_candidate_ci_and_host_target_approval(self):
        service = self.start()
        work = service.work.submit("Fix the value regression.")
        request = eventually(
            lambda: self.pending_target(service),
            120,
        )
        self.assertEqual(request.arguments["scope"], "delivery")
        row = service.records("changesets")["items"][0]
        self.assertEqual(service.delivery(row["id"])["state"], "merge_ready")
        self.assertNotEqual(
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip(),
            service.delivery(row["id"])["candidate"]["commit"],
        )
        service.decide(request, "approve", decision_id="target")
        eventually(lambda: work.snapshot().state == "delivered", 80)

        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(service.delivery(row["id"])["state"], "delivered")
        self.assertTrue(service.delivery(row["id"])["quality"])

    def test_candidate_rechecks_original_goal_instead_of_source_run(self):
        (self.workspace / "unpublished.txt").write_text("another work")
        service = self.start()
        service.work.submit(
            "Fix the scoped value.",
            goal={
                "condition": "The marker must be delivered.",
                "judge": False,
                "predicates": [
                    {"id": "marker", "type": "file_exists", "path": "unpublished.txt"}
                ],
            },
        )
        row = eventually(
            lambda: next(
                (
                    r
                    for r in service.records("changesets")["items"]
                    if r.get("error") == "candidate_goal_failed"
                ),
                None,
            ),
            120,
        )
        full = service.runtime.maintenance.store.get("changesets", row["id"])
        self.assertEqual(full["error"], "candidate_goal_failed")
        self.assertFalse(full["goal_evidence"]["ok"])
        self.assertFalse(service.records("actions")["items"])
        self.assertEqual(self.peer.writes, [])

    def test_development_finishes_before_service_opens_pr_without_attention(self):
        baseline = (
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip()
        )
        service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
        work = service.work.submit("Fix the value and hand off its verified result.")
        eventually(lambda: self.peer.pulls, 120)
        saved = service.store.work(work.id)
        self.assertIn(
            "Service development-to-delivery handoff:", saved["run_spec"]["prompt"]
        )
        self.assertIn("Do not wait for a PR URL", saved["run_spec"]["prompt"])
        self.assertTrue(
            any(
                "Service development-to-delivery handoff:" in m.get("content", "")
                for request in self.peer.model_requests
                for m in request["messages"]
            )
        )
        self.assertNotIn("bash", saved["run_spec"]["tools"])
        child = service.runtime.child_for(saved)
        run = child.run_store.get(saved["run_id"])
        self.assertEqual(run["status"], "completed")
        self.assertFalse(
            any(
                r["kind"] == "attention"
                for r in child.store.items("run_requests").values()
            )
        )
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip(),
            baseline,
        )
        self.assertIsNotNone(saved["changeset_id"])

    def test_delivery_resume_keeps_handoff_run_and_candidate_identity(self):
        service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
        work = service.work.submit("Fix and hand off the scoped source.")
        eventually(lambda: self.peer.pulls, 120)
        before = service.store.work(work.id)
        progress = work.progress()
        self.assertEqual(progress["phase"], "delivery")
        self.assertIsNotNone(progress["handoff_ref"])
        work.pause()
        work.resume()
        after = service.store.work(work.id)
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["changeset_id"], before["changeset_id"])
        self.assertEqual(after["progress_ref"], before["progress_ref"])
        self.assertEqual(len(self.peer.pulls), 1)

    def test_execution_context_does_not_revise_requirements_or_candidate(self):
        service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
        work = service.work.submit("Complete the source change.")
        eventually(lambda: self.peer.pulls, 120)
        before = service.store.work(work.id)
        work.context("The runtime was restarted; keep the current delivery stage.")
        after = service.store.work(work.id)
        self.assertEqual(
            after["requirements_revision"], before["requirements_revision"]
        )
        (
            self.assertEqual(after["amendments"], before.get("amendments", []))
            if "amendments" in after
            else None
        )
        self.assertEqual(after["changeset_id"], before["changeset_id"])
        self.assertIn("runtime was restarted", after["execution_context"][-1])

    def test_restart_retains_candidate_and_target_request(self):
        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        request = eventually(lambda: self.pending_target(service), 120)
        candidate = service.delivery(work.snapshot().details["changeset_id"])[
            "candidate"
        ]["commit"]
        service.stop()
        self.assertTrue(service.wait_closed(15))
        restored = self.host.services.start(identity=service.id)
        self.addCleanup(lambda: (restored.close(), restored.wait_closed(15)))
        fresh = eventually(lambda: self.pending_target(restored), 60)
        self.assertEqual(fresh.ref.id, request.ref.id)
        self.assertEqual(
            restored.delivery(work.snapshot().details["changeset_id"])["candidate"][
                "commit"
            ],
            candidate,
        )
        restored.decide(fresh, "approve", decision_id="restored-target")
        eventually(
            lambda: restored.work.get(work.id).snapshot().state == "delivered", 80
        )
        self.assertEqual(len(self.peer.pulls), 1)

    def test_old_ci_does_not_make_current_candidate_merge_ready(self):
        self.peer.ci_head = "0" * 40
        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        row = eventually(
            lambda: next(
                (
                    r
                    for r in service.records("changesets")["items"]
                    if r["state"] == "ci_waiting"
                ),
                None,
            ),
            120,
        )
        full = service.runtime.maintenance.store.get("changesets", row["id"])
        service.runtime.maintenance.delivery.advance(full)
        self.assertEqual(service.delivery(row["id"])["state"], "ci_waiting")
        self.assertFalse(self.pending_target(service))
        self.peer.ci_head = None
        eventually(lambda: self.pending_target(service), 80)
        self.assertNotEqual(work.snapshot().state, "delivered")

    def test_ci_fix_uses_a_new_native_run_and_preserves_scope(self):
        self.peer.ci_failure = True
        service = self.start(auto_ci_fix=True)
        work = service.work.submit("Fix the value regression.")
        request = eventually(
            lambda: self.pending_target(service),
            180,
        )
        value = service.store.work(work.id)
        self.assertEqual(value["ci_fix_attempts"], 1)
        original = service.runtime.maintenance.store.get(
            "work_progress", value["original_baseline_ref"]
        )
        self.assertNotEqual(original["run_id"], value["run_id"])
        self.assertEqual(
            original["original_baseline"]["head"],
            service.delivery(value["changeset_id"])["candidate"]["target_base"],
        )
        self.assertTrue(value["run_workspace"].startswith(str(self.control)))
        self.assertEqual(value["run_spec"]["write_paths"], ["src"])
        self.assertEqual(
            Path(value["run_workspace"], "src/value.txt").read_text(), "2\n"
        )
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(sum(kind == "pull-update" for kind, _ in self.peer.writes), 1)
        service.decide(request, "approve", decision_id="target")
        eventually(lambda: work.snapshot().state == "delivered", 80)

    def test_ci_triage_keeps_fixed_delta_without_serializing_host_inventory(self):
        from homebrew_harness.services.agent import ServiceAgent

        original = ServiceAgent._execute
        inventory = {
            f"internal_version_{n}.txt": {
                "sha256": "a" * 64,
                "version": [1, 2, 3, 4, 5],
                "executable": False,
            }
            for n in range(2000)
        }

        def execute(agent, job, **kwargs):
            if job["kind"] == "ci_triage":
                with agent.store.db.write() as conn:
                    row = agent.store.get("changesets", job["target"], conn)
                    row["primary_expected"] = inventory
                    agent.store.put(conn, "changesets", row)
            return original(agent, job, **kwargs)

        self.peer.ci_failure = True
        with patch.object(ServiceAgent, "_execute", execute):
            service = self.start(auto_ci_fix=False)
            work = service.work.submit(
                "Fix the scoped source and diagnose its fixed CI result."
            )
            row = eventually(
                lambda: next(
                    (
                        r
                        for r in service.records("changesets")["items"]
                        if r.get("ci_diagnosis")
                    ),
                    None,
                ),
                140,
            )
        self.assertEqual(row["ci_diagnosis"]["classification"], "code")
        inputs = [
            json.loads(m["content"])["input"]
            for request in self.peer.model_requests
            for m in request["messages"]
            if m.get("role") == "user"
            and m.get("content", "").startswith("{")
            and json.loads(m["content"]).get("job", {}).get("kind") == "ci_triage"
        ]
        self.assertTrue(inputs)
        self.assertNotIn("primary_expected", inputs[-1])
        self.assertIn("src/value.txt", inputs[-1]["candidate"]["delta"])
        self.assertEqual(
            service.runtime.maintenance.store.get("changesets", row["id"])[
                "primary_expected"
            ],
            inventory,
        )

    def test_repair_derives_work_identity_without_a_prior_run_context(self):
        from homebrew_harness.services.delivery.runtime import Delivery

        original = Delivery.repair

        def repair(delivery, row, work):
            settings = {
                k: v for k, v in work["run_spec"].items() if k != "work_context"
            }
            return original(delivery, row, {**work, "run_spec": settings})

        self.peer.ci_failure = True
        with patch.object(Delivery, "repair", repair):
            service = self.start(auto_ci_fix=True)
            work = service.work.submit("Continue this Work through focused CI repair.")
            eventually(lambda: self.pending_target(service), 180)
        saved = service.store.work(work.id)
        self.assertEqual(saved["ci_fix_attempts"], 1)
        context = saved["run_spec"]["work_context"]
        self.assertEqual(context["service_id"], service.id)
        self.assertEqual(context["work_id"], work.id)
        self.assertEqual(
            context["requirements_revision"], saved["requirements_revision"]
        )

    def test_lost_pull_creation_ack_is_reconciled_without_duplicate_write(self):
        self.peer.drop = "pull"
        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        action = eventually(
            lambda: next(
                (
                    a
                    for a in service.records("actions")["items"]
                    if a["state"] == "unknown"
                ),
                None,
            ),
            120,
        )
        self.assertEqual(action["kind"], "ensure_change_request")
        self.assertEqual(sum(kind == "pull" for kind, _ in self.peer.writes), 1)
        self.peer.drop = None
        self.assertEqual(service.inspect_delivery(action["id"])["state"], "confirmed")
        request = eventually(lambda: self.pending_target(service), 60)
        service.decide(request, "approve", decision_id="target")
        eventually(lambda: work.snapshot().state == "delivered", 80)
        self.assertEqual(sum(kind == "pull" for kind, _ in self.peer.writes), 1)

    def test_batch_records_partial_expiration_and_replays_confirmed_decisions(self):
        import time

        service = self.start(auto_actions=[])
        service.work.submit("Fix the scoped value.")
        requests = eventually(
            lambda: (
                [r for r in service.requests() if r.kind == "delivery_authority"]
                if len(
                    [r for r in service.requests() if r.kind == "delivery_authority"]
                )
                == 2
                else None
            ),
            120,
        )
        batch = service.batch(requests, request_id="group")
        store = service.runtime.maintenance.store
        with store.db.write() as conn:
            row = store.get("decisions", requests[0].ref.id, conn)
            row["expires_at"] = time.time() - 1
            store.put(conn, "decisions", row)
        decisions = [{"id": r.ref.id, "decision": "approve"} for r in requests]
        first = service.decide_batch(batch["id"], decisions, request_id="decisions")
        self.assertEqual(
            first, service.decide_batch(batch["id"], decisions, request_id="decisions")
        )
        states = [r["state"] for r in first["results"].values()]
        self.assertEqual(sorted(states), ["confirmed", "expired"])
        self.assertEqual(service.batch(requests, request_id="group")["id"], batch["id"])

    def test_changed_target_after_approval_is_not_overwritten(self):
        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        request = eventually(lambda: self.pending_target(service), 120)
        before = (
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip()
        )
        tree = (
            self.run_git(
                "--git-dir=" + str(self.remote), "rev-parse", before + "^{tree}"
            )
            .decode()
            .strip()
        )
        other = (
            self.run_git(
                "--git-dir=" + str(self.remote),
                "commit-tree",
                tree,
                "-p",
                before,
                "-m",
                "external",
            )
            .decode()
            .strip()
        )
        self.run_git(
            "--git-dir=" + str(self.remote), "update-ref", "refs/heads/main", other
        )
        from homebrew_harness.contracts import InvalidInput

        try:
            service.decide(request, "approve", decision_id="target")
        except InvalidInput:
            # Observation can invalidate this exact request before approval;
            # the accepted-action path must also preserve the changed target.
            saved = service.runtime.maintenance.store.get("decisions", request.ref.id)
            self.assertEqual(saved["state"], "superseded")
            self.assertGreater(saved["revision"], request.ref.revision)
        eventually(
            lambda: any(
                a["state"] == "superseded" for a in service.records("actions")["items"]
            ),
            60,
        )
        self.assertEqual(
            self.run_git("--git-dir=" + str(self.remote), "rev-parse", "main")
            .decode()
            .strip(),
            other,
        )
        self.assertNotEqual(work.snapshot().state, "delivered")

    def test_public_comment_lost_ack_is_checked_by_fixed_correlation(self):
        from homebrew_harness.platforms.contracts import ObjectRef

        service = self.start()
        service.work.submit("Fix the scoped value.")
        eventually(lambda: self.pending_target(service), 120)
        row = service.records("changesets")["items"][0]
        subject = ObjectRef(
            "github", "primary", "example/project", "issue", "1", 1
        ).data()
        action = service.plan_delivery(
            row["id"],
            "post_comment",
            body="The fixed tests passed for this candidate.",
            subject=subject,
        )
        request = next(
            r
            for r in service.requests()
            if service.runtime.maintenance.store.get("decisions", r.ref.id).get(
                "action_id"
            )
            == action["id"]
        )
        self.peer.drop = "comment"
        service.decide(request, "approve", decision_id="comment")
        eventually(
            lambda: (
                service.delivery(action["id"], kind="actions")["state"] == "unknown"
            ),
            60,
        )
        self.peer.drop = None
        self.assertEqual(service.inspect_delivery(action["id"])["state"], "confirmed")
        self.assertEqual(sum(kind == "comment" for kind, _ in self.peer.writes), 1)

    def test_http_control_batch_and_text_ui_share_delivery_controls(self):
        import io
        from contextlib import redirect_stdout
        from homebrew_harness import RemoteService
        from homebrew_harness.services.cli import attach

        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        eventually(lambda: self.pending_target(service), 120)
        with RemoteService.from_root(self.control, control=True) as remote:
            request = next(
                r for r in remote.requests() if r.kind == "delivery_authority"
            )
            row = remote.delivery(work.snapshot().details["changeset_id"])
            lines = iter(
                [
                    "delivery list",
                    "delivery show " + row["id"],
                    "batch create --ids " + request.ref.id,
                    "exit",
                ]
            )
            with redirect_stdout(io.StringIO()) as output:
                attach(remote, input_fn=lambda _: next(lines))
            self.assertIn("merge_ready", output.getvalue())
            batch = remote.batch([request], request_id="http-batch")
            result = remote.decide_batch(
                batch["id"],
                [{"id": request.ref.id, "decision": "approve"}],
                request_id="http-decisions",
            )
            self.assertEqual(result["results"][request.ref.id]["state"], "confirmed")
            eventually(lambda: work.snapshot().state == "delivered", 60)

    def test_late_target_receipt_does_not_complete_reauthorized_work(self):
        service = self.start()
        work = service.work.submit("Fix the scoped value.")
        request = eventually(lambda: self.pending_target(service), 120)
        service.pause("all")
        delivery = service.runtime.maintenance.delivery
        row = delivery.store.get("changesets", work.snapshot().details["changeset_id"])
        pending = delivery.store.get("decisions", request.ref.id)
        action = delivery.store.update("actions", pending["action_id"], state="started")
        merged = self.peer.merge_pull(1)
        result = {
            "subject": row["change_request"],
            "head": row["candidate"]["commit"],
            "result_commit": merged,
            "state": "merged",
            "method": "merge",
        }
        with service.store.db.write() as conn:
            current = service.store.work(work.id)
            # A genuinely new accepted requirement version replaces this work;
            # a same-version authorization renewal now preserves its handoff.
            current["requirements_revision"] += 1
            service.runtime.maintenance.store.grant(
                conn, current, current["triage"], "operator:renewal"
            )
        with self.assertRaises(ValueError):
            delivery.store.current(row)
        delivery.store.settle_action(action, "confirmed", result)
        self.assertEqual(work.snapshot().state, "approved")
        self.assertIsNone(work.snapshot().details["run_id"])
        saved = delivery.store.get("changesets", row["id"])
        self.assertEqual(saved["state"], "merged_verifying")
        self.assertEqual(saved["merge"]["result_commit"], merged)
        self.assertEqual(service.snapshot().state, "paused")

    def test_same_version_authority_renewal_keeps_delivery_and_pause(self):
        service = self.start(merge={"mode": "manual", "poll_seconds": 0.1})
        work = service.work.submit("Complete the source change.")
        eventually(lambda: self.peer.pulls, 120)
        service.pause("all")
        before = service.store.work(work.id)
        with service.store.db.write() as conn:
            service.runtime.maintenance.store.grant(
                conn, service.store.work(work.id), before["triage"], "operator:renewal"
            )
        after = service.store.work(work.id)
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["changeset_id"], before["changeset_id"])
        self.assertEqual(after["progress_ref"], before["progress_ref"])
        self.assertEqual(service.snapshot().state, "paused")

    def test_lost_git_push_ack_is_checked_without_repeating_push(self):
        service = self.start()
        delivery = service.runtime.maintenance.delivery
        original = delivery.git.run
        pushes = []

        def lose_ack(args, **kw):
            result = original(args, **kw)
            if "push" in args:
                pushes.append(args)
                if len(pushes) == 1:
                    raise TimeoutError("Fixture lost push acknowledgment.")
            return result

        with patch.object(delivery.git, "run", side_effect=lose_ack):
            service.work.submit("Fix the scoped value.")
            action = eventually(
                lambda: next(
                    (
                        a
                        for a in service.records("actions")["items"]
                        if a["kind"] == "publish_branch" and a["state"] == "unknown"
                    ),
                    None,
                ),
                120,
            )
            self.assertEqual(
                service.inspect_delivery(action["id"], request_id="push-inspection")[
                    "state"
                ],
                "confirmed",
            )
            self.assertEqual(
                service.inspect_delivery(action["id"], request_id="push-inspection")[
                    "state"
                ],
                "confirmed",
            )
            eventually(lambda: self.pending_target(service), 80)
            self.assertEqual(len(pushes), 1)
