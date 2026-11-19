"""Pure execution options and tool names; importing them constructs no runtime."""

import math

<<<<<<< 44b0af9717f2a641ffc1e4acec8aa82686f1c94c
CONTROL_TOOLS = frozenset(
    {
        "list_executions",
        "get_execution",
        "read_execution_output",
        "wait_execution",
        "stop_execution",
        "send_execution_input",
    }
)
SLOW_TOOLS = frozenset(
    {
        "bash",
        "run_tests",
        "task",
        "workflow",
        "connect_mcp",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "glob",
        "search_text",
        "git_status",
        "git_diff",
        "create_worktree",
        "service_read_code",
        "service_read",
        "load_skill",
        "publish_artifacts",
        "integrate_stage",
        "publish_stage",
        "request_shutdown",
        "submit_work_progress",
    }
)
=======
CONTROL_TOOLS = frozenset({'list_executions', 'get_execution', 'read_execution_output', 'wait_execution', 'stop_execution', 'send_execution_input'})
SLOW_TOOLS = frozenset({'bash', 'run_tests', 'task', 'workflow', 'connect_mcp', 'read_file', 'write_file', 'edit_file', 'delete_file', 'glob', 'search_text', 'git_status', 'git_diff', 'create_worktree', 'service_read_code', 'service_read', 'load_skill', 'publish_artifacts', 'integrate_stage', 'publish_stage', 'request_shutdown'})
>>>>>>> d1dd8171a8bb0c53197d34e3d89c7c23bf488f2a


def options(value=None):
    value = {} if value is None else value
<<<<<<< 44b0af9717f2a641ffc1e4acec8aa82686f1c94c
    if not isinstance(value, dict) or set(value) - {
        "mode",
        "wait_seconds",
        "timeout_seconds",
    }:
        raise ValueError(
            "Execution options accept mode, wait_seconds and timeout_seconds."
        )
    mode = value.get("mode", "interactive")
    if mode not in {"interactive", "one_shot"}:
        raise ValueError("Execution mode must be interactive or one_shot.")
    if mode == "one_shot" and "wait_seconds" in value:
        raise ValueError("one_shot uses timeout_seconds, not an observation wait.")
    wait = value.get("wait_seconds", 10)
    timeout = value.get("timeout_seconds")
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 60:
        raise ValueError("Observation wait must be between 0 and 60 seconds.")
    if timeout is not None and (
        type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("Execution timeout must be a positive finite number or null.")
    return {"mode": mode, "wait_seconds": wait, "timeout_seconds": timeout}
=======
    if not isinstance(value, dict) or set(value) - {'mode', 'wait_seconds', 'timeout_seconds'}:
        raise ValueError('Execution options accept mode, wait_seconds and timeout_seconds.')
    mode = value.get('mode', 'interactive')
    if mode not in {'interactive', 'one_shot'}:
        raise ValueError('Execution mode must be interactive or one_shot.')
    if mode == 'one_shot' and 'wait_seconds' in value:
        raise ValueError('one_shot uses timeout_seconds, not an observation wait.')
    wait = value.get('wait_seconds', 10)
    timeout = value.get('timeout_seconds')
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 60:
        raise ValueError('Observation wait must be between 0 and 60 seconds.')
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Execution timeout must be a positive finite number or null.')
    return {'mode': mode, 'wait_seconds': wait, 'timeout_seconds': timeout}
>>>>>>> d1dd8171a8bb0c53197d34e3d89c7c23bf488f2a
