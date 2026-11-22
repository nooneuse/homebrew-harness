from homebrew_harness.runtime.limits import current_limits
"""Opt-in process ownership for long host hooks; no additional dependencies."""

import hashlib
import inspect
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from homebrew_harness.execution.background import ShellRunner


def encode_argument(value):
    from homebrew_harness.runtime.hooks import ToolEvent
    if isinstance(value, ToolEvent):
        return {'kind': 'tool_event', 'name': value.name,
                'arguments': encode_argument(value.arguments),
                'workdir': str(value.workdir), 'call_id': value.call_id}
    if isinstance(value, Mapping):
        return {'kind': 'mapping', 'items': [[key, encode_argument(item)] for key, item in value.items()]}
    if isinstance(value, (list, tuple)):
        return [encode_argument(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError('Process hooks accept JSON values and ToolEvent arguments only.')


WORKER = r'''import hashlib,importlib,json,sys
from pathlib import Path
request=json.load(sys.stdin)
sys.path[:0]=request['paths']
source=Path(request['source'])
if hashlib.sha256(source.read_bytes()).hexdigest()!=request['sha256']:
    raise RuntimeError('Registered hook source changed')
callback=importlib.import_module(request['module'])
for name in request['qualname'].split('.'):
    callback=getattr(callback,name)
def decode(value):
    if isinstance(value,list): return [decode(item) for item in value]
    if isinstance(value,dict):
        if value.get('kind')=='tool_event':
            from homebrew_harness.runtime.hooks import ToolEvent
            return ToolEvent(value['name'],decode(value['arguments']),Path(value['workdir']),value['call_id'])
        if value.get('kind')=='mapping': return {key:decode(item) for key,item in value['items']}
        raise ValueError('Unknown hook transport value')
    return value
result=callback(*decode(request['arguments']))
print('__HARNESS_HOOK_RESULT__'+json.dumps(result,ensure_ascii=False))
'''


class ProcessHook:
    def __init__(self, callback):
        source = inspect.getsourcefile(callback)
        name = getattr(callback, '__qualname__', '')
        module = getattr(callback, '__module__', '')
        if not source or '<locals>' in name or module == '__main__':
            raise ValueError('Process hooks require an importable module-level function.')
        self.callback = callback
        self.source = Path(source).resolve()
        self.sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.__name__ = getattr(callback, '__name__', name)
        self.execution_mode = 'process'

    def __call__(self, *args):
        from homebrew_harness.configuration import public_environment
        if hashlib.sha256(self.source.read_bytes()).hexdigest() != self.sha256:
            raise ValueError('Registered hook source changed; register it again before execution.')
        request = {'source': str(self.source), 'sha256': self.sha256,
                   'module': self.callback.__module__, 'qualname': self.callback.__qualname__,
                   'paths': [path for path in sys.path if path and Path(path).is_dir()],
                   'arguments': encode_argument(args)}
        payload = json.dumps(request, ensure_ascii=False).encode()
        if len(payload) > current_limits().hook_payload_bytes:
            raise ValueError('Process hook input exceeds its one MiB transport limit.')
        runner = ShellRunner()
        runner.stdin_pipe = True
        def submitted(process):
            process.stdin.write(payload)
            process.stdin.close()
        try:
            result = runner.run([sys.executable, '-I', '-c', WORKER], self.source.parent,
                                public_environment(os.environ), None,
                                on_started=submitted, allow_input=False)
            if result.returncode != 0:
                raise RuntimeError('Process hook failed: ' + result.stderr[-2000:])
            records = [line for line in result.stdout.splitlines() if line.startswith('__HARNESS_HOOK_RESULT__')]
            if len(records) != 1:
                raise RuntimeError('Process hook did not return one result.')
            return json.loads(records[0][len('__HARNESS_HOOK_RESULT__'):])
        finally:
            runner.close()
