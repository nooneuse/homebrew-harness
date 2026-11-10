import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, ServiceSpec, RemoteService
from tests.services.p2_fixture import Peer, issue
from tests.runtime.test_interaction import eventually


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.control = self.base / "control"
        self.control.mkdir(mode=0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.peer = Peer()
        self.addCleanup(self.peer.close)
        self.env = self.base / "model.env"
        self.env.write_text(
            "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
            + self.peer.url
            + "/v1\nSOURCE_ENDPOINT="
            + self.peer.url
            + "\nSOURCE_TOKEN=synthetic\n"
        )
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(env_file=self.env, shutdown_seconds=5),
        )
        self.addCleanup(self.host.close)

    def start(self, *, sources=False, auto=True, lifetime="attached", **options):
        spec = ServiceSpec(
            str(self.workspace),
            profile="repository_maintenance",
            sources=(
                {
                    "name": "project",
                    "provider": "github",
                    "instance": "primary",
                    "project": "example/project",
                    "api_url_env": "SOURCE_ENDPOINT",
                    "token_env": "SOURCE_TOKEN",
                    "poll_seconds": 0.1,
                    "max_pages": 2,
                },
            )
            if sources
            else (),
            run_defaults={
                "total_seconds": 30,
                "request_seconds": 5,
                "episode_model_calls": 3,
                "shutdown_seconds": 2,
            },
            maintenance={
                "scopes": {
                    "code": {
                        "write_paths": ["src"],
                        "tools": ["read_file", "write_file"],
                        "allow_tests": [],
                    }
                },
                "auto_categories": ["bug"] if auto else [],
                "job_seconds": 15,
                "report_seconds": 3600,
                **options,
            },
        )
        service = self.host.services.start(spec, lifetime=lifetime)
        if lifetime == "detached":
            from tests.support.services import ready_service
            ready_service(service)

        def stop():
            from homebrew_harness.services.launcher import live

            if live(self.control):
                service.stop()
            eventually(lambda: not live(self.control), 15)

        self.addCleanup(stop)
        return service

    def test_manual_work_is_triaged_and_host_scope_is_compiled(self):
        service = self.start()
        work = service.work.submit("Fix the documented local defect.", request_id="one")
        eventually(lambda: work.snapshot().state == "review", 20)
        value = service.store.work(work.id)
        self.assertEqual(value["authority"]["origin"], "host_policy")
        self.assertEqual(value["run_spec"]["write_paths"], ["src"])
        self.assertIn("Fix the documented local defect.", value["run_spec"]["prompt"])
        self.assertIn(
            "Maintain the project within its existing contract.",
            value["run_spec"]["prompt"],
        )
        self.assertIn("Do not publish external changes.", value["run_spec"]["prompt"])
        self.assertTrue(
            any(
                "service_propose" in [t["function"]["name"] for t in r.get("tools", [])]
                for r in self.peer.model_requests
            )
        )

    def test_empty_source_sweeps_do_not_call_the_model(self):
        service = self.start(sources=True)
        eventually(lambda: len(self.peer.source_requests) >= 4, 5)
        self.assertEqual(self.peer.model_requests, [])
        self.assertEqual(service.work.list()["items"], [])

    def test_repository_event_replay_keeps_one_work_and_run(self):
        self.peer.issues = [issue()]
        service = self.start(sources=True)
        eventually(lambda: service.work.list()["items"], 5)
        identity = service.work.list()["items"][0]["id"]
        eventually(lambda: service.work.get(identity).snapshot().state == "review", 20)
        self.assertEqual(len(service.work.list()["items"]), 1)
        self.assertEqual(len(service.records("events")["items"]), 1)

    def test_conversation_and_reports_use_the_remote_control_api(self):
        self.start()
        with RemoteService.from_root(self.control, control=True) as remote:
            accepted = remote.converse("What should we do next?", request_id="message")
            result = eventually(
                lambda: remote.conversation(accepted["id"]).get("result"), 15
            )
            self.assertIn("no permission granted", result["text"])
            self.assertEqual(remote.work.list()["items"], [])

    def test_service_request_approval_uses_host_sdk_and_replays_once(self):
        self.peer.triage["disposition"] = "decide"
        service = self.start(auto=False)
        work = service.work.submit("Consider a substantial new feature.")
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        self.assertEqual(request.ref.target.kind, "service")
        self.assertEqual(request.kind, "work_authority")
        self.assertIsNone(service.store.work(work.id)["run_id"])
        first = self.host.requests.approve(request.ref, decision_id="one")
        self.assertEqual(
            first, self.host.requests.approve(request.ref, decision_id="one")
        )
        eventually(lambda: work.snapshot().state == "review", 20)
        self.assertEqual(service.store.work(work.id)["grant_round"], 1)

    def test_new_source_version_invalidates_old_decision_and_preserves_input(self):
        self.peer.triage["disposition"] = "decide"
        self.peer.issues = [issue()]
        service = self.start(sources=True, auto=False)
        request = eventually(lambda: service.requests() and service.requests()[0], 15)
        self.peer.issues = [
            issue(
                body="Also preserve strict compatibility.",
                updated="2026-01-02T00:00:00Z",
            )
        ]
        work = service.work.get(request.arguments["work_id"])
        eventually(lambda: work.snapshot().input_revision == 1, 10)
        with self.assertRaises(Exception):
            service.decide(request, "approve", decision_id="stale")
        current = eventually(
            lambda: next(
                (r for r in service.requests() if r.ref.id != request.ref.id), None
            ),
            15,
        )
        service.decide(current, "approve", decision_id="current")
        eventually(lambda: work.snapshot().state == "review", 20)
        spec = service.store.work(work.id)["run_spec"]
        self.assertIn("Also preserve strict compatibility.", spec["prompt"])
        self.assertIn("Fix the documented local defect.", spec["prompt"])

    def test_report_has_recorded_facts_and_ack_is_a_host_operation(self):
        service = self.start()
        work = service.work.submit("Fix a local defect.")
        eventually(lambda: work.snapshot().state == "review", 20)
        receipt = service.report(request_id="report")
        report = eventually(
            lambda: (
                service.report(receipt["id"])
                if service.report(receipt["id"])["state"] == "ready"
                else None
            ),
            15,
        )
        self.assertTrue(report["facts"])
        self.assertEqual(report["text"], "Verified service update.")
        self.assertTrue(
            service.acknowledge_report(report["id"], request_id="read")["accepted"]
        )
        self.assertFalse(service.report(request_id="unchanged")["accepted"])

    def test_source_failure_keeps_cursor_and_exposes_retry_state(self):
        self.peer.source_failure = 429
        service = self.start(sources=True)
        state = eventually(lambda: service.records("sources")["items"], 5)[0]
        self.assertEqual(state["state"], "retrying")
        record = service.runtime.maintenance.store.get("sources", state["id"])
        self.assertEqual(record.get("cursor"), {})
        self.assertEqual(self.peer.model_requests, [])

    def test_scope_rejects_paths_and_tools_outside_run_contract(self):
        with self.assertRaises(ValueError):
            ServiceSpec(
                str(self.workspace),
                profile="repository_maintenance",
                run_defaults={"write_paths": ["src"], "tools": ["read_file"]},
                maintenance={
                    "scopes": {
                        "escape": {"write_paths": ["."], "tools": ["write_file"]}
                    }
                },
            )

    def test_client_chat_exit_does_not_create_development_work(self):
        import io
        from contextlib import redirect_stdout
        from homebrew_harness.services.cli import attach

        service = self.start()
        with RemoteService.from_root(self.control, control=True) as remote:
            lines = iter(["What should we build next?", "exit"])
            with redirect_stdout(io.StringIO()):
                attach(remote, input_fn=lambda _prompt: next(lines))
        self.assertEqual(service.work.list()["items"], [])
        self.assertEqual(len(service.records("messages")["items"]), 1)

    def test_model_tools_cannot_read_private_files_or_grant_work(self):
        service = self.start()
        actor = service.runtime.maintenance.agent
        private = self.workspace / ".harness"
        private.mkdir(exist_ok=True)
        (private / "secret").write_text("private state")
        with self.assertRaises(ValueError):
            actor.read_code(".harness/secret")
        with self.assertRaises(ValueError):
            actor.read_code("../model.env")
        self.assertNotIn("approve", actor.handlers())
        self.assertNotIn("bash", actor.handlers())
