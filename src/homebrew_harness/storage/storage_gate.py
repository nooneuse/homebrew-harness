"""Cross-process host maintenance gate; nested operations share one lock."""

import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from homebrew_harness.execution.execution_control import operation_deadline
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.runtime.limits import current_limits
from homebrew_harness.storage.persistence import storage_check

_HELD = ContextVar("storage_gates", default={})


@contextmanager
def storage_gate(root, *, exclusive=False):
    started = time.monotonic()
    deadline = operation_deadline(current_limits().storage_lock_seconds)
    def checked():
        storage_check(deadline, phase="maintenance_lock", limit=current_limits().storage_lock_seconds, started=started)
    checked()
    root = canonical_root(root)
    held = _HELD.get()
    key = str(root)
    identity = (os.getpid(), threading.get_ident())
    if key in held and held[key][1] == identity:
        if exclusive and not held[key][0]:
            raise ValueError(
                "Cannot upgrade an active storage operation to maintenance."
            )
        yield
        return
    with SafeFile(
        root, ".harness/runtime/maintenance.lock", create_parents=True
    ) as target:
        while True:
            checked()
            try:
                fd = os.open(
                    target.parts[-1],
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=target.fds[-1],
                )
                break
            except FileNotFoundError:
                try:
                    fd = os.open(
                        target.parts[-1],
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_RDWR
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK,
                        0o600,
                        dir_fd=target.fds[-1],
                    )
                    break
                except (FileExistsError, FileNotFoundError):
                    if deadline.remaining() == 0:
                        checked()
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError(
                    "Storage maintenance lock must be a private regular file."
                )
            while True:
                checked()
                try:
                    fcntl.flock(
                        fd,
                        (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    if deadline.remaining() == 0:
                        checked()
                    time.sleep(min(0.02, deadline.remaining()))
            checked()
            target.verify_binding()
            token = _HELD.set({**held, key: (exclusive, identity)})
            try:
                yield
            finally:
                _HELD.reset(token)
        finally:
            os.close(fd)


def storage_operation(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with storage_gate(self.workdir):
            return function(self, *args, **kwargs)

    return wrapped
