"""Host-selected execution backends. Container requests never fall back to native."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from homebrew_harness.execution.execution_control import check
from homebrew_harness.execution.model_gateway import Deadline


class BackendUnavailable(ValueError):
    pass


def _control(argv, timeout, cancel_events=(), retain=None):
    from homebrew_harness.execution.background import ShellRunner
    from homebrew_harness.execution.execution_control import cleanup_scope

    runner = ShellRunner()
    try:
        with cleanup_scope():
            return runner.run(
                argv,
                Path("/"),
                {"PATH": os.defpath},
                max(0.001, timeout),
                output_bytes=65536,
                cancel_events=cancel_events,
            )
    finally:
        runner.close(0.2)
        if not runner.wait_closed(0) and retain is not None:
            retain(runner)


@dataclass(frozen=True)
class ExecutionRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: dict
    deadline: Deadline
    pass_fds: tuple[int, ...] = ()
    recipe_argv: tuple[str, ...] = ()
    cpu_seconds: int = 120
    file_bytes: int = 64000000
    open_files: int = 256
    input_globs: tuple[str, ...] = ("**/*",)
    source_snapshot: dict | None = None
    cancel_events: tuple = ()
    owner_root: Path | None = None


class NativeBackend:
    name = "native"

    def fingerprint(self):
        return "native-v1"

    def launch(self, request):
        check(request.deadline, request.cancel_events)
        return subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            env=request.env,
            pass_fds=request.pass_fds,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def finish(self, process):
        return {"backend": "native", "os_isolation": False, "cleanup_complete": True}

    def wait_closed(self, timeout=0):
        return True


@dataclass(frozen=True)
class ContainerPolicy:
    cli: str
    image: str
    argv: tuple[str, ...]
    native_argv: tuple[str, ...]
    cpus: float = 1
    memory_mb: int = 512
    pids: int = 64
    uid: int = 65534
    workspace_writable: bool = False
    workspace_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if not Path(self.cli).is_absolute() or not re.fullmatch(
            r"(?:[A-Za-z0-9._/-]+@)?sha256:[0-9a-f]{64}", self.image
        ):
            raise ValueError(
                "Container policy needs an absolute local CLI and an immutable image digest."
            )
        if (
            not self.argv
            or not self.native_argv
            or not self.argv[0].startswith("/")
            or any(
                not isinstance(arg, str) or "\0" in arg
                for arg in (*self.argv, *self.native_argv)
            )
        ):
            raise ValueError(
                "Container command must be a fixed host-selected argv inside the image."
            )
        if (
            not 0.1 <= self.cpus <= 8
            or not 64 <= self.memory_mb <= 4096
            or not 16 <= self.pids <= 256
            or not 1 <= self.uid <= 65534
            or type(self.workspace_writable) is not bool
            or type(self.workspace_bytes) is not int
            or not 1024 * 1024 <= self.workspace_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError(
                "Container resource policy exceeds the supported host bounds."
            )


class ContainerBackend:
    name = "container"

    def __init__(self, policy):
        self.policy = policy
        self.lock = threading.Lock()
        self.active = {}
        self.pending = {}
        self.unknown_create = set()
        self.rejected_create = set()
        self.requests = {}
        self.control_runners = []
        self.cli_identity = None
        if Path(policy.cli).is_file() and os.access(policy.cli, os.X_OK):
            from homebrew_harness.execution.test_runner import executable_identity

            self.cli_identity = executable_identity(policy.cli)

    def retain_control(self, runner):
        with self.lock:
            self.control_runners.append(runner)

    def _control(self, argv, timeout, cancel_events=()):
        return _control(argv, timeout, cancel_events, retain=self.retain_control)

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                {**self.policy.__dict__, "cli_identity": self.cli_identity},
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def probe(self, timeout=2, cancel_events=()):
        if self.cli_identity is None:
            return {
                "available": False,
                "reason": "container_cli_missing",
                "os_isolation_verified": False,
            }
        from homebrew_harness.execution.test_runner import executable_identity

        if executable_identity(self.policy.cli) != self.cli_identity:
            return {
                "available": False,
                "reason": "container_cli_changed",
                "os_isolation_verified": False,
            }
        try:
            result = self._control(
                [self.policy.cli, "info", "--format", "{{.OSType}}"],
                max(0.001, min(2, timeout)),
                cancel_events,
            )
            available = result.returncode == 0 and result.stdout.strip() == "linux"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            available = False
        return {
            "available": available,
            "reason": None if available else "linux_container_daemon_unavailable",
            "os_isolation_verified": False,
        }

    def _inspect(self, name, timeout=1):
        try:
            result = self._control(
                [self.policy.cli, "inspect", name], max(0.001, timeout)
            )
            if result.returncode != 0 or getattr(result, "output_limit", False):
                return None
            values = json.loads(result.stdout)
            return (
                values[0]
                if len(values) == 1 and values[0].get("Name") == "/" + name
                else None
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _verified(self, value, name, temporary, request, image):
        if value is None:
            return False
        config = value["Config"]
        host = value["HostConfig"]
        labels = config.get("Labels") or {}
        mounts = value.get("Mounts", [])
        source = str(temporary / "workspace")
        destination = "/inputs" if self.policy.workspace_writable else "/workspace"
        workspace = [m for m in mounts if m.get("Destination") == destination]
        return (
            value.get("Image") == image
            and labels.get("homebrew-harness.run") == name
            and labels.get("homebrew-harness.managed") == "1"
            and config.get("User") == str(self.policy.uid)
            and config.get("Entrypoint") == [self.policy.argv[0]]
            and (config.get("Cmd") or []) == list(self.policy.argv[1:])
            and host.get("NetworkMode") == "none"
            and host.get("ReadonlyRootfs") is True
            and not host.get("Privileged")
            and host.get("PidMode") != "host"
            and host.get("IpcMode") != "host"
            and not host.get("CapAdd")
            and "ALL" in (host.get("CapDrop") or [])
            and any(
                x.startswith("no-new-privileges") for x in host.get("SecurityOpt", [])
            )
            and host.get("Memory") == self.policy.memory_mb * 1024 * 1024
            and host.get("MemorySwap") == self.policy.memory_mb * 1024 * 1024
            and host.get("PidsLimit") == self.policy.pids
            and host.get("NanoCpus") == int(self.policy.cpus * 1_000_000_000)
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and not host.get("VolumesFrom")
            and set(host.get("Tmpfs", {}))
            == ({"/tmp", "/workspace"} if self.policy.workspace_writable else {"/tmp"})
            and (
                not self.policy.workspace_writable
                or all(
                    flag in host["Tmpfs"]["/workspace"].split(",")
                    for flag in (
                        "nosuid",
                        "nodev",
                        "exec",
                        "mode=0700",
                        "uid=" + str(self.policy.uid),
                        "size=" + str(self.policy.workspace_bytes),
                    )
                )
            )
            and all(
                flag in host["Tmpfs"]["/tmp"]
                for flag in ("nosuid", "nodev", "noexec", "size=64m")
            )
            and len(workspace) == 1
            and workspace[0].get("Source") == source
            and workspace[0].get("RW") is False
            and (
                not self.policy.workspace_writable
                or all(
                    m.get("Type") == "tmpfs" and m.get("RW") is True
                    for m in mounts
                    if m.get("Destination") == "/workspace"
                )
            )
            and all(
                m.get("Destination")
                in (
                    {"/inputs", "/workspace", "/tmp"}
                    if self.policy.workspace_writable
                    else {"/workspace", "/tmp"}
                )
                for m in mounts
            )
            and all(
                any(
                    u.get("Name") == name
                    and u.get("Soft") == limit
                    and u.get("Hard") == limit
                    for u in host.get("Ulimits", [])
                )
                for name, limit in (
                    ("cpu", request.cpu_seconds),
                    ("fsize", request.file_bytes),
                    ("nofile", request.open_files),
                )
            )
        )

    def _snapshot(self, request):
        from homebrew_harness.execution.source_snapshot import snapshot

        return snapshot(request.cwd, request.input_globs)

    def _prepare_staging(self, staging):
        if not self.policy.workspace_writable:
            return
        paths = [staging, *staging.rglob("*")]
        for path in paths:
            if path.stat().st_uid != self.policy.uid:
                try:
                    os.chown(path, self.policy.uid, -1)
                except PermissionError:
                    raise BackendUnavailable(
                        "The host cannot assign the sandbox copy to its approved non-root UID."
                    ) from None
            path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)

    def launch(self, request):
        check(request.deadline, request.cancel_events)
        if request.recipe_argv != self.policy.native_argv:
            raise BackendUnavailable(
                "Container command is not bound to this host recipe."
            )
        if not self.probe(request.deadline.remaining() or 0.001, request.cancel_events)[
            "available"
        ]:
            raise BackendUnavailable(
                "Container backend unavailable; host execution was refused."
            )
        from homebrew_harness.security.safe_files import SafeFile
        from homebrew_harness.storage.storage_admin import publish

        from homebrew_harness.security.safe_files import canonical_root

        owner_root = canonical_root(request.owner_root or request.cwd)
        if not request.cwd.resolve().is_relative_to(owner_root):
            raise BackendUnavailable(
                "Container inputs must belong to their owning runtime."
            )
        source = self._snapshot(request)
        if request.source_snapshot is not None and source != request.source_snapshot:
            raise BackendUnavailable(
                "Declared test inputs changed before container preparation."
            )
        temporary = Path(tempfile.mkdtemp(prefix="harness-container-")).resolve()
        temporary.chmod(0o755)
        staging = temporary / "workspace"
        staging.mkdir(mode=0o755)
        name = "harness-" + uuid.uuid4().hex
        created = False
        create_started = False
        process = None
        try:
            for path, expected in source.items():
                check(request.deadline, request.cancel_events)
                with SafeFile(request.cwd, path) as target:
                    data, identity, mode = target.snapshot(max_bytes=32_000_000)
                if (
                    hashlib.sha256(data).hexdigest() != expected["sha256"]
                    or list(identity[:-1]) != expected["version"]
                    or bool(mode & 0o111) != expected["executable"]
                ):
                    raise BackendUnavailable(
                        "Declared inputs changed while copying the isolated workspace."
                    )
                destination = staging / path
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                destination.write_bytes(data)
                destination.chmod(0o555 if expected["executable"] else 0o444)
            self._prepare_staging(staging)
            policy = self.policy
            remaining = request.deadline.remaining()
            image_result = self._control(
                [policy.cli, "image", "inspect", policy.image, "--format", "{{.Id}}"],
                max(0.001, min(2, remaining if remaining is not None else 2)),
                request.cancel_events,
            )
            if image_result.returncode != 0:
                raise BackendUnavailable(
                    "Immutable container image is unavailable; no image pull or native fallback was attempted."
                )
            image = image_result.stdout.strip()
            command = [
                policy.cli,
                "create",
                "--pull=never",
                "--name",
                name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                str(policy.uid),
                "--cpus",
                str(policy.cpus),
                "--memory",
                str(policy.memory_mb) + "m",
                "--memory-swap",
                str(policy.memory_mb) + "m",
                "--pids-limit",
                str(policy.pids),
                "--ulimit",
                "nofile=" + str(request.open_files) + ":" + str(request.open_files),
                "--ulimit",
                "cpu=" + str(request.cpu_seconds) + ":" + str(request.cpu_seconds),
                "--ulimit",
                "fsize=" + str(request.file_bytes) + ":" + str(request.file_bytes),
                "--log-driver=none",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--mount",
                "type=bind,src="
                + str(staging)
                + ",dst="
                + ("/inputs" if policy.workspace_writable else "/workspace")
                + ",readonly",
                "--workdir",
                "/workspace",
                "--label",
                "homebrew-harness.managed=1",
                "--label",
                "homebrew-harness.run=" + name,
                "--entrypoint",
                policy.argv[0],
                "--env",
                "HOME=/tmp",
                "--env",
                "TMPDIR=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONNOUSERSITE=1",
            ]
            command.extend(self._create_options(request))
            if policy.workspace_writable:
                command.extend(
                    [
                        "--tmpfs",
                        "/workspace:rw,nosuid,nodev,exec,mode=0700,uid="
                        + str(policy.uid)
                        + ",size="
                        + str(policy.workspace_bytes),
                    ]
                )
            for key, value in request.env.items():
                if key not in {"HOME", "TMPDIR", "PATH"}:
                    command.extend(["--env", key + "=" + value])
            command.extend([policy.image, *policy.argv[1:]])
            journal = {
                "format": 1,
                "name": name,
                "staging": str(temporary),
                "policy": self.fingerprint(),
                "phase": "prepared",
            }
            publish(owner_root, ".harness/containers/" + name + ".json", journal)
            check(request.deadline, request.cancel_events)
            create_started = True
            creation = self._control(
                command, request.deadline.remaining() or 0.001, request.cancel_events
            )
            if creation.returncode != 0:
                if self._precreation_rejection(creation):
                    self.rejected_create.add(name)
                raise BackendUnavailable(
                    "Container creation failed; no task code was started."
                )
            created = True
            check(request.deadline, request.cancel_events)
            configuration = self._inspect(
                name, min(2, request.deadline.remaining() or 0.001)
            )
            if not self._verified(configuration, name, temporary, request, image):
                raise BackendUnavailable(
                    "Container configuration did not match its authorized isolation policy."
                )
            publish(
                owner_root,
                ".harness/containers/" + name + ".json",
                {**journal, "phase": "created", "configuration_verified": True},
            )
            check(request.deadline, request.cancel_events)
            process = self._start_process(name, staging, request)
            with self.lock:
                self.active[process.pid] = (name, temporary, owner_root)
                self.requests[process.pid] = (request, image)
            publish(
                owner_root,
                ".harness/containers/" + name + ".json",
                {**journal, "phase": "running", "configuration_verified": True},
            )
            return process
        except BaseException as error:
            if process is not None:
                from homebrew_harness.execution.background import ShellRunner

                ShellRunner._stop(process)
                self.finish(process)
            elif create_started:
                if not created:
                    self.unknown_create.add(name)
                if not self._remove(name, temporary, owner_root):
                    with self.lock:
                        self.pending[name] = (temporary, owner_root)
            else:
                shutil.rmtree(temporary)
            if isinstance(error, subprocess.TimeoutExpired):
                check(request.deadline, request.cancel_events)
                raise BackendUnavailable(
                    "Container control deadline reached; inspect pending cleanup."
                ) from None
            raise

    def _create_options(self, request):
        return []

    def _start_process(self, name, staging, request):
        return subprocess.Popen(
            [self.policy.cli, "start", "--attach", name],
            cwd=staging,
            env={"PATH": os.defpath},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @staticmethod
    def _precreation_rejection(result):
        # This daemon validation error occurs before a container is created.
        # Transport errors and incomplete control output remain ambiguous.
        return (
            result.returncode == 1
            and not getattr(result, "output_limit", False)
            and (result.stderr or "").startswith(
                'Error response from daemon: invalid mount config for type '
            )
        )

    def _absent(self, name, timeout):
        listing = self._control(
            [
                self.policy.cli,
                "container",
                "ls",
                "-a",
                "--filter",
                "name=" + name,
                "--format",
                "{{.Names}}",
            ],
            max(0.001, timeout),
        )
        return (
            listing.returncode == 0
            and not getattr(listing, "output_limit", False)
            and name not in listing.stdout.splitlines()
        )

    def _remove(self, name, temporary, root, timeout=1):
        deadline = Deadline.after(timeout)
        absent = False
        try:
            if name in self.unknown_create:
                observed = self._inspect(name, deadline.remaining() or 0.001)
                if observed is None:
                    if (
                        name not in self.rejected_create
                        or deadline.remaining() == 0
                        or not self._absent(name, deadline.remaining())
                    ):
                        return False
                    absent = True
                else:
                    labels = observed.get("Config", {}).get("Labels") or {}
                    if labels.get("homebrew-harness.run") != name:
                        return False
                self.unknown_create.discard(name)
                self.rejected_create.discard(name)
            if not absent:
                result = self._control(
                    [self.policy.cli, "rm", "-f", name],
                    max(0.001, deadline.remaining() or 0.001),
                )
                if result.returncode != 0:
                    if deadline.remaining() == 0 or not self._absent(
                        name, deadline.remaining()
                    ):
                        return False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        from homebrew_harness.execution.execution_control import cleanup_scope
        from homebrew_harness.storage.storage_admin import publish

        try:
            with cleanup_scope():
                if temporary.exists():
                    shutil.rmtree(temporary)
                publish(
                    root,
                    ".harness/containers/" + name + ".json",
                    {
                        "format": 1,
                        "name": name,
                        "policy": self.fingerprint(),
                        "phase": "removed",
                    },
                )
        except (OSError, ValueError):
            return False
        return True

    def finish(self, process):
        with self.lock:
            entry = self.active.pop(process.pid, None)
            details = self.requests.pop(process.pid, None)
            if entry is not None:
                self.pending[entry[0]] = (entry[1], entry[2])
        if entry is None:
            return self.status()
        name, temporary, root = entry
        request, image = details
        try:
            verified = self._verified(
                self._inspect(name), name, temporary, request, image
            )
        except Exception:
            verified = False
        try:
            complete = self._remove(name, temporary, root)
        except Exception:
            complete = False
        if complete:
            with self.lock:
                self.pending.pop(name, None)
        return {
            "backend": "container",
            "os_isolation_requested": True,
            "configuration_verified": verified,
            "os_isolation_verified": verified,
            "cleanup_complete": complete
            and all(r.wait_closed(0) for r in self.control_runners),
            "policy_fingerprint": self.fingerprint(),
            "pending_container": None if complete else name,
        }

    def status(self):
        with self.lock:
            return {
                "backend": "container",
                "os_isolation_requested": True,
                "configuration_verified": False,
                "os_isolation_verified": False,
                "cleanup_complete": not self.active
                and not self.pending
                and all(r.wait_closed(0) for r in self.control_runners),
            }

    def wait_closed(self, timeout=0):
        deadline = Deadline.after(timeout)
        while True:
            with self.lock:
                pending = list(self.pending.items())
                if (
                    not self.active
                    and not pending
                    and all(r.wait_closed(0) for r in self.control_runners)
                ):
                    return True
            for name, (temporary, root) in pending:
                if deadline.remaining() == 0:
                    return False
                if self._remove(
                    name,
                    temporary,
                    root,
                    deadline.remaining() if deadline.remaining() is not None else 1,
                ):
                    with self.lock:
                        self.pending.pop(name, None)
            if timeout is not None:
                with self.lock:
                    return (
                        not self.active
                        and not self.pending
                        and all(r.wait_closed(0) for r in self.control_runners)
                    )
            threading.Event().wait(0.5)
