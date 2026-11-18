"""Importable callbacks for actual process-hook lifecycle tests."""

import time


def slow_hook(event):
    (event.workdir / 'hook-started').write_text('started')
    time.sleep(10)


def inspect_hook(event):
    if event.arguments['data'] != {'kind': 'tool_event', 'value': 7}:
        raise AssertionError('Argument transport changed the input mapping')
    return None
