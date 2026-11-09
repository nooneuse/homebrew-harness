"""Bounded host Goal input; execution uses the validated snapshot."""

import json
import os
import stat
from pathlib import Path

from .hard_conditions import validate_spec


class GoalInputError(ValueError):
    pass


def load_goal_input(path, *, max_bytes=1024 * 1024):
    path = Path(path).expanduser()
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("Goal input must be a bounded regular file.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(max_bytes + 1)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)

        def version(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if (
            len(raw) > max_bytes
            or version(before) != version(after)
            or version(after) != version(named)
        ):
            raise ValueError("Goal input changed while reading.")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate Goal input field.")
                result[key] = value
            return result

        def nonfinite(_value):
            raise ValueError("Nonfinite JSON is not supported.")

        return validate_spec(
            json.loads(
                raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite
            )
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise GoalInputError(
            "Cannot read valid --goal-spec input (" + type(error).__name__ + ")."
        ) from error
    finally:
        if fd is not None:
            os.close(fd)
