"""Host-registered test commands, scoped grants and current-session receipts."""

from __future__ import annotations


import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelGatewayError,
    current_scope,
)
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.security.safe_files import (
    SafeFile,
    canonical_root,
)

from .source_snapshot import TestRunnerError, snapshot, validate_limits

TEST_TOOL_NAMES = ("list_tests", "run_tests", "get_test_result")
_CONTEXT = ContextVar("test_execution_context", default=None)
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def executable_identity(path):
    configured = Path(path)
    if not configured.is_absolute():
        raise TestRunnerError(
            "Test executables must be selected by the host using an absolute path."
        )
    resolved = configured.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise TestRunnerError("Registered test executable is unavailable.")
    if resolved.name in {
        "sh",
        "bash",
        "zsh",
        "fish",
        "sudo",
        "su",
        "rm",
        "dd",
        "shutdown",
        "reboot",
    }:
        raise TestRunnerError(
            "Shell and system-control executables are not TestRunner recipes."
        )
    value = hashlib.sha256()
    with resolved.open("rb") as stream:
        while chunk := stream.read(65536):
            value.update(chunk)
    configuration = configured.parent.parent / "pyvenv.cfg"
    venv = (
        hashlib.sha256(configuration.read_bytes()).hexdigest()
        if configuration.is_file()
        else None
    )
    return {"target": str(resolved), "sha256": value.hexdigest(), "venv": venv}


@dataclass(frozen=True)
class TestRecipe:
    name: str
    argv: tuple[str, ...]
    description: str = "Registered test suite"
    roles: tuple[str, ...] = ("lead",)
    input_globs: tuple[str, ...] = ("**/*",)
    locked_globs: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    timeout: float = 120
    output_bytes: int = 1_000_000
    cpu_seconds: int = 120
    file_bytes: int = 64_000_000
    open_files: int = 256
    memory_mb: int | None = None
    max_runs: int = 10
    approval_seconds: float = 900
    allow_worktrees: bool = True
    snapshot_limits: dict = field(default_factory=dict)
    _identity: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(
            self, "snapshot_limits", validate_limits(self.snapshot_limits)
        )
        for name in ("argv", "roles", "input_globs", "locked_globs", "env"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(self.name, str) or not _ID.fullmatch(self.name):
            raise TestRunnerError("Invalid registered test name.")
        if (
            not self.argv
            or len(self.argv) > 64
            or sum(len(arg) for arg in self.argv if isinstance(arg, str)) > 16_000
            or any(
                not isinstance(arg, str) or not arg or "\0" in arg for arg in self.argv
            )
        ):
            raise TestRunnerError("Test argv must be a fixed nonempty string array.")
        if not isinstance(self.description, str) or len(self.description) > 500:
            raise TestRunnerError("Test description must be short text.")
        if not self.roles or not set(self.roles) <= {"lead", "subagent", "teammate"}:
            raise TestRunnerError("Invalid TestRunner roles.")
        for patterns in (self.input_globs, self.locked_globs):
            if any(
                not isinstance(p, str)
                or not p
                or Path(p).is_absolute()
                or ".." in Path(p).parts
                for p in patterns
            ):
                raise TestRunnerError(
                    "Test input patterns must stay within their workspace."
                )
        if not self.input_globs:
            raise TestRunnerError("A recipe must declare watched inputs.")
        for name in (
            "output_bytes",
            "cpu_seconds",
            "file_bytes",
            "open_files",
            "max_runs",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise TestRunnerError("Test resource limits must be positive integers.")
        for value in (self.timeout, self.approval_seconds):
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 < value <= 3600
            ):
                raise TestRunnerError(
                    "Test time limits must be positive and at most one hour."
                )
        if self.memory_mb is not None and (
            type(self.memory_mb) is not int
            or self.memory_mb < 64
            or sys.platform != "linux"
        ):
            raise TestRunnerError(
                "Address-space limits require Linux and at least 64 MB; no memory limit is implied otherwise."
            )
        if type(self.allow_worktrees) is not bool:
            raise TestRunnerError("allow_worktrees must be boolean.")
        for key, value in self.env:
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or not isinstance(value, str)
                or "\0" in value
                or key
                in {
                    "HOME",
                    "PATH",
                    "TMPDIR",
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "BASH_ENV",
                    "ENV",
                }
                or key.startswith(("LD_", "DYLD_"))
                or key.startswith("PYTHON")
                and key
                not in {
                    "PYTHONHASHSEED",
                    "PYTHONUTF8",
                    "PYTHONDONTWRITEBYTECODE",
                    "PYTHONNOUSERSITE",
                }
                or re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key)
                or redact(value) != value
            ):
                raise TestRunnerError(
                    "Recipe environment contains an unsafe or private setting."
                )
        object.__setattr__(self, "_identity", executable_identity(self.argv[0]))

    def fingerprint(self):
        return digest(
            {
                "argv": self.argv,
                "snapshot_limits": self.snapshot_limits,
                "executable": self._identity,
                "env": self.env,
                "inputs": self.input_globs,
                "locked": self.locked_globs,
                "roles": self.roles,
                "timeout": self.timeout,
                "output": self.output_bytes,
                "cpu": self.cpu_seconds,
                "file": self.file_bytes,
                "fds": self.open_files,
                "memory": self.memory_mb,
                "worktrees": self.allow_worktrees,
            }
        )


