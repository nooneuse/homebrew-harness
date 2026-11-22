"""Owner-requested progress-budget changes on a fully quiescent Run."""
import json
import time
from .spec import RunSpec
from .requests import digest
from homebrew_harness.storage.storage_gate import storage_gate

FIELDS = frozenset({'total_seconds', 'retries', 'repeat_limit', 'replan_limit', 'episode_seconds',
                    'episode_model_calls', 'approval_seconds', 'approval_review', 'runtime_limits'})


def configure(store, identity, changes, revision, request_id):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS or not isinstance(request_id, str) or not request_id:
        raise ValueError('Budget configuration requires known fields and a stable request ID.')
    db = store.runtime.backend()
    key = identity + ':budgets:' + request_id
    with storage_gate(store.root), db.write() as conn:
        old = db._get(conn, 'run_controls', key)
        if old:
            if old['payload'] != digest(changes):
                raise ValueError('Budget request ID has another payload.')
            return old['result']
        run = db._get(conn, 'runs', identity)
        if (not run or run['revision'] != revision or run['status'] not in {'paused','blocked'}
                or not run.get('cleanup_complete') or run.get('finalized_at')):
            raise ValueError('Budget changes require the current paused/blocked Run after complete cleanup.')
        before = run['spec']
        spec = json.loads(json.dumps(RunSpec.from_dict({**before, **changes}).data()))
        history = [*run.get('budget_history', []), {'at': time.time(), 'request_id': request_id,
            'previous': {k: before.get(k) for k in changes}, 'configured': changes,
            'previous_spec': digest(before), 'next_spec': digest(spec), 'previous_deadline_at': run['deadline_at']}]
        run.update(spec=spec, budget_history=history, revision=run['revision']+1)
        # Changing a configured budget does not itself issue a new execution window,
        # renew a grant, reset counters, or alter its recorded deadline.
        db._put(conn, 'runs', identity, run)
        result = {'id':identity, 'revision':run['revision'], 'configured_fields':sorted(changes),
                  'execution_resumed':False, 'deadline_unchanged':True, 'spec_digest':digest(spec)}
        db._put(conn, 'run_controls', key, {'payload':digest(changes), 'result':result})
        return result
