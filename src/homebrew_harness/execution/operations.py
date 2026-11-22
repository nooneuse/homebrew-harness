from homebrew_harness.runtime.limits import current_limits
"""Owned tool attempts with bounded observation, cancellation and durable results.

Waiting never owns process lifetime. Native tools retain their permission and
result contracts; this module owns only an attempt and its observation channel.
"""

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path

from homebrew_harness.contracts import ExecutionRef, ExecutionSnapshot
from homebrew_harness.execution.execution_control import check, cleanup_scope, control_scope
from homebrew_harness.execution.execution_leases import ExecutionLeases, lease_scope
from homebrew_harness.execution.model_gateway import Deadline, current_scope
from homebrew_harness.observation import Handle
from homebrew_harness.runtime.runtime_control import Workers, WorkAccepted
from homebrew_harness.security.data_boundary import redact
from homebrew_harness.storage.durable_runtime import DurableStore, checkpoint_scope

CURRENT = ContextVar('owned_execution', default=None)
PARENT_CHECKPOINT = ContextVar('execution_parent_checkpoint', default=None)
ACCEPTING_THREAD = ContextVar('execution_accepting_thread', default=None)
from homebrew_harness.runtime.execution_options import CONTROL_TOOLS, SLOW_TOOLS, options


def emit_output(stream, chunk):
    current = CURRENT.get()
    if current is not None:
        current[0]._output(current[1], stream, chunk)


def bind_agent_input():
    current = CURRENT.get()
    if current is None:
        return
    manager, identity = current
    def accept(text):
        with manager.lock:
            manager.rows[identity].setdefault('agent_inputs', []).append(text)
            manager._save(manager.rows[identity])
        return False
    manager._inputs[identity] = accept
    with manager.lock:
        manager.rows[identity]["accepts_input"] = True
        manager._save(manager.rows[identity])


def apply_agent_inputs(messages, save):
    current = CURRENT.get()
    if current is None:
        return
    manager, identity = current
    with manager.lock:
        row = manager.rows[identity]
        accepted = list(row.get('agent_inputs', []))
        if not accepted:
            return
        for text in accepted:
            messages.append({'role': 'user', 'name': 'parent_steering', 'content': text})
        save()
        row['agent_inputs'] = []
        for command in row['controls'].values():
            if command.get('digest') in {hashlib.sha256(text.encode()).hexdigest() for text in accepted}:
                command['state'] = 'applied'
        manager._save(row)


def agent_input_pending():
    current = CURRENT.get()
    if current is None:
        return False
    manager, identity = current
    with manager.lock:
        return bool(manager.rows[identity].get('agent_inputs'))


def bind_input(callback, *, workdir=None):
    current = CURRENT.get()
    if current is not None:
        manager, identity = current
        directory = Path(workdir).resolve() if workdir is not None else manager.root
        relative = directory.relative_to(manager.root).as_posix()
        with manager.lock:
            manager._inputs[identity] = callback
            manager.rows[identity]["accepts_input"] = True
            manager.rows[identity]["input_workdir"] = relative
            manager._save(manager.rows[identity])


class OperationHandle(Handle):
    def __init__(self, manager, identity):
        super().__init__(manager, ExecutionRef('operation', identity, manager.owner))
        self.manager = manager

    def snapshot(self):
        row = self.manager.get(self.id)
        state = row['state']
        return ExecutionSnapshot(self.ref, state, row['revision'],
                                 persistence_confirmed=row.get("persistence_confirmed", True),
                                 execution_pending=row['execution_pending'],
                                 output_available=row.get('result') is not None,
                                 cleanup=row['cleanup'], details=row)

    def result(self):
        self._require_result()
        return self.manager.get(self.id)['result']

    def output(self, cursor=0, limit=16000, *, stream='output'):
        return self.manager.output(self.id, cursor, limit, stream=stream)

    def input(self, text, *, request_id=None):
        return self.manager.input(self.id,text,request_id or uuid.uuid4().hex,trusted_host=True)

    def stop(self, revision=None, request_id=None):
        return self.manager.stop(self.id, revision, request_id)


