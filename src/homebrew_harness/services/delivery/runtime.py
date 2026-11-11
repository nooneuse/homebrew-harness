"""Fixed delivery progression; model jobs supply evidence, never execute writes."""

import json
import threading
import time
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.execution.test_runner import load_test_runner
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
            capabilities = self.adapter.capabilities()
            if capabilities.get("merge_observation") is not True:
                raise ValueError(
                    "Delivery adapter cannot observe native merge outcomes."
                )
            if (
                self.spec["merge"]["mode"] == "service_approved"
                and self.spec["merge"]["method"] not in capabilities["merge_methods"]
            ):
                raise ValueError(
                    "Delivery adapter does not support the selected native merge method."
                )
            self.git = GitBackend(runtime.store.root, self.spec, self.values)
        except BaseException:
            self.adapter.close()
            raise
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.started = False
        self.error = None
        self.preparing = False
        from homebrew_harness.execution.execution_leases import ExecutionLeases
        from homebrew_harness.services.resources import TestResources

        self.test_leases = ExecutionLeases(runtime.store.root)
        self.tests = TestResources(self.test_leases, "delivery-tests")

    @property
    def test_runner(self):
        return self.tests.runner

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
                        " AND coalesce(json_extract(value,'$.observation_terminal'),0)=0 AND json_extract(value,'$.state') IN ('queued','preparing','validating','reviewing','review_branch_ready','ci_waiting','review_waiting','repair_approved','merge_ready','merge_pending','merged_verifying','integration_required','closed_unmerged','verification_blocked','superseded','ci_diagnosing','repairing','repair_waiting','repair_waiting_authority','quality_failed','blocked')"
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
                from homebrew_harness.runtime.runtime_control import ClosePending

                try:
                    self.tests.settle()
                except ClosePending:
                    continue
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
        from .merge_flow import OBSERVING, PASSIVE, observe, verify_result

        if row["state"] == "merged_verifying":
            verify_result(self, row)
            return
        if row.get("change_request") and row["state"] in OBSERVING | PASSIVE:
            if observe(self, row):
                return
            if row["state"] in OBSERVING and row["state"] not in {
                "ci_waiting",
                "review_waiting",
            }:
                return
        if row["state"] not in {
            "queued",
            "preparing",
            "validating",
            "review_branch_ready",
            "ci_waiting",
            "review_waiting",
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
            from .quality import verify

            checked = verify(self, row["candidate"], work)
            if not checked["ok"]:
                self.store.update(
                    "changesets",
                    row["id"],
                    state="quality_failed",
                    quality=checked["quality"],
                    goal_evidence=checked["goal_evidence"],
                    error="candidate_goal_failed",
                )
                return
            row = self.store.update(
                "changesets",
                row["id"],
                quality=checked["quality"],
                goal_evidence=checked["goal_evidence"],
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
                        row["id"] + ":review:" + row["candidate"]["commit"],
                    )
                else:
                    row["frozen"] = self.store.freeze(row)
                    self.store.put(conn, "changesets", row)
            return
        if row["state"] == "review_branch_ready":
            self.git.verify(row["candidate"])
            with self.ms.db.write() as conn:
                self.store.action(conn, row, "publish_branch")
                self.store.action(conn, row, "ensure_change_request")
            return
        if (
            row["state"] in {"ci_waiting", "review_waiting"}
            and row.get("next_ci", 0) <= time.time()
        ):
            self.git.verify(row["candidate"])
            try:
                feedback = self.adapter.feedback(row["change_request"])
            except Exception as error:
                from homebrew_harness.execution.execution_control import cleanup_scope

                with cleanup_scope():
                    self.store.read_failure(row["id"], "feedback", error)
                return
            self.store.clear_read_failure(row["id"], "feedback")
            if (
                feedback["head"] != row["candidate"]["commit"]
                or feedback["base_branch"] != self.spec["target_branch"]
                or feedback["state"] != "open"
                or feedback["merged"]
            ):
                raise StaleDelivery(
                    "Change request points to a different candidate or target."
                )
            from .merge_flow import gate

            evaluation = gate(feedback, self.spec)
            ready, failed = evaluation["ci_ready"], evaluation["failed"]
            review_gate = evaluation["review_gate"]
            row = self.store.update(
                "changesets",
                row["id"],
                ci=feedback,
                review_gate=review_gate,
                next_ci=time.time() + self.spec["ci_poll_seconds"],
            )
            if ready and not review_gate["ready"]:
                self.store.update(
                    "changesets",
                    row["id"],
                    state="review_waiting",
                    error="review_authority_required",
                )
            elif ready:
                if (
                    feedback["draft"]
                    or feedback["mergeable"] is not True
                    or feedback["mergeable_state"]
                    in {"blocked", "behind", "dirty", "draft", "unknown"}
                ):
                    self.store.update(
                        "changesets",
                        row["id"],
                        state="review_waiting",
                        error="platform_merge_waiting",
                    )
                    return
                if feedback["base"] != row["candidate"]["target_base"]:
                    self.store.update(
                        "changesets",
                        row["id"],
                        state="integration_required",
                        error="target_base_changed",
                    )
                    return
                if self.spec["merge"]["mode"] == "service_approved":
                    try:
                        evidence = self.adapter.merge_gate(self.spec["merge"]["method"])
                    except Exception as error:
                        self.store.update(
                            "changesets",
                            row["id"],
                            state="review_waiting",
                            error="native_merge_policy_unverified",
                            error_detail=redact(str(error)),
                        )
                        return
                    row = self.store.update(
                        "changesets", row["id"], merge_rules=evidence
                    )
                row = self.store.update(
                    "changesets", row["id"], state="merge_ready", next_merge=0
                )
                if self.spec["merge"]["mode"] == "service_approved":
                    with self.ms.db.write() as conn:
                        self.store.action(conn, row, "merge_change_request")
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
        from .spec import ACTIONS

        if action["kind"] not in ACTIONS:
            self.store.update(
                "actions",
                action["id"],
                state="superseded",
                error="unsupported_delivery_action",
            )
            return
        try:
            row = self.store.get("changesets", action["changeset_id"])
            self.store.current(row)
            self.git.verify(row["candidate"])
            if (
                action["kind"] == "ensure_change_request"
                and row.get("review_commit") != row["candidate"]["commit"]
            ):
                return
            if action["kind"] == "merge_change_request":
                from .merge_flow import gate

                observed = self.adapter.feedback(row["change_request"])
                evaluation = gate(observed, self.spec)
                if observed["merged"]:
                    result = self.adapter.inspect_merge(action["plan"])
                    self.store.record_merge(row["id"], result)
                    self.store.invalidate_merges(row["id"])
                    return
                if (
                    evaluation["hash"] != action["plan"]["gate_hash"]
                    or not evaluation["ci_ready"]
                    or not evaluation["review_gate"]["ready"]
                ):
                    self.store.update(
                        "changesets",
                        row["id"],
                        ci=observed,
                        state="ci_waiting",
                        next_ci=0,
                    )
                    self.store.invalidate_merges(row["id"])
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
                previous = row.get("publish_base") or (
                    self.store.get("changesets", row["previous"])["candidate"]["commit"]
                    if row.get("previous")
                    else None
                )
                result = self.git.publish(row["candidate"], row["branch"], previous)
            elif action["kind"] == "merge_change_request":
                result = self.adapter.merge_change_request(plan)
            elif action["kind"] == "ensure_change_request":
                result = self.adapter.ensure_change_request(plan)
            else:
                result = self.adapter.post_comment(plan)
            pending = (
                action["kind"] == "merge_change_request"
                and result.get("state") == "pending"
            )
            self.store.settle_action(
                accepted,
                "unknown" if pending else "confirmed",
                result,
                error="external_merge_pending" if pending else None,
            )
        except StaleDelivery as error:
            self.store.settle_action(accepted, "superseded", error=type(error).__name__)
        except Exception as error:
            from homebrew_harness.services.action_runtime import failed_state

            self.store.settle_action(
                accepted, failed_state(error), error=type(error).__name__
            )

    def reconcile(self, action):
        from .spec import ACTIONS

        if action["kind"] not in ACTIONS:
            return self.store.settle_action(
                action, "unknown", error="inspect_with_the_original_runtime"
            )
        row = self.store.get("changesets", action["changeset_id"])
        plan = {**action["plan"], "correlation": action["id"]}
        try:
            if action["kind"] == "publish_branch":
                result = (
                    {"confirmed": True, "ref": row["branch"], "commit": plan["commit"]}
                    if self.git.ref(row["branch"]) == plan["commit"]
                    else None
                )
            elif action["kind"] == "merge_change_request":
                result = self.adapter.inspect_merge(plan)
            elif action["kind"] == "ensure_change_request":
                result = self.adapter.ensure_change_request(plan, reconcile=True)
            else:
                result = self.adapter.post_comment(plan, reconcile=True)
            return (
                self.store.settle_action(
                    action,
                    "unknown" if result.get("state") == "pending" else "confirmed",
                    result,
                )
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
                self.git.verify(row["integration"])
                output = row["integration"]["files"]
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
        self.tests.request_close()
        if not self.started:
            self.finished.set()
        self.finished.wait()
        self.tests.settle()
        self.test_leases.close()
        self.adapter.close()
        self.git.close()