@dataclass
class Grant:
    recipe: str
    fingerprint: str
    locked: dict
    roles: tuple[str, ...]
    cwd: str | None
    task_id: str | None
    expires: float
    remaining: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class TestContext:
    role: str = "lead"
    task_id: str | None = None


@contextmanager
def test_context(role, task_id=None):
    token = _CONTEXT.set(TestContext(role, task_id))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


# A trusted in-memory trampoline avoids reloading model-editable harness code.
# Resource limits are applied in a separate process, never through preexec_fn.
_WORKER = r"""
import json, os, resource, sys
spec = json.load(sys.stdin)
os.fchdir(spec["cwd_fd"])
os.close(spec["cwd_fd"])
resource.setrlimit(resource.RLIMIT_CPU, (spec["cpu"], spec["cpu"] + 1))
resource.setrlimit(resource.RLIMIT_FSIZE, (spec["file"], spec["file"]))
resource.setrlimit(resource.RLIMIT_NOFILE, (spec["fds"], spec["fds"]))
if spec["memory"] is not None:
    cap = spec["memory"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
fd = os.open(os.devnull, os.O_RDONLY)
os.dup2(fd, 0)
os.close(fd)
os.execve(spec["argv"][0], spec["argv"], spec["env"])
"""


_UNITTEST = """import os, sys, unittest
from pathlib import Path
project = Path.cwd()
sys.path.insert(0, str(project))
if (project / 'src').is_dir():
    sys.path.insert(0, str(project / 'src'))
top = str(project) if (project / 'tests' / '__init__.py').is_file() else None
suite = unittest.defaultTestLoader.discover('tests', top_level_dir=top)
result = unittest.TextTestRunner(verbosity=1).run(suite)
if result.testsRun == 0:
    print('No tests were collected.', file=sys.stderr)
    sys.exit(3)
sys.exit(0 if result.wasSuccessful() else 1)
"""


