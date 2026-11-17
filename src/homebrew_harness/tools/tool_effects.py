"""Host-owned execution influence; provider tool hints never set this contract."""

READ_ONLY_TOOLS = frozenset({
    "read_file", "glob", "search_text", "git_status", "git_diff", "load_skill",
    "list_tests", "get_test_result", "list_tasks", "get_task", "list_teammates",
    "check_background", "read_run_event", "get_handoff", "list_artifacts", "get_artifact",
    "read_subagent_result", "service_read", "service_read_code",
})


def is_read_only(name, binding=None):
    return binding.policy.read_only is True if binding is not None else name in READ_ONLY_TOOLS
