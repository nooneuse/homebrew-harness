import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.runs import RunSpec, RunStore
from homebrew_harness.runs.supervisor import RunSupervisor
from tests.integration.test_native_model_protocols import ProtocolService
from tests.support.clients import reply, call


CRASH = """
from pathlib import Path
import sys,os
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from homebrew_harness.runs import worker
from homebrew_harness.storage.durable_runtime import CheckpointWriter
from homebrew_harness.runtime.user_input import UserInputController
from homebrew_harness.tools.files import run_write
from unittest.mock import patch
root=Path(sys.argv[sys.argv.index('--cwd')+1]);marker=root/'.harness/crashed-once'
def once():
 if marker.exists():return False
 marker.write_text('crashed');return True
original_finish=CheckpointWriter.tool_finished
original_started=CheckpointWriter.tool_started
original_end=UserInputController.commit_end
def end(self,*args,**kwargs):
 result=original_end(self,*args,**kwargs)
 if MODE=='completed' and result and once():os._exit(77)
 return result
def finished(self,*args,**kwargs):
 result=original_finish(self,*args,**kwargs)
 if MODE=='known' and once():os._exit(77)
 return result
def started(self,*args,**kwargs):
 if MODE=='unstarted' and once():os._exit(77)
 return original_started(self,*args,**kwargs)
def write(*args,**kwargs):
 result=run_write(*args,**kwargs)
 if MODE=='unknown' and once():os._exit(77)
 return result
with patch.object(UserInputController,'commit_end',end),patch.object(CheckpointWriter,'tool_finished',finished),patch.object(CheckpointWriter,'tool_started',started),patch('homebrew_harness.tools.registry.TOOL_HANDLERS', {**__import__('homebrew_harness.tools.registry',fromlist=['TOOL_HANDLERS']).TOOL_HANDLERS,'write_file':write}):
 raise SystemExit(worker.main())
"""


