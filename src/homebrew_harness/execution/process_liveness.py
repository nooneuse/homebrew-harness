"""Process existence is distinct from an exited Linux zombie's retained PID."""

import os
import sys
from pathlib import Path


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "linux":
        try:
            with Path(f"/proc/{pid}/stat").open() as stream:
                value = stream.read(4096)
            if value.rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                return False
        except (OSError, UnicodeError, IndexError):
            pass
    return True
