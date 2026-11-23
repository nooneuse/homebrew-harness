"""Host-owned execution influence; provider tool hints never set this contract."""

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
        "service_work_sources",
        "service_read_work_record",
        "service_read_source",
        "service_compare_sources",
        "service_observe_target",
    }
)


def is_read_only(name, binding=None, arguments=None):
    if binding is None and name in {
        "service_manage_execution",
        "service_integration_control",
    }:
        operation = (arguments or {}).get("operation")
        return operation in (
            {"list", "inspect", "read", "wait"}
            if name == "service_manage_execution"
            else {"inspect"}
        )
    return (
        binding.policy.read_only is True
        if binding is not None
        else name in READ_ONLY_TOOLS
    )
