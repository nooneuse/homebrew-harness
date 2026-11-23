"""Narrow Work tools shared by service reasoning and its development Lead."""

import json

NAMES = frozenset(
    {
        "service_work_sources",
        "service_read_work_record",
        "service_read_source",
        "service_compare_sources",
        "service_observe_target",
        "service_manage_execution",
        "service_integration_control",
    }
)


class ModelWorkTools:
    def __init__(self, service, work_id, *, read_only=False):
        self.service, self.work_id = service, work_id
        self.read_only = read_only

    def sources(self, **unused):
        return json.dumps(
            self.service.query("work_sources", {"id": self.work_id}), ensure_ascii=False
        )

    def record(self, kind="index", identity=None, offset=0, limit=10000, expected_version=None, **unused):
        return json.dumps(self.service.query("work_record_read", {
            "id": self.work_id, "kind": kind, "identity": identity,
            "offset": offset, "limit": limit, "expected_version": expected_version,
        }), ensure_ascii=False)

    def read(
        self, source_ref, path, offset=0, limit=10000, expected_version=None, **unused
    ):
        return json.dumps(
            self.service.query(
                "work_source_read",
                {
                    "id": self.work_id,
                    "source_ref": source_ref,
                    "path": path,
                    "offset": offset,
                    "limit": limit,
                    "expected_version": expected_version,
                },
            ),
            ensure_ascii=False,
        )

    def compare(
        self,
        before,
        after,
        offset=0,
        limit=100,
        path=None,
        expected_version=None,
        **unused
    ):
        return json.dumps(
            self.service.query(
                "work_source_compare",
                {
                    "id": self.work_id,
                    "before": before,
                    "after": after,
                    "offset": offset,
                    "limit": limit,
                    "path": path,
                    "expected_version": expected_version,
                },
            ),
            ensure_ascii=False,
        )

    def target(self, **unused):
        return json.dumps(
            self.service.command(
                "work_observe_target",
                {
                    "id": self.work_id,
                    "execution": {"mode": "interactive", "wait_seconds": 0},
                },
            ),
            ensure_ascii=False,
        )

    def execution(
        self,
        operation,
        execution_id=None,
        cursor=0,
        limit=None,
        stream="output",
        wait_seconds=10,
        input=None,
        request_id=None,
        **unused
    ):
        if self.read_only and operation in {"stop", "input"}:
            raise ValueError(
                "This job only observes Work executions; submit a proposal for an execution change."
            )
        if operation == "list":
            result = self.service.query(
                "work_executions",
                {
                    "id": self.work_id,
                    "cursor": cursor,
                    "limit": 20 if limit is None else limit,
                },
            )
        elif operation == "inspect":
            result = self.service.query(
                "work_execution_get", {"id": self.work_id, "execution_id": execution_id}
            )
        elif operation == "read":
            result = self.service.query(
                "work_execution_output",
                {
                    "id": self.work_id,
                    "execution_id": execution_id,
                    "cursor": cursor,
                    "limit": 16000 if limit is None else limit,
                    "stream": stream,
                },
            )
        elif operation == "wait":
            result = self.service.query(
                "work_execution_wait",
                {
                    "id": self.work_id,
                    "execution_id": execution_id,
                    "seconds": wait_seconds,
                },
            )
        elif operation in {"stop", "input"}:
            current = self.service.query("work_get", {"id": self.work_id})
            payload = {
                "id": self.work_id,
                "revision": current["revision"],
                "execution_id": execution_id,
            }
            if operation == "input":
                payload["text"] = input
            result = self.service.command(
                "work_execution_" + operation, payload, request_id
            )
        else:
            raise ValueError("Choose list, inspect, read, wait, stop or input.")
        return json.dumps(result, ensure_ascii=False)

    def integration(self, operation, **unused):
        if self.read_only and operation != "inspect":
            raise ValueError(
                "This job is a read-only proposal; select integration in its action instead of applying it."
            )
        current = self.service.query("work_get", {"id": self.work_id})
        from homebrew_harness.execution.operations import CURRENT

        owned = CURRENT.get()
        return json.dumps(
            self.service.command(
                "work_integration_control",
                {
                    "id": self.work_id,
                    "revision": current["revision"],
                    "operation": operation,
                    "caller_execution_id": owned[1] if owned else None,
                    "execution": {"mode": "interactive", "wait_seconds": 0},
                },
            ),
            ensure_ascii=False,
        )

    def tools(self):
        return dict(
            zip(
                (
                    "service_work_sources",
                    "service_read_work_record",
                    "service_read_source",
                    "service_compare_sources",
                    "service_observe_target",
                    "service_manage_execution",
                    "service_integration_control",
                ),
                (
                    self.sources,
                    self.record,
                    self.read,
                    self.compare,
                    self.target,
                    self.execution,
                    self.integration,
                ),
            )
        )


def definitions():
    from homebrew_harness.tools.catalog import define_tool

    string = {"type": "string", "minLength": 1}
    integer = {"type": "integer", "minimum": 0}
    return [
        define_tool(
            "service_read_work_record",
            "Read this Work's original review jobs, candidates, requirements and recovery plan. Start with kind=index; pages use character offsets and exact versions. Old evidence does not verify changed code.",
            {"kind": {"type": "string", "enum": ["index", "work", "recovery_plan", "jobs", "changesets", "requirements", "evidence"]},
             "identity": string, "offset": integer,
             "limit": {"type": "integer", "minimum": 1}, "expected_version": string},
            [],
        ),
        define_tool(
            "service_work_sources",
            "Inspect this Work source directory: current draft, owned candidates and preserved snapshots. Distinguish HEAD from published PR head.",
            {},
            [],
        ),
        define_tool(
            "service_read_source",
            "Read a file of an owned source_ref with character paging and exact file version.",
            {
                "source_ref": string,
                "path": string,
                "offset": integer,
                "limit": {"type": "integer", "minimum": 1},
                "expected_version": string,
            },
            ["source_ref", "path"],
        ),
        define_tool(
            "service_compare_sources",
            "Compare two owned complete source trees; pages name all changes. Comparison version must stay fixed across pages.",
            {
                "before": string,
                "after": string,
                "offset": integer,
                "limit": {"type": "integer", "minimum": 1},
                "path": string,
                "expected_version": string,
            },
            ["before", "after"],
        ),
        define_tool(
            "service_observe_target",
            "Observe the registered target branch once. Returns an execution ID; its result contains one fixed target source_ref. Observation does not rebase or replace your code.",
            {},
            [],
        ),
        define_tool(
            "service_manage_execution",
            "Observe/control owned Work executions including final candidate tests and Service jobs. Wait expiry does not stop work. Inspect source and actual result before deciding to retry.",
            {
                "operation": {
                    "type": "string",
                    "enum": ["list", "inspect", "read", "wait", "stop", "input"],
                },
                "execution_id": string,
                "cursor": integer,
                "limit": {"type": "integer", "minimum": 1},
                "stream": {"type": "string", "enum": ["output", "result"]},
                "wait_seconds": {"type": "number", "minimum": 0},
                "input": {"type": "string"},
                "request_id": string,
            },
            ["operation"],
        ),
        define_tool(
            "service_integration_control",
            "Inspect, continue after your conflict edits, or abort the selected owned Git integration. This does not choose a new target or publish.",
            {"operation": {"type": "string", "enum": ["inspect", "continue", "abort"]}},
            ["operation"],
        ),
    ]
