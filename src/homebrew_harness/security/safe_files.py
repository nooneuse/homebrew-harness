"""POSIX descriptor-relative file access and atomic, version-checked writes."""

import hashlib
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from homebrew_harness.security.data_boundary import sensitive_path


class FilePublicationError(OSError):
    """A file was published, but durability or local cleanup failed."""

    def __init__(self, path, data, *, durable):
        super().__init__("File was published; verify its contents before retrying.")
        self.outcome = "partial" if durable else "unknown"
        self.details = {
            "path": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "published": True,
            "durability_confirmed": durable,
        }


def canonical_root(root: Path) -> Path:
    candidate = root.absolute()
    before = os.stat(candidate, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("Working directory must remain a real directory.")
    resolved = candidate.resolve()
    after = os.stat(candidate, follow_symlinks=False)
    selected = os.stat(resolved, follow_symlinks=False)
    identity = lambda info: (info.st_dev, info.st_ino)
    if (
        not stat.S_ISDIR(after.st_mode)
        or identity(before) != identity(after)
        or identity(after) != identity(selected)
    ):
        raise ValueError("Working directory changed during access.")
    return resolved


def relative_parts(path: str, root: Path) -> tuple[str, ...]:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            raise ValueError(
                "File paths must stay inside the working directory."
            ) from None
    if ".." in candidate.parts or not candidate.parts:
        raise ValueError("Use a file path inside the working directory without '..'.")
    if sensitive_path(candidate.parts):
        raise ValueError(
            "Sensitive configuration and credential paths are not exposed to file tools."
        )
    return candidate.parts


class SafeFile:
    def __init__(self, root: Path, path: str, *, create_parents: bool = False):
        self.root = canonical_root(root)
        candidate = Path(path)
        if candidate.is_absolute() and candidate.is_relative_to(root.absolute()):
            candidate = candidate.relative_to(root.absolute())
        self.parts = relative_parts(str(candidate), self.root)
        self.create_parents = create_parents
        self.fds = []

    def __enter__(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            self.fds.append(os.open(self.root, flags))
            for part in self.parts[:-1]:
                if self.create_parents:
                    try:
                        mode = (
                            0o700
                            if self.parts[0]
                            in {".harness", ".tasks", ".memory", ".mailboxes"}
                            else 0o755
                        )
                        os.mkdir(part, mode, dir_fd=self.fds[-1])
                    except FileExistsError:
                        pass
                self.fds.append(os.open(part, flags, dir_fd=self.fds[-1]))
            self.verify_binding()
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_args):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def verify_binding(self):
        """Detect replacement of an opened parent before reading or publishing."""
        for index, fd in enumerate(self.fds):
            current = (
                os.stat(self.root, follow_symlinks=False)
                if index == 0
                else os.stat(
                    self.parts[index - 1],
                    dir_fd=self.fds[index - 1],
                    follow_symlinks=False,
                )
            )
            opened = os.fstat(fd)
            if not stat.S_ISDIR(current.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise ValueError(
                    "File parent changed during access; retry with the current workspace."
                )

    @contextmanager
    def open_read(self):
        self.verify_binding()
        fd = os.open(
            self.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=self.fds[-1],
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("File tools require a regular file.")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)

    def snapshot(self, *, max_bytes=64 * 1024 * 1024):
        try:
            with self.open_read() as stream:
                from homebrew_harness.execution.execution_control import check

                chunks, size = [], 0
                info = os.fstat(stream.fileno())
                if info.st_size > max_bytes:
                    raise ValueError("File snapshot exceeds the host size limit.")
                while True:
                    check()
                    chunk = stream.read(min(65536, max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("File snapshot exceeds the host size limit.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                info = os.fstat(stream.fileno())
                version = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    hashlib.sha256(data).digest(),
                )
                return data, version, stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            return None, None, 0o644

    def unlink(self, version=None):
        from homebrew_harness.execution.execution_control import check

        check()
        self.verify_binding()
        if version is not None and self.snapshot()[1] != version:
            raise ValueError("File changed before removal.")
        os.unlink(self.parts[-1], dir_fd=self.fds[-1])
        os.fsync(self.fds[-1])

    def publish(self, data: bytes, version, mode: int):
        from homebrew_harness.execution.execution_control import check

        check()
        self.verify_binding()
        temporary = ".harness-write-" + uuid.uuid4().hex
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=self.fds[-1],
        )
        published = durable = False
        failure = None
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            self.verify_binding()
            if self.snapshot()[1] != version:
                raise ValueError(
                    "File changed while preparing this update; read it again before retrying."
                )
            check()
            if version is None:
                # Exclusive publication never replaces a concurrently created file.
                os.link(
                    temporary,
                    self.parts[-1],
                    src_dir_fd=self.fds[-1],
                    dst_dir_fd=self.fds[-1],
                    follow_symlinks=False,
                )
            else:
                os.replace(
                    temporary,
                    self.parts[-1],
                    src_dir_fd=self.fds[-1],
                    dst_dir_fd=self.fds[-1],
                )
            published = True
            os.fsync(self.fds[-1])
            durable = True
        except BaseException as error:
            failure = error
        finally:
            try:
                os.close(fd)
            except BaseException as error:
                failure = failure or error
            try:
                os.unlink(temporary, dir_fd=self.fds[-1])
            except FileNotFoundError:
                pass
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            if published and isinstance(failure, Exception):
                raise FilePublicationError(
                    "/".join(self.parts), data, durable=durable
                ) from failure
            raise failure
