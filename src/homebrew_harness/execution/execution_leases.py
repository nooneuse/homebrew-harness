"""Live POSIX locks, with reference sharing only inside one trusted host group."""

import errno
import fcntl
import hashlib
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from homebrew_harness.security.safe_files import SafeFile, canonical_root

_CURRENT = ContextVar("execution_leases", default=None)


@contextmanager
def lease_scope(leases):
    token = _CURRENT.set(leases)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class ExecutionLeases:
    def __init__(self, workdir):
        self.workdir = canonical_root(workdir)
        self.instance = uuid.uuid4().hex
        self._held = {}
        self._lock = threading.RLock()

    def acquire(self, resource: str):
        with self._lock:
            if resource in self._held:
                if not self.held(resource):
                    raise ValueError(
                        "Execution lease descriptor was lost; explicitly recover before acquiring it again."
                    )
                fd, count, identity = self._held[resource]
                self._held[resource] = (fd, count + 1, identity)
                return
            name = hashlib.sha256(resource.encode()).hexdigest()
            with SafeFile(
                self.workdir, ".harness/leases/" + name + ".lock", create_parents=True
            ) as target:
                fd = os.open(
                    target.parts[-1],
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=target.fds[-1],
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    target.verify_binding()
                except BaseException as error:
                    os.close(fd)
                    if isinstance(error, BlockingIOError):
                        raise ValueError(
                            "Execution lease is held by another live host; wait for it to stop."
                        ) from None
                    raise
            info = os.fstat(fd)
            self._held[resource] = (fd, 1, (info.st_dev, info.st_ino))

    def descriptor(self, resource: str):
        """Transfer a held lock to a trusted child; it remains live until child exit."""
        with self._lock:
            if not self.held(resource):
                raise ValueError("Execution lease is not held.")
            return self._held[resource][0]

    def release(self, resource: str):
        with self._lock:
            if not self.held(resource):
                return
            fd, count, identity = self._held[resource]
            if count > 1:
                self._held[resource] = (fd, count - 1, identity)
            else:
                os.close(fd)
                del self._held[resource]

    def held(self, resource: str) -> bool:
        with self._lock:
            entry = self._held.get(resource)
            if entry is None:
                return False
            try:
                info = os.fstat(entry[0])
                present = (info.st_dev, info.st_ino) == entry[2]
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
                present = False
            if not present:
                self._held.pop(resource, None)
            return present

    @contextmanager
    def use(self, resource: str):
        self.acquire(resource)
        try:
            yield
        finally:
            self.release(resource)

    def close(self):
        with self._lock:
            failure = None
            for resource in list(self._held):
                if not self.held(resource):
                    continue
                try:
                    os.close(self._held[resource][0])
                except OSError as error:
                    failure = failure or error
                else:
                    self._held.pop(resource, None)
            if failure:
                raise failure


@contextmanager
def file_update_lease(workdir, path):
    leases = _CURRENT.get() or ExecutionLeases(workdir)
    from homebrew_harness.security.safe_files import relative_parts

    root = canonical_root(workdir)
    candidate = Path(path)
    if candidate.is_absolute() and candidate.is_relative_to(workdir.absolute()):
        candidate = candidate.relative_to(workdir.absolute())
    resource = (
        "file:" + str(root) + ":" + "/".join(relative_parts(str(candidate), root))
    )
    with leases.use("workspace:" + str(root)), leases.use(resource):
        yield


@contextmanager
def file_update_batch(workdir):
    """Share one host lease identity while holding several file leases together."""
    current = _CURRENT.get()
    leases = current or ExecutionLeases(workdir)
    try:
        with lease_scope(leases):
            yield
    finally:
        if current is None:
            leases.close()
