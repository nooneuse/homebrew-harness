"""s12: local cron matching, recoverable prompt delivery and idle dispatch."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from homebrew_harness.execution.execution_control import (
    ControlledRLock,
    cleanup_scope,
)
from homebrew_harness.execution.execution_control import (
    check as execution_check,
)
from homebrew_harness.storage.durable_runtime import DurableStore

CRON_TOOL_NAMES = ("schedule_cron", "list_crons", "cancel_cron")
FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


class CronError(ValueError):
    """A scheduling or storage error with no private connection details."""


def _field_values(field: str, minimum: int, maximum: int) -> set[int]:
    values = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(minimum, maximum + 1))
        elif re.fullmatch(r"\*/[0-9]+", part):
            step = int(part[2:])
            if step == 0:
                raise CronError("Cron steps must be positive.")
            # Match the lesson: */N selects field values divisible by N.
            values.update(
                value for value in range(minimum, maximum + 1) if value % step == 0
            )
        elif re.fullmatch(r"[0-9]+-[0-9]+", part):
            start, end = map(int, part.split("-"))
            if not minimum <= start <= end <= maximum:
                raise CronError(f"Cron range must stay within {minimum}..{maximum}.")
            values.update(range(start, end + 1))
        elif re.fullmatch(r"[0-9]+", part):
            value = int(part)
            if not minimum <= value <= maximum:
                raise CronError(f"Cron value must stay within {minimum}..{maximum}.")
            values.add(value)
        else:
            raise CronError(
                "Unsupported cron field; use *, */N, N, N-M or comma-separated values."
            )
    return values


def validate_cron(expression: str) -> str:
    if not isinstance(expression, str) or len(expression) > 500:
        raise CronError("Cron expression must be a string of at most 500 characters.")
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            "Cron expression requires five fields: minute hour day month weekday."
        )
    for field, (minimum, maximum) in zip(fields, FIELD_LIMITS):
        _field_values(field, minimum, maximum)
    return " ".join(fields)


def cron_matches(expression: str, moment: datetime) -> bool:
    fields = validate_cron(expression).split()
    accepted = [
        _field_values(field, *limits) for field, limits in zip(fields, FIELD_LIMITS)
    ]
    if (
        moment.minute not in accepted[0]
        or moment.hour not in accepted[1]
        or moment.month not in accepted[3]
    ):
        return False
    day, weekday = moment.day in accepted[2], (moment.weekday() + 1) % 7 in accepted[4]
    if fields[2] == "*":
        return weekday
    if fields[4] == "*":
        return day
    return day or weekday


@dataclass(frozen=True)
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None


class CronScheduler:
    """The disk state is committed before pending prompts become deliverable."""

    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        self.path = self.workdir / ".scheduled_tasks.json"
        self._lock = ControlledRLock()
        self.runtime = DurableStore(self.workdir)
        self._jobs: dict[str, CronJob] = {}
        self._queue: list[str] = []
        self._inflight: set[str] = set()
        raw = self._read_file()
        self._version = self._digest(raw)
        if raw is not None:
            try:
                payload = json.loads(raw)
                if not isinstance(payload, list):
                    raise CronError("Saved cron data must be an array.")
                for item in payload:
                    job = self._decode_job(item)
                    if job.id in self._jobs:
                        raise CronError("Duplicate saved cron ID.")
                    self._jobs[job.id] = job
                    if job.pending_delivery:
                        run = self.runtime.get(
                            "cron", job.id + ":" + str(job.last_fired), {}
                        )
                        if run.get("state") not in {
                            "received",
                            "unknown",
                            "executed",
                            "inspected",
                        }:
                            self._queue.append(job.id)
            except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise CronError(
                    "Cannot load scheduled tasks; repair the invalid saved file before starting."
                ) from error

    @staticmethod
    def _digest(raw: bytes | None) -> str | None:
        return hashlib.sha256(raw).hexdigest() if raw is not None else None

    @staticmethod
    def _id(job_id: str) -> str:
        if not isinstance(job_id, str) or not re.fullmatch(r"cron_[0-9a-f]{8}", job_id):
            raise CronError("Invalid cron ID; use an ID returned by schedule_cron.")
        return job_id

    @staticmethod
    def _decode_job(item: dict) -> CronJob:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "cron",
            "prompt",
            "recurring",
            "durable",
            "pending_delivery",
            "last_fired",
        }:
            raise CronError("Invalid saved cron fields.")
        CronScheduler._id(item["id"])
        validate_cron(item["cron"])
        if not isinstance(item["prompt"], str) or not item["prompt"].strip():
            raise CronError("Saved prompt must be nonempty.")
        for key in ("recurring", "durable", "pending_delivery"):
            if type(item[key]) is not bool:
                raise CronError("Saved cron flags must be booleans.")
        if not item["durable"]:
            raise CronError("Saved cron records must be durable.")
        marker = item["last_fired"]
        if marker is not None:
            if (
                not isinstance(marker, str)
                or datetime.strptime(marker, "%Y-%m-%d %H:%M").strftime(
                    "%Y-%m-%d %H:%M"
                )
                != marker
            ):
                raise CronError("Invalid saved minute marker.")
        if item["pending_delivery"] and marker is None:
            raise CronError("A pending prompt must have a trigger minute.")
        return CronJob(**item)

    def _read_file(self) -> bytes | None:
        if self.path.is_symlink():
            raise CronError("Scheduled task data must not be a symlink.")
        try:
            from homebrew_harness.security.safe_files import SafeFile

            with (
                SafeFile(self.workdir, str(self.path)) as target,
                target.open_read() as handle,
            ):
                raw = handle.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise CronError("Cron definitions exceed their bound.")
                target.verify_binding()
                return raw
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CronError("Cannot read scheduled task data.") from error

    @staticmethod
    def _durable(jobs: dict[str, CronJob]) -> list[dict]:
        return [asdict(jobs[key]) for key in sorted(jobs) if jobs[key].durable]

    def _commit(self, proposed: dict[str, CronJob]) -> None:
        payload = self._durable(proposed)
        if payload != self._durable(self._jobs):
            lock_path = self.workdir / ".scheduled_tasks.lock"
            if lock_path.is_symlink():
                raise CronError("Scheduled task lock must not be a symlink.")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            temporary = None
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise CronError(
                        "Scheduled task store is busy; retry later."
                    ) from error
                if self._digest(self._read_file()) != self._version:
                    raise CronError(
                        "Scheduled task data changed externally; restart to reload it."
                    )
                raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                )
                temp_fd, name = tempfile.mkstemp(
                    prefix=".scheduled_tasks-", suffix=".tmp", dir=self.workdir
                )
                temporary = Path(name)
                with os.fdopen(temp_fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self.path.is_symlink():
                    raise CronError("Scheduled task data must not be a symlink.")
                execution_check()
                os.replace(temporary, self.path)
                self._version = self._digest(raw)
            finally:
                os.close(fd)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self._jobs = proposed

    def schedule(
        self, cron: str, prompt: str, recurring: bool = True, durable: bool = True
    ) -> CronJob:
        cron = validate_cron(cron)
        if not isinstance(prompt, str) or not prompt.strip():
            raise CronError("Scheduled prompt must be nonempty.")
        if type(recurring) is not bool or type(durable) is not bool:
            raise CronError("recurring and durable must be booleans.")
        with self._lock:
            for _ in range(100):
                job_id = "cron_" + secrets.token_hex(4)
                if job_id not in self._jobs:
                    break
            else:
                raise CronError("Could not allocate a unique cron ID.")
            job = CronJob(job_id, cron, prompt, recurring, durable)
            self._commit({**self._jobs, job.id: job})
            return job

    def list(self) -> list[CronJob]:
        with self._lock:
            return [self._jobs[key] for key in sorted(self._jobs)]

    def cancel(self, job_id: str) -> CronJob:
        self._id(job_id)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise CronError("Cron task not found.")
            self._commit(
                {key: value for key, value in self._jobs.items() if key != job_id}
            )
            self._queue = [key for key in self._queue if key != job_id]
            self._inflight.discard(job_id)
            return job

    def poll_due(self, moment: datetime) -> list[CronJob]:
        marker = moment.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            due = [
                replace(job, pending_delivery=True, last_fired=marker)
                for job in self._jobs.values()
                if not job.pending_delivery
                and job.last_fired != marker
                and cron_matches(job.cron, moment)
            ]
            if due:
                self._commit({**self._jobs, **{job.id: job for job in due}})
                self._queue.extend(job.id for job in due)
            return due

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def take_pending(self) -> CronJob | None:
        with self._lock:
            while self._queue:
                job_id = self._queue.pop(0)
                job = self._jobs.get(job_id)
                if (
                    job is not None
                    and job.pending_delivery
                    and job_id not in self._inflight
                ):
                    self._inflight.add(job_id)
                    return job
        return None

    def restore(self, delivered: CronJob) -> None:
        if self.runtime.get(
            "cron", delivered.id + ":" + str(delivered.last_fired), {}
        ).get("state") in {"received", "unknown", "executed", "inspected"}:
            return
        with self._lock:
            self._inflight.discard(delivered.id)
            job = self._jobs.get(delivered.id)
            if (
                job is not None
                and job.pending_delivery
                and job.last_fired == delivered.last_fired
                and job.id not in self._queue
            ):
                self._queue.append(job.id)

    def received(self, delivered):
        key = delivered.id + ":" + str(delivered.last_fired)
        self.runtime.put(
            "cron",
            key,
            {
                "job_id": delivered.id,
                "minute": delivered.last_fired,
                "state": "received",
                "revision": 0,
            },
        )

    def executed(self, delivered):
        key = delivered.id + ":" + str(delivered.last_fired)
        with cleanup_scope():
            self.runtime.put(
                "cron",
                key,
                {
                    "job_id": delivered.id,
                    "minute": delivered.last_fired,
                    "state": "executed",
                    "revision": 1,
                },
            )
            self.acknowledge(delivered)

    def unresolved(self):
        return [
            key
            for key, value in self.runtime.items("cron").items()
            if value["state"] in {"received", "unknown"}
        ]

    def reconcile(self, *, inspect_unknown=False):
        runs = self.runtime.items("cron")
        for key, value in runs.items():
            status = value["state"]
            if (
                status == "executed"
                or inspect_unknown
                and status in {"received", "unknown"}
            ):
                job = self._jobs.get(value["job_id"])
                if job and job.pending_delivery and job.last_fired == value["minute"]:
                    self.acknowledge(job)
                if status != "executed":
                    self.runtime.update_fields("cron", key, {"state": "inspected"})

    def inspect_run(self, identity, revision):
        value = self.runtime.get("cron", identity)
        if (
            value is None
            or value.get("revision", 0) != revision
            or value["state"] not in {"received", "unknown"}
        ):
            raise CronError("Cron inspection is stale.")
        # Explicit host control is serialized with scheduled delivery by Session.
        with cleanup_scope():
            job = self._jobs.get(value["job_id"])
            if job and job.pending_delivery and job.last_fired == value["minute"]:
                self.acknowledge(job)
            self.runtime.update_fields(
                "cron", identity, {"state": "inspected", "revision": revision + 1}
            )

    def acknowledge(self, delivered: CronJob) -> None:
        with self._lock:
            job = self._jobs.get(delivered.id)
            if (
                job is None
                or not job.pending_delivery
                or job.last_fired != delivered.last_fired
            ):
                raise CronError(
                    "Scheduled prompt was cancelled or is no longer pending."
                )
            proposed = dict(self._jobs)
            if job.recurring:
                proposed[job.id] = replace(job, pending_delivery=False)
            else:
                proposed.pop(job.id)
            self._commit(proposed)
            self._inflight.discard(job.id)
            self._queue = [key for key in self._queue if key != job.id]

    def run_tool(self, name: str, *, workdir: Path, **arguments) -> str:
        if workdir.resolve() != self.workdir:
            raise CronError("Cron scheduler belongs to a different working directory.")
        if name == "schedule_cron":
            result = asdict(self.schedule(**arguments))
        elif name == "list_crons":
            result = {
                "jobs": [asdict(job) for job in self.list()],
                "clock": "process local time",
            }
        elif name == "cancel_cron":
            result = {"cancelled": asdict(self.cancel(arguments["job_id"]))}
        else:
            raise CronError("Unknown cron tool.")
        return json.dumps(result, ensure_ascii=False)
