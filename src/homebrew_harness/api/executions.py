from homebrew_harness.runtime.limits import current_limits
"""Scoped host views over durable tool executions, including remote Run workers."""

import hashlib
import uuid

from homebrew_harness.contracts import ExecutionRef, ExecutionSnapshot, ExecutionResult
from homebrew_harness.observation import Handle


class ExecutionHandle(Handle):
    def __init__(self, views, identity):
        super().__init__(views.core, ExecutionRef('operation', identity, views.owner))
        self.views = views

    def _row(self):
        row = self.views.store.get('executions', self.id)
        if not row or row.get('checkpoint') != self.views.owner:
            raise ValueError('Execution is outside this owning context.')
        return row

    def snapshot(self):
        row = self._row()
        details = {k: v for k, v in row.items() if k not in {'output', 'result', 'controls'}}
        details['inputs'] = {key[6:]: dict(value) for key, value in row.get('controls', {}).items() if key.startswith('input:')}
        details['capabilities'] = {'can_wait': True, 'can_read_output': True, 'can_request_stop': self.views.control and row['execution_pending'], 'can_send_input': self.views.control and row.get('accepts_input', False) and row['execution_pending'] and row['phase'] != 'cleanup'}
        return ExecutionSnapshot(self.ref, row['state'], row['revision'],
                                 persistence_confirmed=True, execution_pending=row['execution_pending'],
                                 output_available=row.get('result') is not None, cleanup=row['cleanup'], details=details)

    def result(self):
        self._require_result()
        row = self._row()
        return ExecutionResult(self.ref, row['state'], value=row['result'])

    def output(self, cursor=0, limit=16000, *, stream="output"):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= current_limits().operation_read_chars:
            raise ValueError('Invalid output cursor or limit.')
        row = self._row()
        if stream == 'result':
            import json
            value = json.dumps(row['result'], ensure_ascii=False) if row['result'] is not None else ''
            return {'content': value[cursor:cursor+limit], 'next_cursor': min(cursor+limit, len(value)), 'total_characters': len(value)}
        if stream != 'output':
            raise ValueError('Output stream must be output or result.')
        start = max(cursor, row['output_base'])
        content = row['output'][start-row['output_base']:start-row['output_base']+limit]
        return {'content': content, 'next_cursor': start+len(content), 'omitted_before': row['output_base']}

    def stop(self, *, revision=None, request_id=None):
        if not self.views.control:
            raise ValueError("This execution view is read-only.")
        row = self._row()
        if revision is not None and row['revision'] != revision:
            raise ValueError('Execution revision changed.')
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('Invalid stop request ID.')
        key = hashlib.sha256((self.id + ':' + request_id).encode()).hexdigest()
        db = self.views.store.backend()
        with db.write() as conn:
            existing = db._get(conn, 'execution_controls', key)
            if existing:
                return existing
            current = db._get(conn, 'executions', self.id)
            if revision is not None and current['revision'] != revision:
                raise ValueError('Execution revision changed.')
            if current['checkpoint'] != self.views.owner or current['instance'] != row['instance']:
                raise ValueError('Execution owner changed.')
            command = {'id': key, 'execution_id': self.id, 'instance': row['instance'],
                       'checkpoint': self.views.owner, 'action': 'stop', 'state': 'accepted'}
            db._put(conn, 'execution_controls', key, command)
        return command

    def input(self, text, *, request_id=None):
        if not self.views.control:
            raise ValueError('This execution view is read-only.')
        if not isinstance(text, str) or len(text) > current_limits().operation_input_chars:
            raise ValueError('Execution input must be bounded text.')
        row = self._row()
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('Invalid input request ID.')
        key = hashlib.sha256((self.id + ':input:' + request_id).encode()).hexdigest()
        digest = hashlib.sha256(text.encode()).hexdigest()
        db = self.views.store.backend()
        with db.write() as conn:
            existing = db._get(conn, 'execution_controls', key)
            if existing:
                if existing.get('digest') != digest:
                    raise ValueError('Input request_id has different text.')
                return existing
            current = db._get(conn, 'executions', self.id)
            if current['instance'] != row['instance'] or current['checkpoint'] != self.views.owner or not current['execution_pending']:
                raise ValueError('Execution owner or state changed.')
            command = {'id': key, 'execution_id': self.id, 'instance': row['instance'],
                       'checkpoint': self.views.owner, 'action': 'input', 'text': text,
                       'digest': digest, 'state': 'accepted'}
            db._put(conn, 'execution_controls', key, command)
        return {key: value for key, value in command.items() if key != 'text'}


class Executions:
    _api_service = True
    def __init__(self, core, store, owner, *, control=True):
        self.core, self.store, self.owner = core, store, owner
        self.control = control

    def list(self):
        if self.owner is None:
            return []
        return [ExecutionHandle(self, key).snapshot() for key in self.store.items('executions', owner=self.owner)]

    def get(self, identity):
        if self.owner is None:
            raise ValueError("The execution owner has not initialized yet.")
        handle = ExecutionHandle(self, identity)
        handle._row()
        return handle