class Operations:
    def __init__(self, workdir, owner, *, leases=None, parent=None, max_running=None):
        self.root, self.owner = Path(workdir).resolve(), owner
        self.store = DurableStore(self.root)
        self._owns_leases = leases is None
        self.leases = leases or ExecutionLeases(self.root)
        self.parent, self.max_running = parent, current_limits().execution_slots if max_running is None else max_running
        self.changed = threading.Event()
        self.lock = threading.RLock()
        self.workers = Workers()
        self.rows, self.cancel, self.done, self._inputs = {}, {}, {}, {}
        self._input_workers = Workers()
        self.closed = False
        self._monitoring = False
        self._monitor_lock = threading.Lock()
        self.control_workers = Workers()
        self.instance = uuid.uuid4().hex
        self.children = []
        self.cleanup_errors = []
        self._terminal_broker = None
        self._terminal_ready = set()
        if parent is not None:
            parent.children.append(self)

    def _monitor(self):
        try:
            while self.pending() and not self.closed:
                with cleanup_scope():
                    commands = self.store.items('execution_controls', owner=self.owner)
                for key, command in commands.items():
                    if command['state'] != 'accepted':
                        continue
                    row = self.rows.get(command['execution_id'])
                    if row is None or row['instance'] != command['instance']:
                        continue
                    try:
                        if command['action'] == 'stop':
                            self.stop(row['id'], request_id=command['id'])
                        else:
                            self.input(row['id'], command['text'], command['id'], trusted_host=True)
                        outcome = {'state': 'applied' if command['action'] == 'stop' else 'delivered'}
                    except ValueError as error:
                        outcome = {'state': 'rejected', 'error': str(error)}
                    with cleanup_scope():
                        self.store.update_fields('execution_controls', key, outcome)
                self.changed.wait(.1)
                self.changed.clear()
        except Exception as error:
            with self.lock:
                self.cleanup_errors.append({'component': 'execution_controls', 'error_type': type(error).__name__})
                for identity, event in self.cancel.items():
                    if self.rows[identity]['execution_pending']:
                        event.set()
        finally:
            with self._monitor_lock:
                self._monitoring = False

    def _ensure_monitor(self):
        with self._monitor_lock:
            if not self._monitoring:
                self._monitoring = True
                try:
                    self.control_workers.start(self._monitor, track_operation=False, name='execution-controls')
                except WorkAccepted:
                    pass
                except BaseException:
                    self._monitoring = False
                    for identity, event in self.cancel.items():
                        if self.rows[identity]['execution_pending']:
                            event.set()
                    raise

    def _save(self, row):
        from homebrew_harness.storage.persistence import persist
        with self.lock:
            row['revision'] += 1
            row['persistence_confirmed'] = True
            saved = copy.deepcopy(row)
            try:
                persist("execution_state", lambda: self.store.put('executions', row['id'], saved), context_id=self.owner)
            except BaseException:
                row['persistence_confirmed'] = False
                raise
        self.changed.set()

    def recover(self):
        for identity, row in self.store.items('executions', owner=self.owner).items():
            if identity in self.rows:
                continue
            if row['execution_pending']:
                row.update(state='interrupted', effect_state='unknown',
                           execution_pending=False, result={'error': 'Previous execution owner ended. Inspect effects before starting a new attempt.'},
                           cleanup={'complete': False, 'recovery_required': True})
                self._save(row)
            self.rows[identity] = row

    def handle(self, identity):
        self.get(identity)
        return OperationHandle(self, identity)

    def _resolve(self, identity):
        if identity in self.rows:
            return self
        for child in self.children:
            try:
                return child._resolve(identity)
            except ValueError:
                pass
        raise ValueError('Execution is not owned by this context or an authorized child.')

    def get(self, identity):
        owner = self._resolve(identity)
        with owner.lock:
            row = copy.deepcopy(owner.rows[identity])
            row['elapsed_seconds'] = max(0, row.get('finished_at', time.time()) - row['created_at'])
            row.pop('_last_flush', None)
            row['capabilities'] = {
                'can_wait': True, 'can_read_output': True,
                'can_confirm_local_stop': identity in owner.cancel,
                'can_request_stop': row['execution_pending'] and identity in owner.cancel,
                'can_send_input': identity in owner._inputs and row['execution_pending'] and row['phase'] != 'cleanup',
                'can_pause': False, 'can_resume_checkpoint': False,
                'can_restart': not row['execution_pending'] and row.get('effect_state') != 'unknown',
            }
            return row

    def snapshots(self):
        with self.lock:
            identities = list(self.rows)
        return [self.get(identity) for identity in identities]

    def ready(self):
        with self.lock:
            return any(not row["execution_pending"] and not row["delivered"] for row in self.rows.values())

    def pending(self):
        with self.lock:
            pending = any(row['execution_pending'] for row in self.rows.values())
            children = list(self.children)
        return pending or any(child.pending() for child in children)

    def _output(self, identity, stream, chunk):
        text = chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else str(chunk)
        text = redact(text)
        with self.lock:
            row = self.rows[identity]
            # The tool's native full-output store remains authoritative. This is
            # a bounded incremental observation buffer, with an explicit base.
            output = row['output'] + text
            excess = max(0, len(output) - current_limits().execution_output_chars)
            row['output'] = output[excess:]
            row['output_base'] += excess
            row['last_output_at'] = time.time()
            row['last_stream'] = stream
            if time.monotonic() - row.get('_last_flush', 0) > 0.25:
                row['_last_flush'] = time.monotonic()
                self._save(row)

    def _approval_context(self):
        from homebrew_harness.runtime.input_router import current_broker, ApprovalBroker, approval_scope
        from homebrew_harness.security.permissions import interactive_available
        if current_broker() is not None or not interactive_available():
            return nullcontext()
        from homebrew_harness.security.review_context import owner
        import re
        if self._terminal_broker is None:
            def announce(text):
                match = re.search(r'"id": "(approval_[0-9a-f]+)"', text)
                if match:
                    self._terminal_ready.add(match[1])
            self._terminal_broker = ApprovalBroker(trusted=True, announce=announce, owner=owner())
        return approval_scope(self._terminal_broker)

    def service_approvals(self):
        broker = self._terminal_broker
        if broker is None or threading.current_thread() is not threading.main_thread():
            return
        from homebrew_harness.runtime.input_router import approval_scope
        from homebrew_harness.security.permissions import ask_user
        for request in broker.records():
            if request['id'] not in self._terminal_ready:
                continue
            with approval_scope(None):
                decision = ask_user(request['tool'], request['arguments'], Path(request['directory']), 'A pending execution requires exact terminal approval.')
            try:
                broker.respond(request['id'], decision.action == 'allow')
            except ValueError:
                pass  # The execution may have been cancelled while input was read.
            self._terminal_ready.discard(request['id'])

    def wait_for(self, identity, seconds=None):
        owner = self._resolve(identity)
        end = None if seconds is None else time.monotonic() + seconds
        while self.get(identity)['execution_pending']:
            self.service_approvals()
            try:
                check()
            except Exception as error:
                if getattr(error, "source", None) != "episode":
                    owner.stop(identity)
                    break
            row = self.get(identity)
            if row['phase'] == 'cleanup' and row.get('effect_state') == 'unknown':
                break
            if end is not None and time.monotonic() >= end:
                break
            self.changed.wait(0.05 if end is None else min(0.05, max(0, end-time.monotonic())))
            self.changed.clear()
        return self.get(identity)

    def output(self, identity, cursor=0, limit=16000, stream="output"):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= current_limits().operation_read_chars:
            raise ValueError('Output cursor must be nonnegative; limit must be 1-16000 characters.')
        row = self.get(identity)
        if stream == 'result':
            value = json.dumps(row['result'], ensure_ascii=False) if row['result'] is not None else ''
            return {'id': identity, 'state': row['state'], 'content': value[cursor:cursor+limit], 'next_cursor': min(cursor+limit, len(value)), 'total_characters': len(value), 'execution_pending': row['execution_pending']}
        if stream != 'output':
            raise ValueError('Output stream must be output or result.')
        start = max(cursor, row['output_base'])
        text = row['output'][start-row['output_base']:start-row['output_base']+limit]
        return {'id': identity, 'state': row['state'], 'content': text, 'next_cursor': start+len(text), 'omitted_before': row['output_base'], 'execution_pending': row['execution_pending']}

    def start(self, name, arguments, execute, *, execution=None, request_id=None, deadline=None, read_only=False, checkpoint=None, settled=None, resume_interrupted=False):
        config = options(execution)
        signature = hashlib.sha256(json.dumps([name, arguments, config], sort_keys=True).encode()).hexdigest()
        identity = 'exec_' + hashlib.sha256((self.owner + ':' + (request_id or uuid.uuid4().hex)).encode()).hexdigest()[:32]
        recovered=[]
        with self.lock:
            previous = self.store.get('executions', identity)
            if identity not in self.rows and previous is not None:
                self.rows[identity] = previous
            if identity in self.rows:
                if self.rows[identity]['signature'] != signature:
                    raise ValueError('Execution request_id already has different arguments.')
                if resume_interrupted and self.rows[identity]['state']=='interrupted':
                    previous=self.rows.pop(identity)
                    recovered=[*previous.get('recovery_history',[]),{k:previous.get(k) for k in ('instance','state','created_at','finished_at','result','cleanup')}]
                else:
                    return self.handle(identity)
            if self.closed:
                raise ValueError('Execution owner is closed.')
            if sum(row['execution_pending'] for row in self.rows.values()) >= self.max_running:
                raise ValueError('Concurrent execution limit reached; observe existing executions.')
            parent_deadline = deadline or (current_scope().gateway.operation_deadline() if current_scope().gateway else current_scope().deadline)
            row = {'id': identity, 'owner': self.owner, 'checkpoint': checkpoint or self.owner,
                   'instance': self.instance, 'name': name, 'signature': signature, 'mode': config['mode'],
                   'revision': 0, 'state': 'queued', 'phase': 'accepted', 'created_at': time.time(),
                   'execution_pending': True, 'cleanup': {'complete': False}, 'effect_state': 'not_started',
                   'read_only': read_only, 'output': '', 'output_base': 0, 'last_output_at': None,
                   'accepts_input': False, 'result': None, 'delivered': False, 'controls': {}, 'timeout_seconds': config['timeout_seconds']}
            if recovered:row['recovery_history']=recovered
            self.rows[identity] = row
            cancel, done = threading.Event(), threading.Event()
            self.cancel[identity], self.done[identity] = cancel, done
            try:
                self._save(row)
            except BaseException:
                row.update(state='failed', phase='settled', execution_pending=False,
                           cleanup={'complete': True}, result={'error': 'Execution registration failed before launch.', 'executed': False})
                self.done[identity].set()
                raise
        from homebrew_harness.storage.durable_runtime import active_writer
        writer = active_writer()
        parent_checkpoint = {"store": writer.store, "key": writer.key, "session_id": writer.snapshot().get("session_id")} if writer is not None else None
        captured_scope = current_scope()
        from homebrew_harness.storage.durable_runtime import current_operation, operation_active
        resource_key = current_operation()
        accepting_thread = threading.current_thread()

        def run():
            token = CURRENT.set((self, identity))
            parent_token = PARENT_CHECKPOINT.set(parent_checkpoint)
            accepting_token = ACCEPTING_THREAD.set(accepting_thread)
            effective = parent_deadline.intersect(Deadline.after(config['timeout_seconds'], source='tool'))
            gateway_scope = captured_scope.gateway.scope(deadline=effective, inherit_deadline=False, cancel_event=cancel) if captured_scope.gateway else nullcontext()
            resource = 'execution:' + identity
            leased = False
            try:
                if self.leases is not None:
                    self.leases.acquire(resource)
                    leased = True
                with checkpoint_scope(None), lease_scope(self.leases), control_scope(effective, cancel_events=(cancel,)), gateway_scope:
                    check(effective, (cancel,))
                    with self.lock:
                        row.update(state='running', phase='execute', effect_state='started',
                                   deadline_at=time.time()+effective.remaining() if effective.remaining() is not None else None,
                                   deadline_source=effective.source)
                        self._save(row)
                    result = execute()
                try:
                    value = json.loads(result) if isinstance(result, str) else result
                except ValueError:
                    value = result
                state = 'cancelled' if cancel.is_set() else 'failed' if isinstance(value, dict) and (value.get('error') or value.get('status') in {'failed', 'timeout'}) else 'completed'
                unknown = isinstance(value, dict) and value.get('outcome') in {'unknown', 'partial'}
                if unknown:
                    with self.lock:
                        row.update(result=redact(value), phase='cleanup', effect_state='unknown')
                        self._save(row)
            except BaseException as error:
                from homebrew_harness.runtime.errors import error_info, error_message
                value = {'error': redact(error_message(error)), 'error_info': redact(error_info(error))}
                state = 'cancelled' if cancel.is_set() else 'failed'
                unknown = row['effect_state'] == 'started' and not read_only
            finally:
                if settled is not None:
                    while not settled():
                        with self.lock:
                            row['phase'] = 'cleanup'
                        threading.Event().wait(0.05)
                if resource_key is not None and name != "connect_mcp":
                    with self.lock:
                        row['phase'] = 'cleanup'
                    while operation_active(resource_key[0], resource_key[1], excluding=(accepting_thread, threading.current_thread())):
                        threading.Event().wait(0.05)
                CURRENT.reset(token)
                PARENT_CHECKPOINT.reset(parent_token)
                ACCEPTING_THREAD.reset(accepting_token)
                if leased:
                    self.leases.release(resource)
            with self.lock:
                row.update(state=state, phase='settled', result=redact(value), execution_pending=False,
                           cleanup={'complete': True}, effect_state='unknown' if unknown else 'observed', finished_at=time.time())
                self._inputs.pop(identity, None)
                row['accepts_input'] = False
                try:
                    self._save(row)
                except Exception as error:
                    row['cleanup'] = {'complete': False, 'persistence_error': type(error).__name__}
                    self.cleanup_errors.append({'component': 'execution_result', 'error_type': type(error).__name__})
                done.set()
                self.changed.set()
        try:
            with self._approval_context():
                self.workers.start(run, name='execution-' + identity)
        except WorkAccepted:
            pass
        except Exception as error:
            if not done.is_set() and not any(t.name == 'execution-' + identity and t.ident is not None for t in self.workers.threads):
                with self.lock:
                    row.update(state='failed', execution_pending=False, effect_state='not_started', cleanup={'complete': True}, result={'error': type(error).__name__ + ': execution was not launched.', 'executed': False})
                    done.set()
                    self._save(row)
            raise
        self._ensure_monitor()
        return self.handle(identity)

    def stop(self, identity, revision=None, request_id=None):
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 200):
            raise ValueError('Stop needs a request_id of 1-200 characters.')
        owner = self._resolve(identity)
        with owner.lock:
            row = owner.rows[identity]
            key = request_id or uuid.uuid4().hex
            if key in row['controls']:
                return row['controls'][key]
            if revision is not None and revision != row['revision']:
                raise ValueError('Execution revision changed; refresh before stopping.')
            if row['execution_pending']:
                row['state'] = 'stopping'
                if identity not in owner.cancel:
                    raise ValueError('Execution has another owner instance; use its durable host controls.')
                owner.cancel[identity].set()
            response = {'id': identity, 'state': 'stop_requested' if row['execution_pending'] else row['state']}
            row['controls'][key] = response
            owner._save(row)
            return response

    def input(self, identity, value, request_id, *, trusted_host=False):
        owner = self._resolve(identity)
        if not isinstance(value, str) or len(value) > current_limits().operation_input_chars or not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('Input needs bounded text and a request_id.')
        key = 'input:' + request_id
        digest = hashlib.sha256(value.encode()).hexdigest()
        with owner.lock:
            row = owner.rows[identity]
            old = row['controls'].get(key)
            if old:
                if old['digest'] != digest:
                    raise ValueError('Input request_id has different text.')
                return copy.deepcopy(old)
            callback = owner._inputs.get(identity)
            if callback is None or not row['execution_pending']:
                raise ValueError('Execution does not currently accept input. Stop and start with revised arguments instead.')
            instance = row['instance']
        if owner.cancel.get(identity) is not None and owner.cancel[identity].is_set():
            raise ValueError('Execution is stopping and no longer accepts input.')
        if row.get('deadline_at') is not None and time.time() >= row['deadline_at']:
            raise ValueError('Execution deadline has expired.')
        if row['name'] == 'bash' and not trusted_host:
            from homebrew_harness.security.permissions import check_permission
            decision = check_permission('bash', {'command': value}, owner.root / row.get('input_workdir', '.'))
            if decision.action != 'allow':
                raise ValueError('Execution input requires permission: ' + decision.reason)
        with owner.lock:
            if row['instance'] != instance or not row['execution_pending'] or owner._inputs.get(identity) is not callback:
                raise ValueError('Execution input target changed during permission review.')
            if key in row['controls']:
                old = row['controls'][key]
                if old['digest'] != digest:
                    raise ValueError('Input request_id has different text.')
                return copy.deepcopy(old)
            row['controls'][key] = {'id': identity, 'state': 'accepted', 'digest': digest}
            owner._save(row)
        def deliver():
            try:
                applied = callback(value)
                state = 'applied' if applied is not False else 'accepted'
                error = None
            except Exception as failure:
                state, error = 'rejected', redact(str(failure))
            with owner.lock:
                # Steering callbacks mark application themselves at a safe point.
                if row['controls'][key]['state'] != 'applied':
                    row['controls'][key].update(state=state)
                if error:
                    row['controls'][key]['error'] = error
                owner._save(row)
        owner._input_workers.start(deliver, name='execution-input', track_operation=False)
        return copy.deepcopy(row['controls'][key])

    def run_tool(self, tool, *, workdir, **arguments):
        if tool == 'list_executions':
            result = {'executions': [{k: r[k] for k in ('id', 'name', 'state', 'execution_pending', 'capabilities')} for r in self.snapshots()[-20:]]}
        else:
            identity = arguments['execution_id']
            if tool == 'get_execution':
                result = self.get(identity)
                result = self._model_view(result)
            elif tool == 'read_execution_output':
                result = self.output(identity, arguments.get('cursor', 0), arguments.get('limit', 16000), arguments.get('stream', 'output'))
            elif tool == 'wait_execution':
                seconds = arguments.get('wait_seconds', 10)
                options({'wait_seconds': seconds})
                result = self.wait_for(identity, seconds)
                result = self._model_view(result)
            elif tool == 'stop_execution':
                result = self.stop(identity, arguments.get('revision'), arguments.get('request_id'))
            elif tool == 'send_execution_input':
                result = self.input(identity, arguments['input'], arguments['request_id'])
            else:
                raise ValueError('Unknown execution control.')
        return json.dumps(result, ensure_ascii=False)

    def _model_view(self, row):
        row.pop('output', None)
        row['inputs'] = {key[6:]: dict(value) for key, value in row.pop('controls', {}).items() if key.startswith('input:')}
        value = json.dumps(row.get('result'), ensure_ascii=False)
        if len(value) > current_limits().operation_input_chars:
            row['result'] = {'truncated': True, 'total_characters': len(value),
                             'preview': value[:12000], 'read_with': 'read_execution_output(stream=result)'}
        return row

    def close(self, timeout=0.5):
        self.closed = True
        if self._terminal_broker is not None:
            self._terminal_broker.close()
        self.changed.set()
        for identity, cancel in list(self.cancel.items()):
            if self.rows[identity]['execution_pending']:
                cancel.set()
        for child in self.children:
            child.close(0)
        self.wait_closed(timeout)

    def wait_closed(self, timeout=5):
        end = None if timeout is None else time.monotonic() + max(0, timeout)
        remaining = lambda: None if end is None else max(0, end-time.monotonic())
        complete = self.workers.wait(remaining()) and self.control_workers.wait(remaining()) and self._input_workers.wait(remaining()) and all(child.wait_closed(remaining()) for child in self.children)
        if complete and self._owns_leases and self.closed:
            self.leases.close()
        return complete
