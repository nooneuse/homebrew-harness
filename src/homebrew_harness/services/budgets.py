"""Stopped-service owner configuration; permissions and private references stay fixed."""
import copy
from pathlib import Path
from .spec import ServiceSpec
from .runtime import saved_config
from .protocol import fingerprint
from homebrew_harness.runs.budgets import FIELDS as RUN_FIELDS

FIELDS = {
    'run_defaults': RUN_FIELDS,
    'maintenance': {'authority_seconds','job_seconds','job_turns','job_attempts','job_warning_seconds','job_call_warning','job_attempt_warning'},
    'delivery': {'gate_seconds','authorization_seconds','ci_wait_seconds','ci_wait_warning_seconds','ci_fix_limit','ci_fix_warning','auto_ci_fix'},
    'communication': {'authorization_seconds','prepare_seconds','request_seconds'},
    'config': {'total_seconds','turn_seconds','max_turns','model_call_warning','turn_warning_seconds','approval_review','runtime_limits'},
}


def configure(store, identity, changes, revision, request_id, *, work_id=None, run_changes=None):
    if not isinstance(changes,dict) or not changes or set(changes)-set(FIELDS):
        raise ValueError('Unknown Service budget group.')
    for group, values in changes.items():
        if not isinstance(values,dict) or set(values)-set(FIELDS[group]):
            raise ValueError('Only progress budgets and explicit approval delegation can be reconfigured.')
    meta = store.get(identity)
    spec, config = copy.deepcopy(meta['spec']), copy.deepcopy(meta['config'])
    for group, values in changes.items():
        if group == 'config': config.update(values)
        else: spec[group] = {**spec.get(group, {}), **values}
    ServiceSpec(**spec)
    saved_config(config)
    key = 'budgets:' + identity + ':' + request_id
    payload = fingerprint([changes, work_id, run_changes])
    prior = store.runtime.get('service_commands', key)
    if prior:
        if prior['payload'] != payload: raise ValueError('Budget request ID has another payload.')
        return prior['result']
    if meta['revision']!=revision or meta['state']!='stopped' or not meta['resources_closed'] or meta.get('owner'):
        raise ValueError('Service budget configuration requires its current stopped owner state.')
    work = store.work(work_id,identity) if work_id else None
    if run_changes:
        if not work or not work.get('pause_requested'):
            raise ValueError('The bound Work must remain paused.')
        from homebrew_harness.runs.store import RunStore
        native = RunStore(Path(work.get('run_workspace') or spec['workspace']))
        try:
            run = native.get(work['run_id'])
            native.configure_budgets(run['id'], run_changes, run['revision'], 'service-'+request_id)
        finally: native.close()
    with store.db.write() as conn:
        current=store.db._get(conn,'service_meta',identity)
        if current['revision']!=revision or current['state']!='stopped' or not current['resources_closed'] or current.get('owner'):
            raise ValueError('Service changed during budget preparation.')
        current.update(spec=spec,config=config,revision=current['revision']+1)
        store.db._put(conn,'service_meta',identity,current)
        if work:
            actual=store.db._get(conn,'service_work',work_id)
            if actual['revision']!=work['revision'] or not actual.get('pause_requested'):
                raise ValueError('Work changed during budget preparation.')
            if run_changes:
                actual.update(run_spec={**actual['run_spec'],**run_changes},revision=actual['revision']+1)
                store.db._put(conn,'service_work',work_id,actual)
        result={'id':identity,'revision':current['revision'],'configured_groups':sorted(changes),
                'work_id':work_id,'execution_resumed':False,'binding_refresh_required':True}
        store.db._put(conn,'service_commands',key,{'payload':payload,'result':result,
            'previous_configured':{g:meta['config'] if g=='config' else meta['spec'].get(g,{}) for g in changes},
            'configured':changes,'checkpoint':'service:'+identity})
        return result
