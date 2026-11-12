"""A single input owner delegates execution to Session and managed Run owners."""

import threading
import time

from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.model_gateway import Deadline
from homebrew_harness.runs.spec import TERMINAL
from homebrew_harness.runs.store import RunStore
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.runtime.input_router import (
    ApprovalBroker,
    approval_scope,
    short_decision,
)
from homebrew_harness.runtime.interaction_state import InteractionState
from homebrew_harness.runtime.runtime_control import Shutdown, Workers
from homebrew_harness.runtime.task_routing import (
    TaskRoute,
    TaskRouter,
    validate_route,
    load_task_reference,
    add_task_reference,
)
from homebrew_harness.security.data_boundary import redact


class InteractionHost:
    def __init__(
        self,
        assembler,
        *,
        trusted=False,
        announce=None,
        router=None,
        supervisor_factory=None,
    ):
        self.assembler, self.config = assembler, assembler.config
        self.workdir = assembler.workdir
        self.state, self.runs = InteractionState(self.workdir), RunStore(self.workdir)
        self.trusted, self.announce = trusted, announce or (lambda value: None)
        self._control_only = False
        preference = self.state.runtime.get("interaction_preferences", "workspace", {})
        models = getattr(assembler, "planning_models", None)
        if preference and models and self.config.model_selection is None:
            if preference.get("base_binding") != models._choice_source_binding.identity() or preference.get("models") != models.registry.manifest():
                raise ValueError("Saved interactive model choice requires its original host configuration.")
            assembler.set_model_selection(preference["choice"])
            self.config = assembler.config
        self.router = router or TaskRouter(
            assembler.binding,
            assembler.gateway,
            allowed=assembler.allowed,
            registry=assembler.registry,
            context=getattr(assembler, "planning_context", None),
            model_planning=self.config.model_planning
            and not self.config.prefer_session,
        )
        self.supervisor_factory = supervisor_factory
        self.lock = threading.RLock()
        self.workers, self.shutdown = Workers(), Shutdown()
        self.leases = ExecutionLeases(self.workdir)
        self.session = self.broker = self.supervisor = None
        self.closed = False
        self.wake = threading.Event()
        self._busy = False
        self.focus = None
        self._request_view = None
        self.observation_errors = []
        try:
            self.leases.acquire("interaction-host")
            if self.runs.runtime.backend().exists():
                existing = [
                    v
                    for v in self.runs.runtime.items("runs").values()
                    if v["status"] not in TERMINAL or v.get("finalized_at") is None
                ]
                if existing:
                    self.state.adopt_run(existing[0], self.configuration())
            self._kick()
        except BaseException:
            self.leases.close()
            assembler.close()
            self.runs.close()
            raise

    def configuration(self):
        return {
            "settings": self.config.identity(),
            "resources": getattr(self.assembler, "prepared_identity", {}),
        }

    def _announce(self, value):
        try:
            self.announce(redact(value))
        except Exception as error:
            self.observation_errors.append(type(error).__name__)
            self.observation_errors[:] = self.observation_errors[-20:]
            return False
        else:
            if (
                self.broker
                and isinstance(value, str)
                and value.startswith("[permission]")
            ):
                self._set_focus(self.requests())
            return True

    def submit(self, text, *, mode="auto", target="lead", input_id=None, literal=False):
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 64000
            or redact(text) != text
        ):
            raise ValueError(
                "Input must be nonempty, nonprivate text within 64,000 characters."
            )
        if mode not in {"auto", "steer", "queue", "interrupt"}:
            raise ValueError("Unknown interaction input mode.")
        with self.lock:
            if self.closed:
                raise ValueError("Interaction host is closing.")
            self._sync_model_preference()
            active = self.state.active()
            if active and (active.get("target") or {}).get("kind") == "run":
                self.status(active["id"])
            if mode == "auto" and not literal:
                control = self._command(text)
                if control is not None:
                    return control
                decision = short_decision(text)
                requests = self.requests()
                focused = len(requests) == 1 and self.focus == self._focus_key(
                    requests[0]
                )
                if focused and requests[0]["kind"] == "attention":
                    return self.respond(
                        requests[0]["id"],
                        requests[0]["revision"],
                        "answer",
                        answer=text,
                    )
                if decision is not None:
                    if not focused or requests[0]["kind"] != "tool":
                        raise ValueError(
                            "Use an exact request ID; no unique tool approval is in focus."
                        )
                    return self.respond(
                        requests[0]["id"], requests[0]["revision"], decision
                    )
            task = self.state.active()
            if (
                task
                and target != "lead"
                and (
                    not self.session
                    or not self.session.team
                    or target not in self.session.members
                )
            ):
                raise ValueError("Target must belong to the current interactive team.")
            if task and task.get("history_only"):
                self.state.update(task["id"], history_only=False)
            record = self.state.accept_input(
                text, mode, target, input_id, literal=literal
            )
            if task and not task.get("target") and task["state"] == "blocked":
                self.state.update(task["id"], state="queued", error=None)
            self._deliver_pending()
            self.wake.set()
            self._kick()
            return {
                "input_id": record["id"],
                "task_id": record["task_id"],
                "state": "accepted",
            }

    def _model_registry(self):
        from homebrew_harness.execution.model_registry import ModelRegistry
        models = getattr(self.assembler, "planning_models", None)
        return models.registry if models is not None else self.session.models if self.session else ModelRegistry()

    def _set_model_preference(self, choice, *, persist=True):
        from homebrew_harness.runtime.model_selection import validate_choice
        choice = validate_choice(choice)
        setter = getattr(self.assembler, "set_model_selection", None)
        if setter is None:
            raise ValueError("This custom host does not provide model selection.")
        previous = (self.config, self.assembler.config, self.assembler.binding, self.assembler.gateway,
            self.assembler.prepared_identity["model"], self.router.binding, self.router.gateway)
        try:
            binding = setter(choice)
            self.config = self.assembler.config
            self.router.binding, self.router.gateway = binding, self.assembler.gateway
            if persist:
                models = self.assembler.planning_models
                self.state.runtime.put("interaction_preferences", "workspace", {
                    "base_binding": models._choice_source_binding.identity(),
                    "models": models.registry.manifest(), "choice": choice})
        except Exception:
            saved = self.state.runtime.get("interaction_preferences", "workspace", {})
            if saved.get("choice") == choice:
                raise ValueError("Model preference was saved, but confirmation failed; inspect /model.") from None
            self.config, self.assembler.config, self.assembler.binding, self.assembler.gateway, identity, self.router.binding, self.router.gateway = previous
            self.assembler.prepared_identity["model"] = identity
            raise
        return binding

    def _sync_model_preference(self, session=None):
        session = session or self.session
        if session is None or not getattr(session, "_committed_model_revision", 0):
            return
        marker = (session.session_id, session._committed_model_revision)
        if marker == getattr(self, "_seen_model_revision", None):
            return
        with self.lock:
            choice = session._committed_model_selection
            if choice != self.config.model_selection:
                self._set_model_preference(choice)
            task = self.state.active()
            if task and (task.get("target") or {}).get("id") == session.session_id and task.get("config") != self.configuration():
                self.state.update(task["id"], config=self.configuration())
            self._seen_model_revision = marker

    def _command_session(self, *, history=False):
        from dataclasses import replace
        from homebrew_harness.runtime.task_routing import TaskRoute
        task = None
        if history:
            last = self.state.runtime.get("interaction_hosts", "workspace", {}).get("last")
            task = self.state.get(last) if last else None
            if not task or (task.get("target") or {}).get("kind") != "session":
                return None
        if task:
            # Restore with the original configuration before applying the frontend choice.
            config = replace(self.config, model_selection=None)
            from homebrew_harness.runtime.interaction_config import InteractionAssembler
            builder = InteractionAssembler(self.workdir, config,
                environment=getattr(self.assembler, "environment", None),
                client_factory=getattr(self.assembler, "factory", None),
                model_provider=getattr(self.assembler, "model_provider", None),
                workflow_registry=self.assembler.registry)
            try:
                session = builder.session(TaskRoute(**task["route"]), resume=task["target"]["id"])
            finally:
                builder.close()
        else:
            session = self.assembler.session(TaskRoute())
        try:
            session._ensure_runtime()
            if self.config.model_selection and session.model_selection != self.config.model_selection:
                from homebrew_harness.runtime.session_commands import select
                with session.lock:
                    select(session, self.config.model_selection)
        except BaseException:
            session.close()
            session.wait_closed(self.config.shutdown_seconds)
            raise
        self.session = session
        self.broker = ApprovalBroker(trusted=self.trusted, owner=session, announce=self._announce)
        self._control_only = True
        self._control_ids = []
        return session

    def _watch_controls(self, session, broker):
        try:
            deadline = time.monotonic() + self.config.turn_seconds
            while not self.closed and not session._closed:
                self._sync_model_preference(session)
                with self.lock:
                    receipts = [session.runtime_store.get("input_receipts", identity, {}) for identity in self._control_ids]
                    settled = receipts and all(row.get("state") in {"applied", "failed", "blocked", "cancelled"} for row in receipts)
                    if settled and not session._input_dispatching.is_set() and not session._user_waiting.is_set() and not session._pending_user and session.inputs.active_id is None:
                        broker.close()
                        session.close()
                        break
                if time.monotonic() >= deadline:
                    self._announce({"error": "Interactive command reached its deadline."})
                    break
                self.wake.wait(0.05)
                self.wake.clear()
            self._sync_model_preference(session)
            broker.close()
            session.close()
            # Retain the owner until cooperative cleanup actually ends.
            while not session.wait_closed(self.config.shutdown_seconds):
                if self.closed:
                    return
            with self.lock:
                if self.session is session:
                    self.session = self.broker = None
                    self._control_only = False
        finally:
            with self.lock:
                self._busy = False
                if not self.closed:
                    self._kick()

    def _extended_command(self, command):
        import json
        from homebrew_harness.runtime.model_selection import model_status, validate_choice
        from homebrew_harness.runtime.session_commands import HELP, run
        from homebrew_harness.context.skill_loader import SkillLoader
        if command.name == "help":
            return {"result": HELP}
        if command.name == "new":
            active = self.state.active()
            if active:
                session = self.session
                if session and not session.wait_user_boundary():
                    raise ValueError("Finish or cancel current work before /new.")
                if not session or self._control_only or session.inputs.pending() or session.inputs.active_id or session._user_waiting.is_set() or session.pending_work() or session.goal.pending or self.state.pending(active["id"]):
                    raise ValueError("Finish or cancel current work before /new.")
                self._sync_model_preference(session)
                self.broker.close()
                session.close()
                if not session.wait_closed(self.config.shutdown_seconds) or not session.shutdown_status()["persistence_ok"]:
                    raise ValueError("Previous Session cleanup is still pending.")
                self.session = self.broker = None
                self.state.finish(active["id"], result=session.last_goal_result, cleanup_complete=True)
            elif self._control_only or self.session:
                raise ValueError("Finish current context operations before /new.")
            value = self.state.runtime.get("interaction_hosts", "workspace", {})
            self.state.runtime.put("interaction_hosts", "workspace", {**value, "last": None})
            return {"result": "New blank chat; current model choice retained."}
        if command.name in {"model", "reasoning"}:
            if command.action == "status":
                active = self.state.active()
                if active and (active.get("target") or {}).get("kind") == "run":
                    value = self.runs.get(active["target"]["id"])
                    return {"result": json.dumps({"run_id": value["id"], "profile": value.get("selected_model"),
                        "binding": value.get("expected_binding"), "fixed_binding": True,
                        "models": self._model_registry().catalog()}, ensure_ascii=False, indent=2)}
                binding = self.session.lead_binding if self.session else self.assembler.binding
                return {"result": json.dumps(model_status(self._model_registry(), binding), ensure_ascii=False, indent=2)}
            active = self.state.active()
            if active and (active.get("target") or {}).get("kind") == "run":
                raise ValueError("This Run has a fixed model binding; use its declared model handoff.")
            if not self.session and active:
                raise ValueError("Wait for task preparation before changing its model.")
            if not self.session:
                choice = dict(self.config.model_selection or {"profile": self.assembler.binding.profile,
                    "model": self.assembler.binding.model if self.assembler.binding.profile is None else None})
                if command.name == "model":
                    choice = {"profile": command.argument if self._model_registry().specs else None,
                        "model": None if self._model_registry().specs else command.argument, "reasoning": None}
                else:
                    choice["reasoning"] = None if command.argument == "default" else command.argument
                binding = self._set_model_preference(choice)
                return {"result": json.dumps(model_status(self._model_registry(), binding), ensure_ascii=False, indent=2)}
        if command.name == "skills" and (command.action in {"list", "show", "use"}):
            loader = self.session.skills if self.session else SkillLoader(self.workdir / "skills", include_builtin=True)
            if command.action == "list":
                return {"result": loader.catalog()}
            content = loader.load(command.argument)
            if command.action == "show":
                return {"result": json.dumps(redact({"name": command.argument, "content": content}), ensure_ascii=False, indent=2)}
            receipt = self.submit(command.values[0], mode="queue")
            self.state.update(receipt["task_id"], selected_skill=command.argument)
            return receipt
        if command.mode == "read":
            if self.session:
                return {"result": run(self.session, command)}
            if command.name == "diff":
                from homebrew_harness.tools.queries import diff_snapshot
                base, paths = command.values
                return {"result": json.dumps(diff_snapshot(workdir=self.workdir, base=base, paths=list(paths) or None), ensure_ascii=False, indent=2)}
        session = self.session
        if session is None:
            if self.state.active():
                raise ValueError("This operation needs an interactive Session; the current Run remains unchanged.")
            session = self._command_session(history=command.name == "compact")
            if session is None:
                return {"result": "No interactive conversation to compact."}
        with approval_scope(self.broker):
            receipt = session.submit_user_input(command.query, mode="queued")
        if self._control_only:
            self._control_ids.append(receipt["input_id"])
        if self._control_only and not self._busy:
            self._busy = True
            self.workers.start(lambda: self._watch_controls(session, self.broker), name="interactive-controls", track_operation=False)
        return {"result": "Command accepted; it will run after the current turn.", "command_id": receipt["input_id"]}


    def _command(self, text):
        plain = text.strip().casefold()
        if plain in {"/status", "状态"}:
            return self.status()
        if plain in {"/pause", "暂停"}:
            return self.pause()
        if plain in {"继续", "continue", "resume"} and self.state.active():
            return self.resume()
        if plain in {"/cancel", "取消任务"}:
            if self._control_only and self.session:
                self.broker.close()
                self.session.close()
                return {"result": "Interactive command cancellation requested."}
            return self.cancel()
        if plain in {"/upgrade", "转为持续推进", "continue as managed"}:
            return self.upgrade()
        if plain in {"/detach", "关闭界面后继续", "keep running after closing"}:
            return self.detach()
        for prefix, mode in (
            ("/queue ", "queue"),
            ("/steer ", "steer"),
            ("/interrupt ", "interrupt"),
        ):
            if text.startswith(prefix):
                return self.submit(text[len(prefix) :], mode=mode)
        if text.startswith("/steer-to "):
            _, target, body = text.split(None, 2)
            return self.submit(body, mode="steer", target=target)
        if text.startswith("/approve "):
            parts = text.split()
            if len(parts) not in {3, 4} or parts[-1] not in {"yes", "no"}:
                raise ValueError("Use /approve ID [REVISION] yes/no.")
            if len(parts) == 4:
                return self.respond(
                    parts[1], int(parts[2]), "approve" if parts[-1] == "yes" else "deny"
                )
            request = next((r for r in self.requests() if r["id"] == parts[1]), None)
            if not request:
                raise ValueError("Request is stale or unknown.")
            if len(parts) == 3 and request.get("source") != "session":
                raise ValueError("Persistent requests require an explicit revision.")
            revision = int(parts[2]) if len(parts) == 4 else request["revision"]
            return self.respond(
                parts[1], revision, "approve" if parts[-1] == "yes" else "deny"
            )
        if text.startswith("/answer "):
            _, identity, revision, answer = text.split(None, 3)
            return self.respond(identity, int(revision), "answer", answer=answer)
        if text.startswith("/grant "):
            parts = text.split()
            if len(parts) != 5:
                raise ValueError("Use /grant ID REVISION RUNS SECONDS.")
            return self.respond(
                parts[1],
                int(parts[2]),
                "approve",
                runs=int(parts[3]),
                seconds=float(parts[4]),
            )
        if text.startswith("/supervisor-recover "):
            if not self.trusted:
                raise ValueError(
                    "Supervisor recovery requires trusted host inspection."
                )
            from homebrew_harness.runs.detached import recover_detached

            task = self.state.active()
            if not task or (task.get("target") or {}).get("kind") != "run":
                raise ValueError("No managed task is attached.")
            return recover_detached(
                self.runs, task["target"]["id"], text.split(None, 1)[1]
            )
        if text.startswith("/managed "):
            receipt = self.submit(text[len("/managed ") :], mode="queue")
            self.state.update(receipt["task_id"], requested_mode="managed")
            self.wake.set()
            return receipt
        command = HostCommand.parse(text)
        if command.name in {"worktree", "memory", "resume", "pwd", "ps", "stop", "hooks", "agents", "quit"}:
            return self._workspace_command(command)
        if command.extended:
            return self._extended_command(command)
        if command.name:
            if not self.session:
                raise ValueError(
                    "This command requires an active Session; use the explicit entry for administration."
                )
            return (
                {"result": self.session.run_user(text)}
                if command.local
                else self.session.submit_user_input(text, mode="queued")
            )
        if text.startswith("/"):
            raise ValueError("Unknown host command.")
        return None

    def _workspace_command(self, command):
        from homebrew_harness.runtime import local_commands as local
        from homebrew_harness.execution.execution_control import control_scope
        task = self.state.active()
        if command.name == "quit":
            return self.close()
        if command.name == "stop":
            if task and (task.get("target") or {}).get("kind") == "run":
                if command.argument and command.argument not in {task["id"], task["target"]["id"]}:
                    raise ValueError("Managed resources are stopped through their native Run controls.")
                return self.runs.control(task["target"]["id"], "pause")
            if self.session:
                result = self.session.request_stop(command.argument or None)
                if not command.argument and task:
                    self.state.update(task["id"], state="paused")
                self.wake.set()
                return {"result": local.output(result)}
            if task:
                requests = self.assembler.gateway.active_requests()
                if command.argument and command.argument != task["id"] and command.argument not in {r["id"] for r in requests}:
                    raise ValueError("Unknown stoppable preparation; use /ps.")
                self.state.update(task["id"], state="paused")
                for row in requests:
                    if not command.argument or command.argument in {row["id"], task["id"]}:
                        try:
                            self.assembler.gateway.stop_request(row["id"])
                        except ValueError:
                            pass
                self.wake.set()
                return {"result": local.output({"state": "stop_requested", "task_id": task["id"], "phase": "preparation"})}
            raise ValueError("No active execution to stop.")
        if command.name == "resume":
            if command.action == "target":
                return self._resume_target(command.argument)
            if command.action != "list" and task and task["state"] not in {"finished", "cancelled"} and not (task.get("history_only") and self.session and not self.session._execution_paused and not self.session.goal.pending):
                return self.resume()
            rows = local.sessions(self.state.runtime, current=self.session.session_id if self.session else None)
            rows += [{"id": row["id"], "kind": "interaction", "summary": row.get("intent", {}).get("original", "")[:300], "state": row["state"]}
                for row in self.state.runtime.items("interaction_tasks").values() if row["state"] in {"paused", "blocked", "accepted"}]
            rows += [{"id": row["id"], "kind": "run", "summary": row["spec"]["prompt"][:300], "state": row["status"]}
                for row in self.runs.runtime.items("runs").values() if row["status"] not in TERMINAL]
            return {"result": local.output({"contexts": rows[:40], "truncated": len(rows) > 40})}
        if self.session:
            if command.mode == "read":
                return {"result": self.session.run_user(command.query)}
            return self._extended_command(command)
        with control_scope(Deadline.after(2)):
            if command.name == "pwd":
                value = local.pwd(self.workdir)
                if task and (task.get("target") or {}).get("kind") == "run":
                    value = local.managed_pwd(self.workdir, self.state.runtime, self.runs.get(task["target"]["id"]))
            elif command.name == "worktree" and command.mode == "read":
                value = local.worktrees(self.workdir, self.state.runtime, name=command.argument or None)
            elif command.name == "memory":
                if command.mode != "read" and task:
                    raise ValueError("Memory changes require the owning Session to reach a complete boundary.")
                value = local.memory(self.workdir, command)
            elif command.name == "hooks":
                from homebrew_harness.runtime.hooks import create_hooks
                value = {"scope": "default registrations for the next Session", **create_hooks().snapshot(command.argument or None)}
            elif command.name in {"ps", "agents"}:
                rows = self.assembler.gateway.active_requests() if command.name == "ps" else []
                if task and command.name == "ps" and not task.get("target"):
                    rows.insert(0, {"id": task["id"], "kind": "preparation", "state": task["state"], "stoppable": True})
                if task and (task.get("target") or {}).get("kind") == "run":
                    run = self.runs.get(task["target"]["id"])
                    rows = local.managed_view(self.state.runtime, run, agent_only=command.name == "agents", worker=getattr(self.supervisor, "process", None))
                if command.action == "show":
                    rows = [r for r in rows if command.argument in {r["id"], r.get("name")}]
                    if not rows:
                        raise ValueError("Unknown operation or agent.")
                value = {command.name: rows}
            else:
                raise ValueError("Enable tasks/worktrees in an interactive Session before managing a worktree.")
            return {"result": local.output(value)}

    def _resume_target(self, identity):
        from homebrew_harness.runtime.local_commands import resolve_session
        from homebrew_harness.runtime.task_routing import TaskRoute
        current = self.state.active()
        if current and identity in {current["id"], (current.get("target") or {}).get("id"), "session:" + str((current.get("target") or {}).get("id"))}:
            return self.resume()
        if current or self._control_only:
            raise ValueError("Finish the current context before restoring another; inspect /status.")
        if identity.startswith("interaction_"):
            task = self.state.get(identity)
            if task.get("config") != self.configuration():
                raise ValueError("Restore the original host configuration before resuming this task.")
            if task["state"] in {"finished", "cancelled"}:
                identity = "session:" + task["target"]["id"] if task.get("target", {}).get("kind") == "session" else identity
            else:
                self.state.runtime.put("interaction_hosts", "workspace", {**self.state.runtime.get("interaction_hosts", "workspace", {}), "active": task["id"]})
                return self.resume(task["id"])
        if identity.startswith("run_"):
            run = self.runs.get(identity)
            if run["status"] in TERMINAL:
                raise ValueError("This Run is terminal; its history remains inspectable.")
            self.state.adopt_run(run, self.configuration())
            return self.resume()
        sid = resolve_session(self.state.runtime, identity)
        saved = self.state.runtime.get("checkpoints", "lead:" + sid)
        from dataclasses import replace
        from homebrew_harness.runtime.interaction_config import InteractionAssembler
        builder = InteractionAssembler(self.workdir, replace(self.config, model_selection=None))
        route = TaskRoute(capabilities=tuple(saved["capabilities"]["enabled"]))
        session = None
        try:
            session = builder.session(route, resume=sid)
            session._ensure_runtime()
            import uuid
            tid = "interaction_" + uuid.uuid4().hex
            task = {"id": tid, "state": "accepted", "revision": 0, "intent": saved["intent"], "created_at": time.time(),
                "input_id": None, "target": {"kind": "session", "id": sid}, "route": route.data(),
                "config": self.configuration(), "lifetime": "attached", "history_only": True}
            db = self.state.runtime.backend()
            with db.write() as conn:
                host = db._get(conn, "interaction_hosts", "workspace", {})
                if host.get("active"):
                    raise ValueError("Another context became active while restoring.")
                db._put(conn, "interaction_tasks", tid, task)
                db._put(conn, "interaction_hosts", "workspace", {**host, "active": tid})
            self.session = session
            self.broker = ApprovalBroker(trusted=self.trusted, owner=session, announce=self._announce)
            session.start(include_clock=True)
            self._kick()
            return {"result": "Session restored; enter the next request or /resume to continue paused work.", "session_id": sid}
        except BaseException:
            if session:
                session.close()
                session.wait_closed(self.config.shutdown_seconds)
            raise
        finally:
            builder.close()

    def _kick(self):
        with self.lock:
            if self.closed or self._busy or not self.state.active():
                return
            self._busy = True
            try:
                self.workers.start(
                    self._drive, name="interaction-owner", track_operation=False
                )
            except BaseException:
                self._busy = False
                raise

    def _drive(self):
        try:
            while not self.closed:
                with self.lock:
                    task = self.state.active()
                if not task:
                    return
                if task["state"] in {"blocked", "paused"}:
                    return
                if task["state"] == "handoff_pending":
                    self._handoff(task)
                    continue
                if task.get("config") and task["config"] != self.configuration():
                    self.state.update(
                        task["id"],
                        state="blocked",
                        error="Host configuration changed; restore it before resuming.",
                    )
                    return
                if not task.get("target"):
                    self.state.update(task["id"], state="preparing")
                    task = self.state.get(task["id"])
                    try:
                        with self.assembler.gateway.scope(
                            deadline=Deadline.after(self.config.request_seconds),
                            context_id="routing",
                        ):
                            routing_intent = dict(task["intent"])
                            reference = load_task_reference(
                                self.state.runtime, task.get("previous_task")
                            )
                            if reference:
                                routing_intent["previous_discussion_reference"] = {
                                    "request": reference["intent"]["original"][:1000],
                                    "answer": reference["answer"],
                                    "state": reference["state"],
                                }
                            route = self.router.propose(
                                routing_intent,
                                execution_mode=task.get("requested_mode"),
                                detach_allowed=self.config.detached,
                            )
                        route = validate_route(
                            route.data(),
                            allowed=self.assembler.allowed,
                            registry=self.assembler.registry,
                            prompt=task["intent"]["original"],
                            detach_allowed=self.config.detached,
                        )
                        active = self.state.active()
                        if not active or active["id"] != task["id"]:
                            continue
                        self.state.runtime.put(
                            "interaction_routing_usage",
                            task["id"],
                            self.assembler.gateway.summary(),
                        )
                        if self.closed or self.state.get(task["id"])["state"] == "paused":
                            return
                        if route.missing_requirements:
                            self.state.update(
                                task["id"],
                                state="blocked",
                                error="; ".join(route.missing_requirements),
                            )
                            self._announce(
                                {
                                    "task_id": task["id"],
                                    "missing": list(route.missing_requirements),
                                }
                            )
                            return
                        if self.state.get(task["id"])["revision"] != task["revision"]:
                            continue
                        if route.execution_mode == "managed":
                            spec = self.assembler.run_spec(
                                route, task["intent"]["original"]
                            )

                            def accept(db, conn, run, task=task, route=route):
                                run.update(
                                    getattr(self.assembler, "run_control_binding", {})
                                )
                                run.update(
                                    routing_plan=route.data(),
                                    interaction_reference=task.get("previous_task"),
                                    bindings=self.assembler.run_bindings,
                                    git_head=self.assembler.run_head,
                                    expected_binding=self.assembler.run_bindings[
                                        "model"
                                    ],
                                    expected_models=self.assembler.run_bindings[
                                        "models"
                                    ],
                                )
                                self.state.accept(
                                    task,
                                    route,
                                    self.configuration(),
                                    {"kind": "run", "id": run["id"]},
                                    db=db,
                                    conn=conn,
                                )

                            try:
                                self.runs.create(spec, _accept=accept)
                            except Exception:
                                saved = self.state.get(task["id"])
                                if not saved.get("target"):
                                    raise
                        else:
                            self._prepare_session(task, route)
                        self._announce(
                            {
                                "task_id": task["id"],
                                "execution_mode": route.execution_mode,
                                "capabilities": list(route.capabilities),
                                "reason": route.rationale,
                            }
                        )
                    except Exception as error:
                        active = self.state.active()
                        if not active or active["id"] != task["id"]:
                            continue
                        if self.state.get(task["id"])["revision"] != task[
                            "revision"
                        ] and not self.state.get(task["id"]).get("target"):
                            continue
                        if self.state.get(task["id"])["state"] == "paused":
                            return
                        self.state.update(
                            task["id"], state="blocked", error=redact(str(error))
                        )
                        self._announce(
                            {"task_id": task["id"], "error": redact(str(error))}
                        )
                        return
                    task = self.state.get(task["id"])
                if task["target"]["kind"] == "session":
                    self._observe_session(task)
                else:
                    self._run(task)
                current = self.state.active()
                if current and current["id"] == task["id"]:
                    return
        except Exception as error:
            task = self.state.active()
            if task and task["state"] != "paused":
                self.state.update(task["id"], state="blocked", error=redact(str(error)))
            self._announce({"error": redact(str(error))})
        finally:
            with self.lock:
                self._busy = False
                self.wake.set()
                active = self.state.active()
                if (
                    not self.closed
                    and active
                    and active["state"] in {"queued", "preparing"}
                ):
                    self._kick()

    def _prepare_session(self, task, route, *, resume=None):
        session = self.assembler.session(route, resume=resume)
        broker = ApprovalBroker(
            trusted=self.trusted, announce=self._announce, owner=session
        )
        try:
            session._ensure_runtime()
            if resume is None:
                try:
                    self.state.accept(
                        task,
                        route,
                        self.configuration(),
                        {"kind": "session", "id": session.session_id},
                    )
                except Exception:
                    if (self.state.get(task["id"]).get("target") or {}).get(
                        "id"
                    ) != session.session_id:
                        raise
                add_task_reference(
                    session,
                    load_task_reference(self.state.runtime, task.get("previous_task")),
                )
                if task.get("selected_skill"):
                    from homebrew_harness.context.tracked_history import set_context
                    set_context(session.messages, "selected_skill", {"role": "user", "name": "selected_skill",
                        "content": "User-selected skill guidance, not additional permission:\n" + session.skills.load(task["selected_skill"])})
                if route.goal_candidate:
                    session._bind_task_goal(
                        task["intent"]["original"], route.goal_candidate
                    )
                session.messages.append(
                    {
                        "role": "user",
                        "name": "routing_plan",
                        "content": "Host planning reference, not authority: "
                        + str(route.data()),
                    }
                )
            with self.lock:
                self.session, self.broker = session, broker
            with approval_scope(broker):
                if self.state.get(task["id"])["state"] == "paused":
                    session.request_stop()
                if task.get("input_id") and not session.runtime_store.get(
                    "input_submissions", task["input_id"]
                ):
                    session.inputs.submit(
                        task["intent"]["original"],
                        mode="queued",
                        input_id=task["input_id"],
                        literal=task.get("literal", False),
                    )
                self._deliver_pending()
                if self.state.get(task["id"]).get("resume_requested"):
                    if session._execution_paused:
                        session.resume_execution()
                    elif session._recovery_blocked or session._automatic_failed:
                        raise ValueError("Inspect Session recovery using /recovery before continuing.")
                    elif session.goal.pending and not session.goal.enabled:
                        session.submit_user_input("/goal resume", mode="queued")
                    self.state.update(task["id"], state="accepted", resume_requested=False)
                session.start(include_clock=True)
        except BaseException:
            broker.close()
            session.close()
            session.wait_closed(self.config.shutdown_seconds)
            raise

    def _deliver_pending(self):
        task = self.state.active()
        if not task or not task.get("target") or task["state"] == "handoff_pending":
            return
        for record in self.state.pending(task["id"]):
            payload = record["payload"]
            mode = "interrupt" if payload["mode"] == "interrupt" else "steer"
            if task["target"]["kind"] == "run":
                self.runs.submit(
                    task["target"]["id"],
                    payload["text"],
                    mode=mode,
                    input_id=record["id"],
                    literal=payload.get("literal", False),
                )
            elif self.session:
                with approval_scope(self.broker):
                    self.session.inputs.submit(
                        payload["text"],
                        mode=mode,
                        target=payload["target"],
                        input_id=record["id"],
                        literal=payload.get("literal", False),
                    )
            else:
                continue
            self.state.delivered(record)

    def _observe_session(self, task):
        if self.session is None:
            self._prepare_session(
                task, TaskRoute(**task["route"]), resume=task["target"]["id"]
            )
        session = self.session
        while not self.closed:
            self._sync_model_preference(session)
            current_active = self.state.active()
            if not current_active or current_active["id"] != task["id"]:
                return
            self._deliver_pending()
            if session._execution_paused:
                self.state.update(task["id"], state="paused")
                return
            if self.state.get(task["id"]).get("history_only"):
                self.wake.wait(.05)
                self.wake.clear()
                continue
            if time.time() >= task["created_at"] + self.config.total_seconds:
                self.state.update(
                    task["id"],
                    state="blocked",
                    error="The original task deadline expired.",
                )
                self.broker.close()
                session.close()
                return
            if (
                session.goal.pending
                and not session.goal.enabled
                and not session.inputs.pending()
            ):
                self.state.update(
                    task["id"],
                    state="blocked",
                    error="Goal remains unmet; explicitly resume it or inspect the required action.",
                )
                return
            if session._automatic_failed or session._recovery_blocked:
                self.state.update(
                    task["id"],
                    state="blocked",
                    error="Session requires inspection or an explicit resume.",
                )
                return
            current = self.state.get(task["id"])
            if (
                current.get("upgrade_requested")
                and not session.inputs.pending()
                and session.inputs.active_id is None
            ):
                pending = set(session.pending_work()) - {"goal"}
                if (
                    pending
                    or session.capabilities.enabled
                    & {"tasks", "teams", "worktrees", "cron"}
                    or self.broker.records()
                    or session.recovery_operations()
                ):
                    self.state.update(
                        task["id"],
                        state="blocked",
                        error="Handoff requires a settled single-agent Session without task/team/worktree or Cron ownership.",
                    )
                    return
                self.state.update(task["id"], state="handoff_pending")
                self.broker.close()
                session.close()
                if (
                    not session.wait_closed(self.config.shutdown_seconds)
                    or not session.shutdown_status()["persistence_ok"]
                ):
                    return
                self.session = self.broker = None
                self._handoff(self.state.get(task["id"]))
                return
            if (
                not session.inputs.pending()
                and session.inputs.active_id is None
                and not session.pending_work()
                and not session.goal.pending
                and not (session.cron and session.cron.list())
            ):
                with self.lock:
                    current_task = self.state.active()
                    if not current_task or current_task["id"] != task["id"] or self.session is not session:
                        return
                    if session._input_dispatching.is_set() or session._user_waiting.is_set() or session._pending_user or self.state.pending(task["id"]) or self.state.get(task["id"]).get(
                        "upgrade_requested"
                    ):
                        continue
                    self._sync_model_preference(session)
                    self.broker.close()
                    session.close()
                    if (
                        session.wait_closed(self.config.shutdown_seconds)
                        and session.shutdown_status()["persistence_ok"]
                    ):
                        self.session = self.broker = None
                        self.state.finish(
                            task["id"],
                            result=session.last_goal_result,
                            cleanup_complete=True,
                        )
                    else:
                        self.state.update(
                            task["id"],
                            state="blocked",
                            error="Session cleanup remains pending.",
                        )
                    return
            self.wake.wait(0.05)
            self.wake.clear()

    def _new_supervisor(self):
        from homebrew_harness.runs.services import RunServices
        from homebrew_harness.runs.supervisor import RunSupervisor

        factory = self.supervisor_factory or RunSupervisor
        return factory(
            self.runs,
            host_arguments=self.config.worker_arguments(self.workdir),
            on_update=self._show_requests,
            services=RunServices.load(
                self.runs,
                self.config.control_config,
                environment=getattr(self.assembler, "environment", None),
            ),
        )

    def _run(self, task):
        from homebrew_harness.runs.detached import detached_status, launch_detached

        identity = task["target"]["id"]
        current = self.runs.get(identity)
        if (
            current["status"] in TERMINAL
            and current.get("finalized_at")
            and current["cleanup_complete"]
        ):
            self.state.finish(
                task["id"],
                result=(current.get("last_outcome") or {}).get("text", ""),
                cleanup_complete=True,
            )
            return
        self._deliver_pending()
        if (
            task.get("lifetime") == "detached"
            or (task.get("route") or {}).get("lifetime") == "detached"
        ):
            self.state.update(task["id"], lifetime="detached")
            launch = launch_detached(self.runs, identity, self.config)
            self._announce(launch)
            if launch["state"] in {"startup_pending", "failed"}:
                self.state.update(
                    task["id"],
                    state="blocked",
                    error="Detached launch requires inspection; check /status and use /supervisor-recover TOKEN if execution has ended.",
                )
                return
            self._observe_run(task)
            return
        if detached_status(self.runs, identity).get("live"):
            self._observe_run(task)
            return
        supervisor = self._new_supervisor()
        self.supervisor = supervisor
        try:
            result = supervisor.supervise(identity)
        finally:
            supervisor.close()
            self.supervisor = None
        if (
            result["status"] in TERMINAL
            and result.get("cleanup_complete")
            and result.get("finalized_at")
        ):
            self.state.finish(
                task["id"],
                result=(result.get("last_outcome") or {}).get("text", ""),
                cleanup_complete=True,
            )
        self._announce(
            {
                "task_id": task["id"],
                "status": result["status"],
                "result": (result.get("last_outcome") or {}).get("text", ""),
            }
        )

    def _show_requests(self, identity):
        if self.closed:
            return
        task = self.state.active()
        if (
            not task
            or (task.get("target") or {}).get("kind") != "run"
            or task["target"]["id"] != identity
        ):
            return
        requests = self.requests()
        view = (task["id"], tuple((r["id"], r["revision"]) for r in requests))
        if view != self._request_view:
            self._request_view = view
            if requests:
                if self._announce({"requests": requests}):
                    self._set_focus(requests)
                else:
                    self._request_view = None
            else:
                self._set_focus([])

    def _observe_run(self, task):
        identity = task["target"]["id"]
        while not self.closed:
            self._deliver_pending()
            run = self.runs.status(identity)
            self._show_requests(identity)
            if (
                run["status"] in TERMINAL
                and run.get("finalized_at")
                and run["cleanup_complete"]
            ):
                self._announce(
                    {
                        "status": run["status"],
                        "result": (run.get("last_outcome") or {}).get("text", ""),
                    }
                )
                self.state.finish(
                    task["id"],
                    result=(run.get("last_outcome") or {}).get("text", ""),
                    cleanup_complete=True,
                )
                return
            if run["status"] in {"paused", "blocked"}:
                return
            self.wake.wait(0.1)
            self.wake.clear()

    def status(self, task_id=None):
        with self.lock:
            task = self.state.get(task_id) if task_id else self.state.active()
            if not task:
                return {"state": "idle"}
            result = {
                "task_id": task["id"],
                "state": task["state"],
                "target": task.get("target"),
                "route": task.get("route"),
                "error": task.get("error"),
                "result": task.get("result"),
            }
            if task.get("target", {}) and task["target"]["kind"] == "run":
                run = self.runs.status(task["target"]["id"])
                result["run"] = run
                from homebrew_harness.runs.detached import detached_status

                result["supervisor"] = detached_status(self.runs, run["id"])
                if (
                    run["status"] in TERMINAL
                    and run.get("finalized_at")
                    and run["cleanup_complete"]
                ):
                    self.state.finish(
                        task["id"],
                        result=(run.get("last_outcome") or {}).get("text", ""),
                        cleanup_complete=True,
                    )
                    result["state"] = "finished"
                    self._kick()
            elif self.session:
                result["inputs"] = self.session.input_status()
                result["pending"] = self.session.pending_work()
                result["cleanup"] = self.session.shutdown_status()
            result["requests"] = self.requests()
            self._set_focus(result["requests"])
            return redact(result)

    @staticmethod
    def _focus_key(request):
        return (
            request.get("task_id"),
            request["id"],
            request["revision"],
            request.get("turn_id"),
        )

    def _set_focus(self, requests):
        self.focus = self._focus_key(requests[0]) if len(requests) == 1 else None

    def requests(self):
        task = self.state.active()
        if not task or not task.get("target"):
            return []
        if task["target"]["kind"] == "session":
            return (
                [
                    {**r, "source": "session", "task_id": task["id"]}
                    for r in self.broker.records()
                ]
                if self.broker
                else []
            )
        from homebrew_harness.runs.requests import HumanRequests

        return [
            {**r, "source": "run", "task_id": task["id"]}
            for r in HumanRequests(self.runs).records(task["target"]["id"])
            if r["state"] == "pending"
        ]

    def respond(
        self, request_id, revision, decision, *, answer=None, runs=None, seconds=None
    ):
        if not self.trusted:
            raise ValueError("Approval and answers require trusted host user input.")
        if type(revision) is not int or decision not in {"approve", "deny", "answer"}:
            raise ValueError("Invalid host decision.")
        payload = {
            "decision": decision,
            "answer": answer,
            "runs": runs,
            "seconds": seconds,
        }
        key = str(request_id) + ":" + str(revision)
        with self.lock:
            prior = self.state.runtime.get("interaction_decisions", key)
            if prior:
                if prior["payload"] != payload:
                    raise ValueError(
                        "This request revision already has another decision."
                    )
                return prior["result"]
            request = next((r for r in self.requests() if r["id"] == request_id), None)
            task = self.state.active()
            if not request and task and (task.get("target") or {}).get("kind") == "run":
                from homebrew_harness.runs.requests import HumanRequests

                saved = HumanRequests(self.runs).get(task["target"]["id"], request_id)
                if saved.get("decision_from") == revision:
                    request = {**saved, "source": "run", "revision": revision}
            if (
                not request
                or type(revision) is not int
                or request["revision"] != revision
            ):
                raise ValueError("Request focus changed; refresh its ID and revision.")
            if request["source"] == "session":
                if (
                    decision not in {"approve", "deny"}
                    or answer is not None
                    or runs is not None
                    or seconds is not None
                ):
                    raise ValueError("This request is a single tool invocation.")
                self.broker.respond(request_id, decision == "approve")
                result = {"request_id": request_id, "state": decision}
            else:
                from homebrew_harness.runs.requests import HumanRequests

                task = self.state.active()
                result = HumanRequests(self.runs).decide(
                    task["target"]["id"],
                    request_id,
                    revision,
                    decision,
                    answer,
                    runs=runs,
                    seconds=seconds,
                )
            receipt = {
                "payload": payload,
                "result": result,
                "task_id": task["id"] if task else request.get("task_id"),
            }
            try:
                self.state.runtime.put("interaction_decisions", key, receipt)
            except OSError:
                if self.state.runtime.get("interaction_decisions", key) != receipt:
                    raise
            self.wake.set()
            self._kick()
            return result

    def pause(self, task_id=None):
        task = self.state.get(task_id) if task_id else self.state.active()
        if not task:
            raise ValueError("No active task.")
        if task.get("target") and task["target"]["kind"] == "run":
            return self.runs.control(task["target"]["id"], "pause")
        raise ValueError(
            "Interactive tasks support steering/interrupt; pause requires a managed Run."
        )

    def resume(self, task_id=None):
        with self.lock:
            task = self.state.get(task_id) if task_id else self.state.active()
            if not task:
                raise ValueError("No active task.")
            if (
                self.session
                and self.session._closed
                and self.session.wait_closed(0)
                and self.session.shutdown_status()["persistence_ok"]
            ):
                self.session = self.broker = None
            if not task.get("target") and self.session is None and (self._busy or self.assembler.gateway.inflight):
                raise ValueError("Preparation still needs cleanup; inspect /ps before /resume.")
            if task.get("target") and task["target"]["kind"] == "session" and self.session is None:
                if task.get("config") != self.configuration():
                    raise ValueError("Restore the original host configuration before resuming this task.")
                self.state.update(task["id"], state="queued", resume_requested=True, error=None)
                self.wake.set()
                self._kick()
                return {"task_id": task["id"], "state": "accepted"}
            if task.get("target") and task["target"]["kind"] == "run":
                self.runs.control(task["target"]["id"], "run")
            elif self.session and self.session._execution_paused:
                self.session.resume_execution()
            elif self.session and (
                self.session._recovery_blocked or self.session._automatic_failed
            ):
                raise ValueError(
                    "Inspect Session recovery using /recovery before continuing."
                )
            if (
                self.session
                and self.session.goal.pending
                and not self.session.goal.enabled
                and not self.session.inputs.pending()
            ):
                self.session.submit_user_input("/goal resume", mode="queued")
            self.state.update(
                task["id"],
                state="handoff_pending"
                if task["state"] == "handoff_pending"
                else "accepted"
                if task.get("target")
                else "queued",
                error=None,
            )
            self._kick()
            return {"task_id": task["id"], "state": "accepted"}

    def cancel(self, task_id=None):
        task = self.state.get(task_id) if task_id else self.state.active()
        if not task:
            raise ValueError("No active task.")
        if task.get("target") and task["target"]["kind"] == "run":
            return self.runs.control(task["target"]["id"], "cancel")
        with self.lock:
            if (
                self.session is None
                and (task.get("target") or {}).get("kind") == "session"
            ):
                sid = task["target"]["id"]
                meta = self.state.runtime.get("session_meta", sid, {})
                saved = self.state.runtime.get("checkpoints", "lead:" + sid, {})
                if not meta.get("close_complete") or saved.get("recovery_blocked"):
                    raise ValueError(
                        "Finish or inspect the saved Session cleanup before cancelling its reference."
                    )
            if self.session:
                self.broker.close()
                self.session.close()
                if (
                    not self.session.wait_closed(self.config.shutdown_seconds)
                    or not self.session.shutdown_status()["persistence_ok"]
                ):
                    return {"state": "cleanup_pending"}
                if self.session.recovery_operations():
                    self.state.update(
                        task["id"],
                        state="blocked",
                        error="Cancellation stopped execution; inspect uncertain operations before starting other work.",
                    )
                    return {"state": "inspection_required"}
                self.session.runtime_store.mailbox(
                    "recover", self.session.inputs.recipient
                )
                for record in self.session.inputs.pending():
                    self.session.inputs.manage("cancel", record["id"])
                if self.session.goal.pending:
                    self.session.goal.suspend(
                        "abandoned",
                        "User cancelled the interactive task; completion was not claimed.",
                    )
                self.session = self.broker = None
            self.state.finish(task["id"], state="cancelled", cleanup_complete=True)
            self.wake.set()
            self._kick()
            return {"state": "cancelled"}

    def upgrade(self):
        with self.lock:
            if not self.trusted:
                raise ValueError("Handoff requires trusted user intent.")
            task = self.state.active()
            if (
                not task
                or not task.get("target")
                or task["target"]["kind"] != "session"
            ):
                raise ValueError("Upgrade requires an active interactive task.")
            self.state.update(task["id"], upgrade_requested=True)
            self.wake.set()
            self._kick()
            return {"task_id": task["id"], "state": "handoff_requested"}

    def _handoff(self, task):
        from homebrew_harness.runs.session_handoff import accept_session_handoff

        remaining = task["created_at"] + self.config.total_seconds - time.time()
        if remaining < 0.1:
            raise ValueError(
                "The original task deadline expired; handoff cannot renew it."
            )
        saved = self.runs.runtime.get("checkpoints", "lead:" + task["target"]["id"])
        goal_state = (
            self.runs.runtime.get("goals", saved.get("goal_id")) if saved else None
        )
        goal = (
            goal_state.get("spec")
            if goal_state
            else task["route"].get("goal_candidate")
        )
        mcp = saved.get("mcp", {})
        if "mcp" in task["route"]["capabilities"] and (
            not mcp.get("supported") or not mcp.get("declaration", {}).get("selection")
        ):
            raise ValueError(
                "MCP handoff requires a prepared host-selected server/tool manifest."
            )
        route = TaskRoute(
            mcp=mcp.get("declaration", {}).get("selection", {}),
            execution_mode="managed",
            capabilities=tuple(task["route"]["capabilities"]),
            goal_candidate=goal,
        )
        spec = self.assembler.run_spec(
            route, task["intent"]["original"], goal=goal, remaining=remaining
        )
        accept_session_handoff(
            self.runs,
            self.state,
            task,
            spec,
            self.configuration(),
            service_binding=getattr(self.assembler, "run_control_binding", None),
        )
        self._announce({"task_id": task["id"], "state": "handed_off"})
        self._run(self.state.get(task["id"]))

    def detach(self):
        if not self.trusted:
            raise ValueError(
                "Detached execution requires trusted user lifecycle intent."
            )
        task = self.state.active()
        if not task or not task.get("target") or task["target"]["kind"] != "run":
            raise ValueError(
                "Only a managed task can detach; first use the managed handoff."
            )
        self.state.update(task["id"], lifetime="detached")
        return {"task_id": task["id"], "state": "detach_requested"}

    def close(self, *, keep_running=False):
        if keep_running:
            self.detach()
        self.closed = True
        self.assembler.gateway.close()
        self.wake.set()
        task = self.state.active()
        if (
            task
            and task.get("target")
            and task["target"]["kind"] == "run"
            and self.supervisor
        ):
            identity = task["target"]["id"]
            if self.runs.get(identity)["status"] not in TERMINAL:
                try:
                    self.runs.control(identity, "pause")
                except ValueError:
                    # Completion can win after the observation and before the
                    # pause transaction. It still needs the ordinary shutdown.
                    if self.runs.get(identity)["status"] not in TERMINAL:
                        raise
        if self.broker:
            self.broker.close()
        if self.session:
            self.session.close()

        def finish():
            self.workers.wait(None)
            if self.session:
                self.session.wait_closed(None)
            task = self.state.active()
            if (
                task
                and task.get("lifetime") == "detached"
                and task.get("target", {}).get("kind") == "run"
            ):
                from homebrew_harness.runs.detached import launch_detached

                run = self.runs.get(task["target"]["id"])
                if run["status"] == "paused":
                    self.runs.control(run["id"], "run")
                launch_detached(self.runs, run["id"], self.config)
            self.assembler.gateway.wait_idle(None)
            self.assembler.close()
            self.runs.close()
            self.leases.close()

        self.shutdown.request(finish, self.config.shutdown_seconds)
        return {
            "state": "closed"
            if not self.leases.held("interaction-host")
            else "cleanup_pending"
        }
