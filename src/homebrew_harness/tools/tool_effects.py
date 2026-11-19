"""Host-owned execution influence; provider tool hints never set this contract."""

<<<<<<< 44b0af9717f2a641ffc1e4acec8aa82686f1c94c
READ_ONLY_TOOLS = frozenset(
    {
        "list_executions",
        "get_execution",
        "read_execution_output",
        "wait_execution",
        "read_file",
        "glob",
        "search_text",
        "git_status",
        "git_diff",
        "load_skill",
        "list_tests",
        "get_test_result",
        "list_tasks",
        "get_task",
        "list_teammates",
        "check_background",
        "read_run_event",
        "get_handoff",
        "list_artifacts",
        "get_artifact",
        "read_subagent_result",
        "service_read",
        "service_read_code",
        "get_work_progress",
    }
)


def is_read_only(name, binding=None):
    return (
        binding.policy.read_only is True
        if binding is not None
        else name in READ_ONLY_TOOLS
    )
=======
READ_ONLY_TOOLS = frozenset({
    "list_executions", "get_execution", "read_execution_output", "wait_execution",
    "read_file", "glob", "search_text", "git_status", "git_diff", "load_skill",
    "list_tests", "get_test_result", "list_tasks", "get_task", "list_teammates",
    "check_background", "read_run_event", "get_handoff", "list_artifacts", "get_artifact",
    "read_subagent_result", "service_read", "service_read_code",
})


def is_read_only(name, binding=None):
    return binding.policy.read_only is True if binding is not None else name in READ_ONLY_TOOLS
>>>>>>> d1dd8171a8bb0c53197d34e3d89c7c23bf488f2a
