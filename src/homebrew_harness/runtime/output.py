"""Context-local rendering; model and MCP protocol streams remain untouched."""

import builtins
from contextlib import contextmanager
from contextvars import ContextVar

_OUTPUT = ContextVar("harness_output", default=None)


def print(*values, sep=" ", end="\n", file=None, flush=False):
    sink = _OUTPUT.get()
    if sink is None:
        return builtins.print(*values, sep=sep, end=end, file=file, flush=flush)
    if sink is not False:
        text = sep.join(str(value) for value in values) + end
        # Rendering is observation. It cannot decide tool acceptance or cleanup.
        try:
            sink(text)
        except Exception:
            pass


@contextmanager
def output_scope(sink=False):
    token = _OUTPUT.set(sink)
    try:
        yield
    finally:
        _OUTPUT.reset(token)