class ProcessRuns(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.store = RunStore(self.root)
        self.addCleanup(self.store.close)
        for capture in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            capture.__enter__()
            self.addCleanup(capture.__exit__, None, None, None)

    def service(self, responses):
        service = ProtocolService([v.model_dump(exclude_none=True) for v in responses])
        self.addCleanup(service.close)
        return service

    def supervisor(self, service, command=None):
        env = self.base / "private.env"
        env.write_text("")
        supervisor = RunSupervisor(
            self.store, worker_command=command, host_arguments=("--env-file", str(env))
        )
        self.addCleanup(supervisor.close)
        return supervisor

    def env(self, service):
        return patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "synthetic",
                "OPENAI_BASE_URL": service.url,
                "OPENAI_MODEL": "fixture",
            },
        )

    def write(self, identity="write"):
        return reply(
            calls=[
                call(
                    identity,
                    name="write_file",
                    arguments='{"path":"out.txt","content":"done"}',
                )
            ],
            finish_reason="tool_calls",
        )

    def crash_command(self, mode):
        # Explicit test executor; production only launches its fixed worker module.
        script = self.base / "crash.py"
        code = CRASH.replace(
            "sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))",
            "sys.path.insert(0,"
            + repr(str(Path(__file__).resolve().parents[2] / "src"))
            + ")",
        )
        script.write_text("MODE=" + repr(mode) + "\n" + code)
        return (sys.executable, "-I", "-B", str(script))

    def test_real_worker_runs_multiple_segments_and_closes(self):
        server = self.service([self.write(), reply("done")])
        state = self.store.create(
            RunSpec(
                "Write and verify out.txt",
                episode_model_calls=1,
                heartbeat_seconds=0.05,
                heartbeat_timeout=15,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server).supervise(state["id"])
        self.assertEqual(
            result["status"],
            "completed",
            "\n".join(
                p.read_text() for p in (self.root / ".harness/runs").rglob("*.log")
            ),
        )
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(result["episode"], 2)
        self.assertEqual(len(server.requests), 2)
        self.assertEqual((self.root / "out.txt").read_text(), "done")

    def test_actual_crash_after_known_effect_resumes_without_repeating_tool(self):
        server = self.service([self.write(), reply("done")])
        state = self.store.create(
            RunSpec(
                "Write output",
                retry_delay=0.01,
                episode_model_calls=1,
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server, self.crash_command("known")).supervise(
                state["id"]
            )
        self.assertEqual(
            result["status"],
            "completed",
            "\n".join(
                p.read_text() for p in (self.root / ".harness/runs").rglob("*.log")
            ),
        )
        self.assertEqual(result["recovery_count"], 1)
        self.assertEqual(len(server.requests), 2)
        tools = [m for m in server.requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertTrue(
            any(
                m["tool_call_id"] == "write" and "bytes_written" in m["content"]
                for m in tools
            )
        )

    def test_actual_crash_before_acceptance_closes_old_proposal_as_unexecuted(self):
        server = self.service([self.write("old"), self.write("new"), reply("done")])
        state = self.store.create(
            RunSpec(
                "Write output",
                retry_delay=0.01,
                episode_model_calls=1,
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server, self.crash_command("unstarted")).supervise(
                state["id"]
            )
        self.assertEqual(
            result["status"],
            "completed",
            "\n".join(
                p.read_text() for p in (self.root / ".harness/runs").rglob("*.log")
            ),
        )
        old = next(
            m for m in server.requests[1]["messages"] if m.get("tool_call_id") == "old"
        )
        self.assertFalse(json.loads(old["content"])["executed"])
        self.assertEqual(len(server.requests), 3)

    def test_actual_crash_with_unknown_effect_is_not_automatically_replayed(self):
        server = self.service([self.write()])
        state = self.store.create(
            RunSpec(
                "Write output",
                retry_delay=0.01,
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server, self.crash_command("unknown")).supervise(
                state["id"]
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(server.requests), 1, result.get("last_outcome"))
        self.assertTrue(
            (self.root / "out.txt").exists(),
            "\n".join(
                p.read_text() for p in (self.root / ".harness/runs").rglob("*.log")
            ),
        )
        self.assertFalse(result["cleanup_complete"])

    def test_stale_heartbeat_terminates_only_owned_worker_with_bounded_wait(self):
        script = self.base / "stalled.py"
        script.write_text(
            "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)\n"
        )
        state = self.store.create(
            RunSpec(
                "Work",
                heartbeat_seconds=0.01,
                heartbeat_timeout=0.1,
                shutdown_seconds=0.1,
                total_seconds=5,
            )
        )
        supervisor = RunSupervisor(
            self.store, worker_command=(sys.executable, "-I", str(script))
        )
        self.addCleanup(supervisor.close)
        start = time.monotonic()
        result = supervisor.supervise(state["id"])
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["last_outcome"]["reason_code"], "heartbeat_stale")

    def test_deadline_expired_before_launch_does_not_start_worker(self):
        state = self.store.create(RunSpec("Work", total_seconds=0.1))
        time.sleep(0.12)
        supervisor = RunSupervisor(
            self.store,
            worker_command=(
                sys.executable,
                "-I",
                "-c",
                'raise RuntimeError("must not start")',
            ),
        )
        self.addCleanup(supervisor.close)
        result = supervisor.supervise(state["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["generation"], 0)

    def test_watchdog_total_deadline_uses_monotonic_time_with_healthy_heartbeat(self):
        from types import SimpleNamespace

        script = self.base / "heartbeat-only.py"
        script.write_text(
            "import argparse,signal,sys,time\nfrom pathlib import Path\n"
            + "sys.path.insert(0,"
            + repr(str(Path(__file__).resolve().parents[2] / "src"))
            + ")\n"
            + "from homebrew_harness.runs import RunStore\n"
            + "p=argparse.ArgumentParser();p.add_argument('--cwd');p.add_argument('--run-id');p.add_argument('--owner');p.add_argument('--generation',type=int);a=p.parse_args()\n"
            + "s=RunStore(Path(a.cwd));h={'run_id':a.run_id,'owner':a.owner,'generation':a.generation}\n"
            + "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            + "end=time.monotonic()+8\nwhile time.monotonic()<end:s.heartbeat(h,{'phase':'tool'});time.sleep(.02)\n"
        )
        state = self.store.create(
            RunSpec(
                "Work",
                total_seconds=5,
                retries=0,
                heartbeat_seconds=0.05,
                heartbeat_timeout=15,
                shutdown_seconds=0.1,
            )
        )
        supervisor = RunSupervisor(
            self.store, worker_command=(sys.executable, "-I", "-B", str(script))
        )
        self.addCleanup(supervisor.close)
        # Wall time stays before the deadline while the actual process keeps heartbeating.
        clock = SimpleNamespace(
            time=lambda: state["created_at"] + 0.01,
            monotonic=time.monotonic,
            sleep=time.sleep,
        )
        start = time.monotonic()
        with patch("homebrew_harness.runs.supervisor.time", clock):
            result = supervisor.supervise(state["id"])
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(result["status"], "failed", result.get("last_outcome"))
        self.assertEqual(result["last_outcome"]["reason_code"], "deadline_reached")
        self.assertEqual(result["heartbeat"]["phase"], "tool")
        self.assertTrue(result["cleanup_complete"])

    def test_concurrent_status_reads_keep_service_wal_binding_stable(self):
        code = """from pathlib import Path
import sys
sys.path.insert(0,sys.argv[1])
from homebrew_harness.runs import RunStore
store=RunStore(Path(sys.argv[2]))
for i in range(100):
 state=store.get(sys.argv[3])
 store.status(sys.argv[3])
store.close()
"""
        state = self.store.create(RunSpec("Work"))
        import subprocess

        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(Path(__file__).resolve().parents[2] / "src"),
                str(self.root),
                state["id"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(100):
                self.store.status(state["id"])
            output, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, error.decode())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_unknown_effect_can_be_host_inspected_then_explicitly_resumed(self):
        server = self.service([self.write(), reply("done")])
        state = self.store.create(
            RunSpec(
                "Write output",
                retry_delay=0.01,
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server, self.crash_command("unknown")).supervise(
                state["id"]
            )
            self.assertEqual(result["status"], "blocked")
            operation = next(
                iter(
                    self.store.runtime.items(
                        "operations", owner="lead:" + state["session_id"]
                    ).values()
                )
            )
            identity = next(
                iter(
                    self.store.runtime.items(
                        "operations", owner="lead:" + state["session_id"]
                    )
                )
            )
            from homebrew_harness.cli import main

            self.assertEqual(
                main(
                    [
                        "run",
                        "--cwd",
                        str(self.root),
                        "--env-file",
                        str(self.base / "private.env"),
                        "inspect",
                        state["id"],
                        identity,
                        str(operation["revision"]),
                    ]
                ),
                0,
            )
            self.store.control(state["id"], "run")
            result = self.supervisor(server).supervise(state["id"])
        self.assertEqual(result["status"], "completed", result["last_outcome"])
        self.assertEqual(len(server.requests), 2)
        self.assertEqual((self.root / "out.txt").read_text(), "done")

    def test_late_model_tool_proposal_is_discarded_at_total_deadline(self):
        server = self.service([])

        class SlowResponse:
            def popleft(inner):
                time.sleep(5)
                return self.write().model_dump(exclude_none=True)

        server.responses = SlowResponse()
        state = self.store.create(
            RunSpec(
                "Write output",
                total_seconds=6,
                episode_seconds=30,
                request_seconds=30,
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=0.5,
            )
        )
        with self.env(server):
            result = self.supervisor(server).supervise(state["id"])
        self.assertEqual(result["status"], "failed", result["last_outcome"])
        self.assertEqual(result["last_outcome"]["reason_code"], "deadline_reached")
        self.assertFalse((self.root / "out.txt").exists())

    def test_completed_run_crash_reconciles_without_another_model_request(self):
        server = self.service([reply("done")])
        state = self.store.create(
            RunSpec(
                "Answer once",
                heartbeat_timeout=15,
                heartbeat_seconds=0.05,
                shutdown_seconds=2,
            )
        )
        with self.env(server):
            result = self.supervisor(server, self.crash_command("completed")).supervise(
                state["id"]
            )
        self.assertEqual(result["status"], "completed", result["last_outcome"])
        self.assertTrue(result["cleanup_complete"])
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(
            self.store.runtime.get("checkpoints", "lead:" + state["session_id"])[
                "phase"
            ],
            "closed",
        )