class TestRunner:
    def __init__(
        self, workdir, recipes=(), *, max_runs=20, total_seconds=600, backend=None
    ):
        from homebrew_harness.execution.execution_backend import NativeBackend

        self.backend = backend or NativeBackend()
        self.workdir = canonical_root(Path(workdir))
        recipes = tuple(recipes)
        self.recipes = {recipe.name: recipe for recipe in recipes}
        if len(self.recipes) != len(tuple(recipes)):
            raise TestRunnerError("Duplicate test recipe names.")
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self._account_lock = threading.RLock()
        self.grants = []
        self._handoff_expiry = {}
        self._receipts = {}
        self.receipt_scope = None
        self.authorizations = None
        self._processes = set()
        self.process_lock = threading.Lock()
        self.closed = threading.Event()
        self.active = threading.Event()
        self.active_test = None
        self._run_cancel = None
        self._active_run_id = None
        self.max_runs, self.total_seconds = max_runs, total_seconds
        if (
            type(max_runs) is not int
            or max_runs < 1
            or not math.isfinite(total_seconds)
            or total_seconds <= 0
        ):
            raise TestRunnerError("Invalid shared TestRunner limits.")
        self.runs = 0
        self.seconds = 0.0
        self._launcher = executable_identity(sys.executable)

    def _fingerprint(self, recipe):
        if self.backend.name == "native":
            return recipe.fingerprint()
        return digest(
            {"recipe": recipe.fingerprint(), "backend": self.backend.fingerprint()}
        )

    def handoff_state(self):
        """Export consumed limits and scoped grants, never test pass evidence."""
        with self._account_lock:
            grants = []
            for grant in self.grants:
                expires = self._handoff_expiry.setdefault(
                    grant.id, time.time() + grant.expires - time.monotonic()
                )
                grants.append(
                    {
                        "recipe": grant.recipe,
                        "identity": {
                            "fingerprint": grant.fingerprint,
                            "locked": grant.locked,
                        },
                        "roles": list(grant.roles),
                        "cwd": grant.cwd,
                        "task_id": grant.task_id,
                        "expires_at": expires,
                        "remaining": grant.remaining,
                        "id": grant.id,
                    }
                )
            return {
                "supported": self.authorizations is None,
                "max_runs": self.max_runs,
                "total_seconds": self.total_seconds,
                "runs": self.runs,
                "seconds": self.seconds,
                "grants": grants,
            }

    def restore_handoff_state(self, state):
        """Restore the same plain Session's consumption instead of renewing it."""
        if (
            not isinstance(state, dict)
            or not state.get("supported")
            or state.get("max_runs") != self.max_runs
            or state.get("total_seconds") != self.total_seconds
            or type(state.get("runs")) is not int
            or state["runs"] < 0
            or type(state.get("seconds")) not in {int, float}
            or not math.isfinite(state["seconds"])
            or state["seconds"] < 0
            or not isinstance(state.get("grants"), list)
        ):
            raise TestRunnerError(
                "Test execution policy changed before Session recovery."
            )
        restored, expiries = [], {}
        for row in state["grants"]:
            recipe = self._recipe(row["recipe"])
            if (
                row["identity"]["fingerprint"] != self._fingerprint(recipe)
                or not isinstance(row["roles"], list)
                or not set(row["roles"]) <= set(recipe.roles)
                or type(row["remaining"]) is not int
                or not 0 <= row["remaining"] <= recipe.max_runs
                or not math.isfinite(row["expires_at"])
            ):
                raise TestRunnerError(
                    "Saved test grant changed before Session recovery."
                )
            restored.append(
                Grant(
                    row["recipe"],
                    row["identity"]["fingerprint"],
                    row["identity"]["locked"],
                    tuple(row["roles"]),
                    row["cwd"],
                    row["task_id"],
                    time.monotonic() + row["expires_at"] - time.time(),
                    row["remaining"],
                    id=row["id"],
                )
            )
            expiries[row["id"]] = row["expires_at"]
        with self._account_lock:
            self._handoff_expiry.update(expiries)
            self.grants, self.runs, self.seconds = (
                restored,
                state["runs"],
                state["seconds"],
            )

    @classmethod
    def default(cls, workdir):
        recipes = []
        if (Path(workdir) / "tests").is_dir():
            recipes.append(
                TestRecipe(
                    "unit-tests",
                    (sys.executable, "-I", "-c", _UNITTEST),
                    "Run the project unittest suite",
                    locked_globs=(
                        "tests/**",
                        "tests/*",
                        "pyproject.toml",
                        "pytest.ini",
                        "conftest.py",
                    ),
                )
            )
        return cls(workdir, recipes)

    def _recipe(self, name):
        recipe = self.recipes.get(name)
        if recipe is None:
            raise TestRunnerError("Unknown registered test recipe.")
        return recipe

    def _cwd(self, cwd, recipe):
        cwd = canonical_root(Path(cwd))
        if cwd != self.workdir and not (
            recipe.allow_worktrees and cwd.is_relative_to(self.workdir / ".worktrees")
        ):
            raise TestRunnerError(
                "Recipe cwd must be the base workspace or a host-bound task worktree."
            )
        return cwd

    def approve(
        self, name, *, cwd=None, role=None, task_id=None, max_runs=None, expected=None
    ):
        recipe = self._recipe(name)
        target = self._cwd(cwd or self.workdir, recipe)
        if executable_identity(recipe.argv[0]) != recipe._identity:
            raise TestRunnerError(
                "Registered executable changed; reload the host registry before authorizing."
            )
        locked = (
            {
                path: info["sha256"]
                for path, info in snapshot(
                    target, recipe.locked_globs, **recipe.snapshot_limits
                ).items()
            }
            if recipe.locked_globs
            else {}
        )
        if expected is not None and expected != {
            "fingerprint": self._fingerprint(recipe),
            "locked": locked,
        }:
            raise TestRunnerError(
                "Test definition changed during approval; review the current recipe again."
            )
        runs = recipe.max_runs if max_runs is None else max_runs
        if (
            type(runs) is not int
            or not 1 <= runs <= recipe.max_runs
            or role is not None
            and role not in recipe.roles
        ):
            raise TestRunnerError("Approval cannot widen recipe roles or run limits.")
        grant = Grant(
            name,
            self._fingerprint(recipe),
            locked,
            (role,) if role else recipe.roles,
            str(target) if cwd is not None else None,
            task_id,
            time.monotonic() + recipe.approval_seconds,
            runs,
        )
        with self.lock, self._account_lock:
            self.grants.append(grant)
        return grant

    def revoke(self, name):
        with self.lock, self._account_lock:
            self.grants[:] = [grant for grant in self.grants if grant.recipe != name]

    def _grant(self, recipe, cwd, context):
        if self.authorizations:
            from homebrew_harness.runs.requests import current_approval

            if current_approval() is None:
                return self.authorizations.grant(recipe, cwd, context)
        protected = (
            {
                path: info["sha256"]
                for path, info in snapshot(
                    cwd, recipe.locked_globs, **recipe.snapshot_limits
                ).items()
            }
            if recipe.locked_globs
            else {}
        )
        return next(
            (
                grant
                for grant in reversed(self.grants)
                if grant.recipe == recipe.name
                and grant.fingerprint == self._fingerprint(recipe)
                and grant.remaining > 0
                and grant.expires > time.monotonic()
                and context.role in grant.roles
                and (grant.cwd is None or grant.cwd == str(cwd))
                and (grant.task_id is None or grant.task_id == context.task_id)
                and grant.locked == protected
            ),
            None,
        )

    def approval_identity(self, name, cwd):
        recipe = self._recipe(name)
        locked = (
            {
                path: info["sha256"]
                for path, info in snapshot(
                    cwd, recipe.locked_globs, **recipe.snapshot_limits
                ).items()
            }
            if recipe.locked_globs
            else {}
        )
        return {"fingerprint": self._fingerprint(recipe), "locked": locked}

    def permission(self, name, cwd, context):
        recipe = self._recipe(name)
        cwd = self._cwd(cwd, recipe)
        if context.role not in recipe.roles:
            return "deny", "This role is not registered for the test recipe."
        if not self.lock.acquire(blocking=False):
            return "deny", "A registered test is already running; wait for its result."
        try:
            allowed = self._grant(recipe, cwd, context) is not None
        finally:
            self.lock.release()
        review = {
            "name": name,
            "argv": list(recipe.argv),
            "cwd": str(cwd),
            "role": context.role,
            "task_id": context.task_id,
            "timeout": recipe.timeout,
            "output_bytes": recipe.output_bytes,
            "cpu_seconds": recipe.cpu_seconds,
            "locked_inputs": list(recipe.locked_globs),
            "definition_id": digest(self.approval_identity(name, cwd)),
        }
        review["execution_backend"] = self.backend.name
        if self.backend.name == "container":
            review["container_policy"] = self.backend.policy.__dict__
        return (
            ("allow", "Host test authorization matches.")
            if allowed
            else ("ask", "Approve this fixed test invocation: " + encoded(review))
        )

    def authorization_available(self, name, cwd, task_id=None):
        recipe = self._recipe(name)
        cwd = self._cwd(cwd, recipe)
        if self.authorizations:
            active = self.active_test
            if (
                active
                and active[0] == name
                and active[1] == str(cwd)
                and (task_id is None or active[2] == task_id)
            ):
                return True
            return self.authorizations.available(recipe, cwd, task_id)
        if not self.lock.acquire(blocking=False):
            return True  # A running approved command will provide its result.
        try:
            protected = (
                {
                    path: info["sha256"]
                    for path, info in snapshot(
                        cwd, recipe.locked_globs, **recipe.snapshot_limits
                    ).items()
                }
                if recipe.locked_globs
                else {}
            )
            return any(
                grant.recipe == name
                and grant.remaining > 0
                and grant.expires > time.monotonic()
                and grant.fingerprint == self._fingerprint(recipe)
                and grant.locked == protected
                and (grant.cwd is None or grant.cwd == str(cwd))
                and (
                    task_id is None or grant.task_id is None or grant.task_id == task_id
                )
                for grant in self.grants
            )
        finally:
            self.lock.release()

    def catalog(self):
        return [
            {
                "name": recipe.name,
                "description": recipe.description,
                "roles": list(recipe.roles),
                "timeout": recipe.timeout,
                "input_globs": list(recipe.input_globs),
                "locked_globs": list(recipe.locked_globs),
            }
            for recipe in self.recipes.values()
        ]

    def _save(self, reference, value):
        from homebrew_harness.execution.execution_control import cleanup_scope

        with (
            cleanup_scope(),
            SafeFile(self.workdir, reference, create_parents=True) as target,
        ):
            target.publish(redact(value).encode(), None, 0o600)

    @staticmethod
    def _kill(process, deadline=None):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                break
        try:
            process.wait(
                timeout=0.5 if deadline is None else max(0, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            pass

    def run(self, name, *, workdir=None, context=None):
        recipe = self._recipe(name)
        cwd = self._cwd(workdir or self.workdir, recipe)
        scope = current_scope()
        context = (
            context or _CONTEXT.get() or TestContext(scope.role, scope.task_id or None)
        )
        deadline = Deadline.after(
            min(recipe.timeout, max(0, self.total_seconds - self.seconds))
        ).intersect(scope.deadline)
        if scope.gateway is not None:
            deadline = deadline.intersect(scope.gateway.operation_deadline())
        if not self.lock.acquire(blocking=False):
            raise TestRunnerError(
                "A registered test is already running; wait for its result."
            )
        self._run_cancel = threading.Event()
        scope = replace(scope, cancel_events=(*scope.cancel_events, self._run_cancel))
        self.active.set()
        process = None
        started = time.monotonic()
        durable_scope = self.receipt_scope() if self.receipt_scope else None
        try:
            if (
                self.closed.is_set()
                or self.runs >= self.max_runs
                or self.seconds >= self.total_seconds
            ):
                raise TestRunnerError(
                    "TestRunner is closed or its shared run/time budget is exhausted."
                )
            grant = self._grant(recipe, cwd, context)
            if grant is None:
                raise TestRunnerError(
                    "approval_required: no current host grant matches this recipe, role, workspace and test definition."
                )
            if (
                executable_identity(recipe.argv[0]) != recipe._identity
                or executable_identity(sys.executable) != self._launcher
            ):
                raise TestRunnerError(
                    "Registered test executable changed; execution refused."
                )
            before = snapshot(cwd, recipe.input_globs, **recipe.snapshot_limits)
            if self.authorizations:
                from homebrew_harness.runs.requests import current_approval

                if current_approval() is None:
                    from homebrew_harness.storage.durable_runtime import (
                        current_operation,
                    )

                    if current_operation() is None:
                        raise TestRunnerError(
                            "Managed tests require an accepted tool reservation."
                        )
                    self.authorizations.consume_projection()
            with self._account_lock:
                grant.remaining -= 1
                self.active_test = (name, str(cwd), context.task_id)
                self.runs += 1
            run_id = "test_" + uuid.uuid4().hex
            self._active_run_id = run_id
            prefix = ".harness/tests/" + self.session_id + "/" + run_id
            with SafeFile(
                self.workdir, prefix + "/home/.keep", create_parents=True
            ) as target:
                target.publish(b"", None, 0o600)
            with SafeFile(
                self.workdir, prefix + "/tmp/.keep", create_parents=True
            ) as target:
                target.publish(b"", None, 0o600)
            environment = {
                "PATH": os.defpath,
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                **dict(recipe.env),
                "HOME": str(self.workdir / prefix / "home"),
                "TMPDIR": str(self.workdir / prefix / "tmp"),
            }
            streams = {"stdout": bytearray(), "stderr": bytearray()}
            status, after, stable, execution_error = "error", {}, False, None
            isolation = None
            try:
                relative_cwd = cwd.relative_to(self.workdir)
                with SafeFile(
                    self.workdir, (relative_cwd / ".cwd-anchor").as_posix()
                ) as target:
                    cwd_fd = os.dup(target.fds[-1])
                try:
                    if deadline.remaining() == 0 or any(
                        event.is_set() for event in scope.cancel_events
                    ):
                        raise TestRunnerError(
                            "Test deadline or cancellation reached before starting the process."
                        )
                    with self.process_lock:
                        if self.closed.is_set():
                            raise TestRunnerError(
                                "TestRunner closed before process startup."
                            )
                        from homebrew_harness.execution.execution_backend import (
                            ExecutionRequest,
                        )

                        process = self.backend.launch(
                            ExecutionRequest(
                                (sys.executable, "-I", "-c", _WORKER),
                                cwd,
                                environment,
                                deadline,
                                (cwd_fd,),
                                recipe.argv,
                                recipe.cpu_seconds,
                                recipe.file_bytes,
                                recipe.open_files,
                                recipe.input_globs,
                                before,
                                (*scope.cancel_events, self.closed),
                            )
                        )
                        self._processes.add(process)
                    specification = {
                        "argv": list(recipe.argv),
                        "env": environment,
                        "cwd_fd": cwd_fd,
                        "cpu": recipe.cpu_seconds,
                        "file": recipe.file_bytes,
                        "fds": recipe.open_files,
                        "memory": recipe.memory_mb,
                    }
                    if self.backend.name == "native":
                        process.stdin.write(encoded(specification).encode())
                        process.stdin.close()
                finally:
                    os.close(cwd_fd)
                streams = {"stdout": bytearray(), "stderr": bytearray()}
                status = "completed"
                with selectors.DefaultSelector() as selector:
                    for kind in streams:
                        pipe = getattr(process, kind)
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, kind)
                    while selector.get_map() or process.poll() is None:
                        if scope.gateway is not None:
                            scope.gateway.checkpoint()
                        if self.closed.is_set() or any(
                            event.is_set() for event in scope.cancel_events
                        ):
                            status = "cancelled"
                            break
                        if deadline.remaining() == 0:
                            status = "timeout"
                            break
                        for key, _ in selector.select(min(0.05, deadline.remaining())):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            else:
                                remaining = recipe.output_bytes - sum(
                                    map(len, streams.values())
                                )
                                streams[key.data].extend(chunk[:remaining])
                                if len(chunk) > remaining:
                                    status = "output_limit"
                                    break
                        if status != "completed":
                            break
                if status == "completed":
                    try:
                        process.wait(timeout=deadline.remaining())
                    except subprocess.TimeoutExpired:
                        status = "timeout"
                if self.closed.is_set() or any(
                    event.is_set() for event in scope.cancel_events
                ):
                    status = "cancelled"
                self._kill(
                    process, deadline.expires_at
                )  # Also stop descendants that outlive a successful parent.
                isolation = self.backend.finish(process)
                after = snapshot(cwd, recipe.input_globs, **recipe.snapshot_limits)
                stable = before == after
                if status == "completed":
                    status = (
                        "passed"
                        if process.returncode == 0
                        and stable
                        and isolation["cleanup_complete"]
                        and (
                            self.backend.name == "native"
                            or isolation.get("configuration_verified")
                        )
                        else "changed"
                        if not stable
                        else "failed"
                    )
            except (OSError, ValueError, ModelGatewayError) as error:
                status = (
                    "timeout"
                    if type(error).__name__ == "ModelDeadlineExceeded"
                    else "cancelled"
                    if isinstance(error, ModelGatewayError)
                    else "error"
                )
                execution_error = (
                    "Test verification unavailable (" + type(error).__name__ + ")."
                )
                if process is not None:
                    self._kill(process, deadline.expires_at)
                    if isolation is None:
                        isolation = self.backend.finish(process)
            receipt = {
                "run_id": run_id,
                "session_id": self.session_id,
                "recipe": name,
                "fingerprint": self._fingerprint(recipe),
                "role": context.role,
                "task_id": context.task_id,
                "cwd": cwd.relative_to(self.workdir).as_posix(),
                "status": status,
                "exit_code": process.returncode if process is not None else None,
                "started": started,
                "completed": time.monotonic(),
                "elapsed_seconds": time.monotonic() - started,
                "source_stable": stable,
                "error": execution_error,
                "inputs": after,
                "grant_id": grant.id,
                "stdout": prefix + "/stdout.txt",
                "stderr": prefix + "/stderr.txt",
            }
            receipt["execution_backend"] = self.backend.name
            receipt["isolation"] = isolation or (
                self.backend.status()
                if hasattr(self.backend, "status")
                else {
                    "backend": self.backend.name,
                    "cleanup_complete": True,
                    "started": False,
                }
            )
            if receipt["error"] is None:
                receipt.pop("error")
            for kind, data in streams.items():
                self._save(
                    receipt[kind],
                    bytes(data[: recipe.output_bytes]).decode("utf-8", "replace"),
                )
            self._save(prefix + "/receipt.json", encoded(receipt))
            if durable_scope is not None:
                from homebrew_harness.storage.durable_runtime import DurableStore

                receipt["scope"] = durable_scope
                receipt["checkpoint"] = (
                    "lead:" + durable_scope["session_id"]
                    if "session_id" in durable_scope
                    else None
                )
                db = DurableStore(self.workdir).backend()
                with db.write() as conn:
                    db._put(conn, "test_receipts", run_id, receipt)
            self._receipts[run_id] = json.loads(encoded(receipt))
            return {key: value for key, value in receipt.items() if key != "inputs"} | {
                "stdout_preview": redact(
                    bytes(streams["stdout"][:4000]).decode("utf-8", "replace")
                ),
                "stderr_preview": redact(
                    bytes(streams["stderr"][:4000]).decode("utf-8", "replace")
                ),
            }
        finally:
            with self._account_lock:
                self.seconds += max(0, time.monotonic() - started)
            if process is not None:
                self._kill(process, deadline.expires_at)
                if isolation is None:
                    self.backend.finish(process)
                if process.poll() is None:

                    def reap():
                        process.wait()
                        with self.process_lock:
                            self._processes.discard(process)

                    threading.Thread(
                        target=reap, name="test-reaper", daemon=True
                    ).start()
                else:
                    with self.process_lock:
                        self._processes.discard(process)
                if process.stdin is not None and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                for kind in ("stdout", "stderr"):
                    getattr(process, kind).close()
            self.active_test = None
            self._active_run_id = None
            self._run_cancel = None
            self.active.clear()
            self.lock.release()

    def verify(self, name, cwd, *, since=0, task_id=None):
        recipe = self._recipe(name)
        cwd = self._cwd(cwd, recipe)
        if self.active.is_set():
            return {
                "ok": False,
                "reason": "Registered verification is still running.",
                "waiting": True,
            }
        location = cwd.relative_to(self.workdir).as_posix()
        durable_scope = self.receipt_scope() if self.receipt_scope else None
        if durable_scope is not None:
            from homebrew_harness.storage.durable_runtime import DurableStore

            db = DurableStore(self.workdir).backend()
            with db.connection() as conn:
                records = [
                    json.loads(raw)
                    for (raw,) in conn.execute(
                        "SELECT value FROM records WHERE section='test_receipts' AND owner=? ORDER BY rowid",
                        ("lead:" + durable_scope["session_id"],),
                    ).fetchall()
                ]
        else:
            records = list(self._receipts.values())
        records = [
            record
            for record in records
            if record["recipe"] == name
            and record["cwd"] == location
            and (
                record.get("scope") == durable_scope
                if durable_scope is not None
                else record["started"] >= since
            )
            and (task_id is None or record["task_id"] == task_id)
        ]
        latest = (
            (records[-1] if records else None)
            if durable_scope is not None
            else max(records, key=lambda record: record["started"], default=None)
        )
        if latest is None:
            return {
                "ok": False,
                "reason": "A fresh authorized test run is required.",
                "recipe": name,
            }
        if (
            latest["status"] != "passed"
            or latest["exit_code"] != 0
            or not latest["isolation"].get("cleanup_complete")
        ):
            return {
                "ok": False,
                "reason": "Latest registered test did not pass: " + latest["status"],
                "run_id": latest["run_id"],
            }
        current = snapshot(cwd, recipe.input_globs, **recipe.snapshot_limits)
        ok = (
            latest["fingerprint"] == self._fingerprint(recipe)
            and current == latest["inputs"]
            and executable_identity(recipe.argv[0]) == recipe._identity
        )
        return {
            "ok": ok,
            "reason": "Registered suite passed for the current watched source."
            if ok
            else "Source, recipe or executable changed after verification.",
            "run_id": latest["run_id"],
            "recipe": name,
            "cwd": location,
        }

    def get(self, run_id):
        record = self._receipts.get(run_id)
        if record is None and self.receipt_scope:
            from homebrew_harness.storage.durable_runtime import DurableStore

            record = DurableStore(self.workdir).get("test_receipts", run_id)
            if record and record.get("scope") != self.receipt_scope():
                record = None
        if record is None:
            raise TestRunnerError("No host-issued test receipt exists in this session.")
        return {key: value for key, value in record.items() if key != "inputs"}

    def run_tool(self, tool_name, *, workdir, **arguments):
        if tool_name == "list_tests":
            result = {"tests": self.catalog()}
        elif tool_name == "run_tests":
            result = self.run(arguments["name"], workdir=workdir)
        elif tool_name == "get_test_result":
            result = self.get(arguments["run_id"])
        else:
            raise TestRunnerError("Unknown TestRunner tool.")
        return encoded(result)

    def activity(self):
        item = self.active_test
        if not self.active.is_set() or not item or not self._active_run_id:
            return None
        return {"id": self._active_run_id, "kind": "test", "name": item[0], "cwd": item[1],
            "task_id": item[2], "state": "stopping" if self._run_cancel and self._run_cancel.is_set() else "running", "stoppable": True}

    def stop(self, identity):
        event = self._run_cancel
        if identity != self._active_run_id or event is None:
            raise TestRunnerError("Test is no longer active in this Session.")
        event.set()
        return {"id": identity, "state": "stop_requested"}

    def close(self, timeout=0.5):
        deadline = time.monotonic() + max(0, timeout)
        self.closed.set()
        if not self.process_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return
        try:
            processes = list(self._processes)
        finally:
            self.process_lock.release()
        for process in processes:
            self._kill(process, deadline)

    def wait_closed(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while self.active.is_set() or self._processes:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            threading.Event().wait(
                0.02
                if deadline is None
                else min(0.02, max(0, deadline - time.monotonic()))
            )
        return self.backend.wait_closed(
            0
            if deadline is not None and deadline <= time.monotonic()
            else max(0, deadline - time.monotonic())
            if deadline is not None
            else None
        )


def load_test_runner(path, workdir, *, missing_ok=False):
    path = Path(path)
    if not path.exists() and missing_ok:
        return TestRunner.default(workdir)
    if path.resolve().is_relative_to(Path(workdir).resolve()) or path.is_symlink():
        raise TestRunnerError(
            "Test registry must be a host-owned file outside the workspace."
        )
    try:
        raw = json.loads(path.read_text())
        if (
            not isinstance(raw, dict)
            or set(raw) - {"version", "recipes", "execution"}
            or raw.get("version") != 1
            or not isinstance(raw.get("recipes"), list)
        ):
            raise ValueError()
        recipes, approvals = [], []
        fields = {
            field.name
            for field in TestRecipe.__dataclass_fields__.values()
            if field.init
        }
        for entry in raw["recipes"]:
            if not isinstance(entry, dict) or set(entry) - fields - {
                "authorization",
                "builtin",
            }:
                raise ValueError()
            values = dict(entry)
            authorization = values.pop("authorization", "confirm")
            if authorization not in {"allow", "confirm", "deny"}:
                raise ValueError()
            builtin = values.pop("builtin", None)
            if builtin is not None:
                if builtin != "unittest" or "argv" in values:
                    raise ValueError()
                values["argv"] = [sys.executable, "-I", "-c", _UNITTEST]
                values.setdefault(
                    "locked_globs",
                    ["tests/**", "pyproject.toml", "conftest.py", "pytest.ini"],
                )
            argv = list(values["argv"])
            if argv[0] == "{python}":
                argv[0] = sys.executable
            values["argv"] = tuple(argv)
            for name in ("roles", "input_globs", "locked_globs"):
                if name in values:
                    values[name] = tuple(values[name])
            values["env"] = tuple(values.get("env", {}).items())
            recipe = TestRecipe(**values)
            if authorization != "deny":
                recipes.append(recipe)
            if authorization == "allow":
                approvals.append(recipe.name)
        backend = None
        execution = raw.get("execution", {"backend": "native"})
        if execution != {"backend": "native"}:
            from homebrew_harness.execution.execution_backend import (
                ContainerBackend,
                ContainerPolicy,
            )

            if (
                not isinstance(execution, dict)
                or set(execution) != {"backend", "policy"}
                or execution["backend"] != "container"
            ):
                raise ValueError()
            policy = dict(execution["policy"])
            policy["argv"] = tuple(policy["argv"])
            policy["native_argv"] = tuple(
                sys.executable if value == "{python}" else value
                for value in policy["native_argv"]
            )
            backend = ContainerBackend(ContainerPolicy(**policy))
            if any(recipe.argv != backend.policy.native_argv for recipe in recipes):
                raise ValueError(
                    "A container registry binds only its exact declared native recipe."
                )
        runner = TestRunner(workdir, recipes, backend=backend)
        for name in approvals:
            runner.approve(name)
        return runner
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        raise TestRunnerError(
            "Invalid host test registry (" + type(error).__name__ + ")."
        ) from error


def execution_context(role, assignment=None):
    inherited = _CONTEXT.get()
    return TestContext(
        role,
        assignment.id
        if assignment is not None
        else inherited.task_id
        if inherited
        else None,
    )
