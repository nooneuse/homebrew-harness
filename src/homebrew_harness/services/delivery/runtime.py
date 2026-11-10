"""Fixed delivery progression; model jobs supply evidence, never execute writes."""

import json
import threading
import time
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.orchestration.hard_conditions import HardVerifier
from homebrew_harness.execution.test_runner import (
    load_test_runner,
    test_context,
)
from homebrew_harness.services.protocol import request_key, fingerprint
from homebrew_harness.security.data_boundary import redact
from .spec import read_values
from .git import GitBackend, StaleDelivery
from .store import DeliveryStore


class Delivery:
    def __init__(self, runtime, maintenance, environment):
        self.runtime, self.ms, self.spec = runtime, maintenance, runtime.spec.delivery
        self.store = DeliveryStore(runtime, maintenance, self.spec)
        maintenance.delivery = self
        self.values = read_values(
            self.spec, environment, (runtime.store.root, runtime.spec.workspace)
        )
        settings = runtime.core.config.settings
        if not settings.test_config:
            raise ValueError("Delivery needs a host test registry.")
        probe = load_test_runner(settings.test_config, runtime.store.root)
        try:
            if set(self.spec["required_tests"]) - set(probe.recipes):
                raise ValueError("Delivery required tests are not registered.")
            if self.spec["mode"] == "isolated" and probe.backend.name != "container":
                raise ValueError(
                    "Credentialed delivery requires isolated candidate tests."
                )
        finally:
            probe.close()
        if (
            "bash" in runtime.spec.run_spec("Validate delivery").tools
            or runtime.spec.run_defaults.get("mcp")
            or set(runtime.spec.run_defaults.get("enabled", ()))
            & {"background", "worktrees"}
        ):
            raise ValueError(
                "Delivery coding scope cannot execute unconfined host commands or MCP."
            )

        from homebrew_harness.platforms.registry import PROVIDERS

        self.adapter = PROVIDERS[self.spec["provider"]]["delivery"](
            self.spec, self.values
        )
        try:
            self.git = GitBackend(runtime.store.root, self.spec, self.values)
        except BaseException:
            self.adapter.close()
            raise
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.started = False
        self.error = None
        self.preparing = False
        self.test_runner = None

    def identity(self):
        return {
            "policy": fingerprint(self.spec),
            "git": self.git.executable,
            "values": fingerprint(
                {
                    k: v
                    for k, v in self.values.items()
                    if k not in {"api_token", "git_token"}
                }
            ),
        }

    def baseline(self, work, child):
        if work.get("baseline"):
            return work
        files = snapshot(child.workdir, **work["run_spec"]["snapshot_limits"])
        return self.runtime.store.update_work(
            self.runtime.header,
            work["id"],
            baseline={"files": files, "head": self.git.head(child.workdir)},
        )

    def rows(self, kind, *, active=False):
        with self.ms.db.connection() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT value FROM records WHERE section=? AND owner=?"
                    + (
                        " AND json_extract(value,'$.state') IN ('queued','preparing','validating','review_branch_ready','ci_waiting','repair_approved')"
                        if active and kind == "changesets"
                        else " AND json_extract(value,'$.state') IN ('queued','started')"
                        if active
                        else ""
                    )
                    + " ORDER BY rowid",
                    ("service_" + kind, "service:" + self.ms.id),
                )
            ]

    def loop(self):
        self.started = True
        try:
            while not self.stop.wait(0.1):
                meta = self.runtime.store.get(self.ms.id)
                if meta["state"] == "stopping":
                    return
                if meta["state"] != "running":
                    continue
                for row in self.rows("changesets", active=True):
                    if self.stop.is_set():
                        break
                    try:
                        with control_scope(Deadline.after(self.spec["gate_seconds"])):
                            self.advance(row)
                    except StaleDelivery as error:
                        self.store.update(
                            "changesets",
                            row["id"],
                            state="superseded",
                            error=type(error).__name__,
                        )
                    except Exception as error:
                        self.store.update(
                            "changesets",
                            row["id"],
                            state="failed",
                            error=type(error).__name__,
                            error_detail=redact(str(error)),
                        )
                for action in self.rows("actions", active=True):
                    if self.stop.is_set():
                        break
                    if action["state"] == "queued":
                        if action["expires_at"] <= time.time():
                            self.store.expire_action(action)
                            continue
                        with control_scope(Deadline.after(self.spec["gate_seconds"])):
                            self.execute(action)
                    elif action["state"] == "started":
                        with control_scope(Deadline.after(self.spec["gate_seconds"])):
                            self.reconcile(action)
                self.sync_pending()
        except Exception as error:
            self.error = type(error).__name__
        finally:
            self.finished.set()

    def advance(self, row):
        if row["state"] not in {
            "queued",
            "preparing",
            "validating",
            "review_branch_ready",
            "ci_waiting",
            "repair_approved",
        }:
            return
        work = self.store.current(row)
        if row["state"] in {"queued", "preparing"}:
            if not work.get("baseline"):
                raise ValueError("Work has no frozen input baseline.")
            with self.runtime.lock:
                active = self.runtime.store.next_work(self.ms.id)
                if active and active["state"] == "in_progress":
                    return
                self.preparing = True
                row = self.store.update(
                    "changesets",
                    row["id"],
                    state="preparing",
                    attempts=row["attempts"] + 1,
                )
                workspace = self.runtime.child_for(work).workdir
            try:
                previous = (
                    self.store.get("changesets", row["previous"])["candidate"]
                    if row.get("previous")
                    else None
                )
                directory = (
                    self.runtime.store.root
                    / ".harness/delivery/candidates"
                    / row["id"]
                    / str(row["attempts"])
                )
                candidate = self.git.prepare(
                    work, workspace, work["baseline"], directory, previous=previous
                )
                expected = (
                    self.store.get("changesets", row["previous"]).get(
                        "primary_expected"
                    )
                    if row.get("previous")
                    else candidate["files"]
                    if row.get("source_workspace", self.runtime.spec.workspace)
                    == self.runtime.spec.workspace
                    else work["baseline"]["files"]
                )
                with self.runtime.lock:
                    self.store.current(row)
                    self.store.update(
                        "changesets",
                        row["id"],
                        candidate=candidate,
                        state="validating",
                        primary_expected=expected,
                    )
            finally:
                with self.runtime.lock:
                    self.preparing = False
            return
        if row["state"] == "validating":
            self.git.verify(row["candidate"])
            runner = load_test_runner(
                self.runtime.core.config.settings.test_config,
                Path(row["candidate"]["workspace"]),
            )
            self.test_runner = runner
            receipts = []
            try:
                for name in self.spec["required_tests"]:
                    if self.stop.is_set():
                        return
                    runner.approve(
                        name,
                        cwd=Path(row["candidate"]["workspace"]),
                        role="lead",
                        max_runs=1,
                    )
                    with test_context("lead"):
                        receipt = runner.run(name)
                    if (
                        receipt["status"] != "passed"
                        or not receipt["source_stable"]
                        or not runner.verify(name, Path(row["candidate"]["workspace"]))[
                            "ok"
                        ]
                    ):
                        raise ValueError(
                            "Candidate test did not produce fresh passing evidence."
                        )
                    receipts.append(
                        {
                            k: v
                            for k, v in receipt.items()
                            if k
                            not in {
                                "stdout",
                                "stderr",
                                "stdout_preview",
                                "stderr_preview",
                                "inputs",
                            }
                        }
                    )
                goal = work["run_spec"].get("goal")
                goal_evidence = (
                    HardVerifier(
                        row["candidate"]["workspace"], test_runner=runner
                    ).check(goal)
                    if goal
                    else None
                )
                if goal_evidence and not goal_evidence["ok"]:
                    self.store.update(
                        "changesets",
                        row["id"],
                        state="quality_failed",
                        quality=receipts,
                        goal_evidence=goal_evidence,
                        error="candidate_goal_failed",
                    )
                    return
                self.git.verify(row["candidate"])
                row = self.store.update(
                    "changesets",
                    row["id"],
                    quality=receipts,
                    goal_evidence=goal_evidence,
                    state="reviewing"
                    if self.spec["require_review"]
                    else "review_branch_ready",
                    review=None
                    if self.spec["require_review"]
                    else {"text": "Host did not require model review.", "findings": []},
                )
                with self.ms.db.write() as conn:
                    if self.spec["require_review"]:
                        self.ms.job(
                            conn,
                            "review",
                            row["id"],
                            row["revision"],
                            {},
                            row["id"] + ":review",
                        )
                    else:
                        row["frozen"] = self.store.freeze(row)
                        self.store.put(conn, "changesets", row)
            finally:
                runner.close()
                runner.wait_closed(None)
                self.test_runner = None
            return
        if row["state"] == "review_branch_ready":
            self.git.verify(row["candidate"])
            with self.ms.db.write() as conn:
                self.store.action(conn, row, "publish_branch")
                self.store.action(conn, row, "ensure_change_request")
            return
        if row["state"] == "ci_waiting" and row.get("next_ci", 0) <= time.time():
            self.git.verify(row["candidate"])
            feedback = self.adapter.feedback(row["change_request"])
            if (
                feedback["head"] != row["candidate"]["commit"]
                or feedback["base_branch"] != self.spec["target_branch"]
                or feedback["state"] != "open"
                or feedback["merged"]
            ):
                raise StaleDelivery(
                    "Change request points to a different candidate or target."
                )
            checks = {}
            for check in feedback["checks"]:
                if (
                    check["head"] == feedback["head"]
                    and check["app_id"] in self.spec["check_app_ids"]
                    and check["name"] in self.spec["required_checks"]
                ):
                    checks.setdefault(check["name"], []).append(check)
            failed = any(
                c["status"] == "completed" and c["conclusion"] not in {"success"}
                for group in checks.values()
                for c in group
            )
            ready = all(
                name in checks
                and all(
                    c["status"] == "completed" and c["conclusion"] == "success"
                    for c in checks[name]
                )
                for name in self.spec["required_checks"]
            )
            row = self.store.update(
                "changesets",
                row["id"],
                ci=feedback,
                next_ci=time.time() + self.spec["ci_poll_seconds"],
            )
            if ready:
                if (
                    self.git.ref(self.spec["target_branch"])
                    != row["candidate"]["target_base"]
                ):
                    raise StaleDelivery("Delivery target baseline changed during CI.")
                row = self.store.update("changesets", row["id"], state="merge_ready")
                with self.ms.db.write() as conn:
                    self.store.action(conn, row, "publish_target")
            elif failed:
                row = self.store.update("changesets", row["id"], state="ci_diagnosing")
                with self.ms.db.write() as conn:
                    self.ms.job(
                        conn,
                        "ci_triage",
                        row["id"],
                        row["revision"],
                        {},
                        row["id"] + ":ci:" + fingerprint(feedback),
                    )
            elif (
                time.time() - row.get("ci_started", time.time())
                > self.spec["ci_wait_seconds"]
            ):
                self.store.update(
                    "changesets", row["id"], state="blocked", error="ci_wait_expired"
                )
            return
        if row["state"] == "repair_approved":
            self.repair(row, work)

    def execute(self, action):
        try:
            row = self.store.get("changesets", action["changeset_id"])
            self.store.current(row)
            self.git.verify(row["candidate"])
            if (
                action["kind"] == "ensure_change_request"
                and row.get("review_commit") != row["candidate"]["commit"]
            ):
                return
            if action["kind"] == "publish_target":
                if self.spec["mode"] == "isolated" and self.spec["required_checks"]:
                    self.adapter.target_guard()
                observed = self.adapter.feedback(row["change_request"])
                if fingerprint(observed) != action["plan"].get("ci_hash"):
                    self.store.update(
                        "changesets",
                        row["id"],
                        ci=observed,
                        state="ci_waiting",
                        next_ci=0,
                    )
                    self.store.update(
                        "actions", action["id"], state="superseded", error="ci_changed"
                    )
                    return
            accepted = self.store.start_action(action)
        except StaleDelivery as error:
            self.store.update(
                "actions", action["id"], state="superseded", error=type(error).__name__
            )
            return
        except Exception:
            return
        plan = {**accepted["plan"], "correlation": accepted["id"]}
        try:
            if action["kind"] == "publish_branch":
                previous = (
                    self.store.get("changesets", row["previous"])["candidate"]["commit"]
                    if row.get("previous")
                    else None
                )
                result = self.git.publish(row["candidate"], row["branch"], previous)
            elif action["kind"] == "publish_target":
                result = self.git.publish(
                    row["candidate"],
                    self.spec["target_branch"],
                    row["candidate"]["target_base"],
                )
            elif action["kind"] == "ensure_change_request":
                result = self.adapter.ensure_change_request(plan)
            else:
                result = self.adapter.post_comment(plan)
            self.store.settle_action(accepted, "confirmed", result)
        except StaleDelivery as error:
            self.store.settle_action(accepted, "superseded", error=type(error).__name__)
        except Exception as error:
            self.store.settle_action(accepted, "unknown", error=type(error).__name__)

    def reconcile(self, action):
        row = self.store.get("changesets", action["changeset_id"])
        plan = {**action["plan"], "correlation": action["id"]}
        try:
            if action["kind"] in {"publish_branch", "publish_target"}:
                branch = (
                    row["branch"]
                    if action["kind"] == "publish_branch"
                    else self.spec["target_branch"]
                )
                result = (
                    {"confirmed": True, "ref": branch, "commit": plan["commit"]}
                    if self.git.ref(branch) == plan["commit"]
                    else None
                )
            elif action["kind"] == "ensure_change_request":
                result = self.adapter.ensure_change_request(plan, reconcile=True)
            else:
                result = self.adapter.post_comment(plan, reconcile=True)
            return (
                self.store.settle_action(action, "confirmed", result)
                if result
                else self.store.settle_action(
                    action, "unknown", error="unconfirmed_external_result"
                )
            )
        except Exception:
            return self.store.settle_action(
                action, "unknown", error="inspection_required"
            )

    def repair(self, row, work):
        if work.get("ci_fix_attempts", 0) >= self.spec["ci_fix_limit"]:
            raise ValueError("CI repair limit exhausted.")
        if not self.ms.valid_authority(work):
            with self.ms.db.write() as conn:
                current = self.store.get("changesets", row["id"], conn)
                current.update(
                    state="repair_waiting_authority", revision=current["revision"] + 1
                )
                self.store.put(conn, "changesets", current)
                self.ms.work_request(conn, work, work["triage"], renew=True)
            return
        self.git.verify(row["candidate"])
        count = work.get("ci_fix_attempts", 0) + 1
        directory = (
            self.runtime.store.root
            / ".harness/delivery/repairs"
            / row["id"]
            / str(count)
            / __import__("uuid").uuid4().hex
        )
        directory.mkdir(parents=True, exist_ok=True)
        import shutil

        workspace = directory / "workspace"
        shutil.copytree(
            row["candidate"]["workspace"],
            workspace,
            ignore=shutil.ignore_patterns(".harness", ".git"),
        )
        repo = directory / "repo.git"
        self.git.run(["init", "--bare", "--template=", str(repo)])
        self.git.run(
            [
                "--git-dir=" + str(repo),
                "fetch",
                "--no-tags",
                row["candidate"]["repo"],
                row["candidate"]["commit"],
            ]
        )
        self.git.run(
            [
                "--git-dir=" + str(repo),
                "update-ref",
                "refs/heads/candidate",
                row["candidate"]["commit"],
            ]
        )
        self.git.run(
            ["--git-dir=" + str(repo), "symbolic-ref", "HEAD", "refs/heads/candidate"]
        )
        self.git.run(["--git-dir=" + str(repo), "config", "core.bare", "false"])
        self.git.run(
            ["--git-dir=" + str(repo), "config", "core.worktree", str(workspace)]
        )
        (workspace / ".git").write_text("gitdir: " + str(repo) + "\n")
        from homebrew_harness.runs.spec import RunSpec

        spec = RunSpec(
            **{
                **work["run_spec"],
                "prompt": work["run_spec"]["prompt"]
                + "\n\nConfirmed CI repair plan:\n"
                + row["ci_diagnosis"]["plan"],
                "total_seconds": min(
                    work["run_spec"]["total_seconds"],
                    work["authority"]["expires_at"] - time.time(),
                ),
            }
        )
        with self.ms.db.write() as conn:
            current = self.store.current(row, conn)
            if (
                not self.ms.valid_authority(current)
                or current.get("ci_fix_attempts", 0) != count - 1
            ):
                raise ValueError(
                    "CI repair authority or attempt changed during preparation."
                )
            current.update(
                state="approved",
                run_workspace=str(workspace),
                run_history=[*current.get("run_history", []), current["run_id"]],
                run_id=None,
                run_spec=spec.data(),
                launch_id="service_"
                + request_key("ci-run", self.ms.id, work["id"] + ":" + str(count)),
                ci_fix_attempts=count,
                repair_of=row["id"],
                baseline=None,
                pause_requested=False,
                revision=current["revision"] + 1,
            )
            current.pop("result", None)
            self.ms.db._put(conn, "service_work", work["id"], current)
            row.update(state="repairing")
            self.store.put(conn, "changesets", row)
            self.ms.changed(conn, work["id"])

    def has_pending_sync(self):
        with self.ms.db.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.workspace_sync.state') IN ('pending','conflict') LIMIT 1",
                    ("service:" + self.ms.id,),
                ).fetchone()
                is not None
            )

    def sync_pending(self):
        import hashlib
        from homebrew_harness.security.safe_files import SafeFile
        from homebrew_harness.execution.execution_leases import (
            file_update_batch,
            file_update_lease,
        )
        from homebrew_harness.services.maintenance_spec import within

        with self.runtime.lock:
            active = self.runtime.store.next_work(self.ms.id)
            if active and active["state"] == "in_progress":
                return False
            with self.ms.db.connection() as conn:
                pending = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT value FROM records WHERE section='service_work' AND owner=? AND json_extract(value,'$.workspace_sync.state') IN ('pending','conflict')",
                        ("service:" + self.ms.id,),
                    )
                ]
            if not pending:
                return True
            self.preparing = True
        try:
            root = Path(self.runtime.spec.workspace)
            for work in pending:
                row = self.store.get(
                    "changesets", work["workspace_sync"]["changeset_id"]
                )
                expected = row.get("primary_expected")
                if expected is None:
                    raise ValueError(
                        "Missing original workspace synchronization precondition."
                    )
                self.git.verify(row["candidate"])
                output = row["candidate"]["files"]
                changed = {
                    n
                    for n in set(expected) | set(output)
                    if (
                        (expected.get(n) or {}).get("sha256"),
                        (expected.get(n) or {}).get("executable"),
                    )
                    != (
                        (output.get(n) or {}).get("sha256"),
                        (output.get(n) or {}).get("executable"),
                    )
                }
                if any(not within(n, row["write_paths"]) for n in changed):
                    raise ValueError("Synchronization exceeds original work scope.")
                conflicts = []
                with file_update_batch(root):
                    for name in sorted(changed):
                        with (
                            file_update_lease(root, name),
                            SafeFile(root, name, create_parents=True) as target,
                        ):
                            raw, version, mode = target.snapshot()
                            actual = (
                                hashlib.sha256(raw).hexdigest()
                                if raw is not None
                                else None
                            )
                            new = output.get(name)
                            if (actual, bool((mode or 0) & 0o111)) == (
                                (new or {}).get("sha256"),
                                bool((new or {}).get("executable")),
                            ):
                                continue
                            if (actual, bool((mode or 0) & 0o111)) != (
                                (expected.get(name) or {}).get("sha256"),
                                bool((expected.get(name) or {}).get("executable")),
                            ):
                                conflicts.append(name)
                                continue
                            if new is None:
                                target.unlink(version)
                            else:
                                with SafeFile(
                                    Path(row["candidate"]["workspace"]), name
                                ) as source:
                                    data = source.snapshot()[0]
                                if (
                                    data is None
                                    or hashlib.sha256(data).hexdigest() != new["sha256"]
                                ):
                                    raise ValueError(
                                        "Synchronization candidate changed."
                                    )
                                target.publish(
                                    data, version, 0o755 if new["executable"] else 0o644
                                )
                state = {
                    "state": "conflict" if conflicts else "synchronized",
                    "changeset_id": row["id"],
                    "paths": conflicts,
                }
                self.runtime.store.update_work(
                    self.runtime.header, work["id"], workspace_sync=state
                )
                if conflicts:
                    return False
        finally:
            with self.runtime.lock:
                self.preparing = False
        return True

    def close(self):
        self.stop.set()
        self.git.stop.set()
        if self.test_runner:
            self.test_runner.close()
        if not self.started:
            self.finished.set()
        self.finished.wait()
        self.adapter.close()
        self.git.close()
