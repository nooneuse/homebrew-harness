"""Opt-in actual Docker checks. No fake CLI is treated as OS-isolation evidence."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from homebrew_harness.execution.execution_backend import (
    ContainerBackend,
    ContainerPolicy,
)
from homebrew_harness.execution.test_runner import (
    TestRecipe,
    TestRunner,
    TestRunnerError,
)

IMAGE = os.getenv("HARNESS_DOCKER_IMAGE", "")
CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


class ObservedBackend(ContainerBackend):
    def __init__(self, policy):
        super().__init__(policy)
        self.observed = []
        self.started = threading.Event()

    def launch(self, request):
        process = super().launch(request)
        self.started.set()
        return process

    def finish(self, process):
        entry = self.active.get(process.pid)
        if entry:
            result = subprocess.run(
                [self.policy.cli, "inspect", entry[0]],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                self.observed.append(json.loads(result.stdout)[0])
        return super().finish(process)


@unittest.skipUnless(
    IMAGE and sys.platform == "linux",
    "requires explicit HARNESS_DOCKER_IMAGE and a Linux Docker controller",
)
class DockerIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="harness-docker-case-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runners = []
        self.backends = []

    def tearDown(self):
        for runner in self.runners:
            runner.close(timeout=0.5)
        for backend in self.backends:
            for name in list(backend.pending):
                subprocess.run([CLI, "rm", "-f", name], capture_output=True, timeout=3)
            backend.wait_closed(3)

    def runner(
        self,
        code,
        *,
        timeout=10,
        memory=128,
        pids=32,
        output=64000,
        file_bytes=64000000,
        cli=CLI,
    ):
        source = self.workspace / "probe.py"
        source.write_text(code)
        argv = (sys.executable, "-I", str(source))
        policy = ContainerPolicy(
            cli,
            IMAGE,
            ("/usr/local/bin/python", "-I", "/workspace/probe.py"),
            argv,
            memory_mb=memory,
            pids=pids,
            cpus=0.5,
        )
        backend = ObservedBackend(policy)
        recipe = TestRecipe(
            "isolation-probe",
            argv,
            input_globs=("probe.py", "input.txt"),
            locked_globs=("probe.py",),
            timeout=timeout,
            output_bytes=output,
            file_bytes=file_bytes,
            cpu_seconds=10,
        )
        runner = TestRunner(
            self.workspace, [recipe], backend=backend, max_runs=1, total_seconds=30
        )
        runner.approve(recipe.name, max_runs=1)
        self.runners.append(runner)
        self.backends.append(backend)
        return runner, backend

    def run_probe(self, code, **kwargs):
        runner, backend = self.runner(code, **kwargs)
        result = runner.run("isolation-probe")
        return result, backend

    def test_readonly_source_host_files_credentials_and_environment(self):
        canary = self.root / "outside-canary"
        canary.write_text("host-only-value")
        (self.workspace / "input.txt").write_text("task input")
        (self.workspace / "not-declared.txt").write_text("not an authorized input")
        (self.workspace / ".env").write_text("TEST_CREDENTIAL=synthetic-value")
        (self.workspace / ".harness").mkdir()
        (self.workspace / ".harness/private.txt").write_text("private state")
        os.environ["HARNESS_PRIVATE_CANARY"] = "synthetic-environment-value"
        self.addCleanup(os.environ.pop, "HARNESS_PRIVATE_CANARY", None)
        code = """import os,json
checks={}
for name,path in %s.items():
 try:open(path,'rb').read();checks[name]=False
 except OSError:checks[name]=True
checks['environment']='HARNESS_PRIVATE_CANARY' not in os.environ
checks['input']=open('/workspace/input.txt').read()=='task input'
try:open('/workspace/input.txt','w').write('changed');checks['readonly']=False
except OSError:checks['readonly']=True
print(json.dumps(checks))
""" % repr(
            {
                "host_file": str(canary),
                "undeclared": "/workspace/not-declared.txt",
                "credential": "/workspace/.env",
                "runtime_state": "/workspace/.harness/private.txt",
                "docker_socket": "/var/run/docker.sock",
            }
        )
        result, backend = self.run_probe(code)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(all(json.loads(result["stdout_preview"]).values()), result)
        self.assertEqual(canary.read_text(), "host-only-value")
        self.assertEqual((self.workspace / "input.txt").read_text(), "task input")
        self.assertTrue(backend.observed)

    def test_network_namespace_cannot_reach_controller_listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        code = (
            """import socket,json
s=socket.socket();s.settimeout(.5)
try:s.connect(('127.0.0.1',%d));blocked=False
except OSError:blocked=True
print(json.dumps({'blocked':blocked,'interfaces':[name for _,name in socket.if_nameindex()]}))
"""
            % port
        )
        result, backend = self.run_probe(code)
        data = json.loads(result["stdout_preview"])
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(data["blocked"])
        self.assertEqual(data["interfaces"], ["lo"])
        self.assertEqual(backend.observed[-1]["HostConfig"]["NetworkMode"], "none")

    def test_uid_capabilities_no_new_privileges_and_config(self):
        code = """import os,json
status=dict(line.strip().split(':',1) for line in open('/proc/self/status') if ':' in line)
print(json.dumps({'uid':os.getuid(),'caps':status['CapEff'].strip(),'nonew':status['NoNewPrivs'].strip()}))
"""
        result, backend = self.run_probe(code)
        data = json.loads(result["stdout_preview"])
        config = backend.observed[-1]
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(data["uid"], 65534)
        self.assertEqual(int(data["caps"], 16), 0)
        self.assertEqual(data["nonew"], "1")
        self.assertTrue(config["HostConfig"]["ReadonlyRootfs"])
        self.assertFalse(config["HostConfig"]["Privileged"])
        self.assertEqual(config["HostConfig"]["NanoCpus"], 500000000)
        self.assertEqual(config["HostConfig"]["Memory"], 128 * 1024 * 1024)
        self.assertEqual(config["Config"]["Entrypoint"], ["/usr/local/bin/python"])

    def test_file_size_limit_and_nonexecuting_tmpfs(self):
        code = """import os,json,resource,subprocess
checks={'fsize':resource.getrlimit(resource.RLIMIT_FSIZE)[0]==32768}
try:open('/tmp/large','wb').write(b'x'*65536);checks['limited']=False
except OSError:checks['limited']=True
open('/tmp/program','w').write('#!/bin/sh\\nexit 0\\n');os.chmod('/tmp/program',0o755)
try:subprocess.run(['/tmp/program']);checks['noexec']=False
except PermissionError:checks['noexec']=True
print(json.dumps(checks))
"""
        result, _ = self.run_probe(code, file_bytes=32768)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(all(json.loads(result["stdout_preview"]).values()))

    def test_pid_limit_is_enforced(self):
        code = """import os,time,json,signal
children=[];limited=False
try:
 for i in range(40):
  try:pid=os.fork()
  except OSError:limited=True;break
  if pid==0:time.sleep(20);os._exit(0)
  children.append(pid)
finally:
 for pid in children:
  try:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
  except OSError:pass
print(json.dumps({'limited':limited,'children':len(children)}))
"""
        result, backend = self.run_probe(code, pids=16)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(json.loads(result["stdout_preview"])["limited"])
        self.assertEqual(backend.observed[-1]["HostConfig"]["PidsLimit"], 16)

    def test_memory_limit_kills_only_task_container(self):
        result, backend = self.run_probe(
            "value=bytearray(256*1024*1024)\nprint('unexpected')\n", memory=96
        )
        self.assertEqual(result["status"], "failed", result)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue(backend.observed[-1]["State"]["OOMKilled"])
        self.assertTrue(result["isolation"]["cleanup_complete"])

    def test_output_limit_and_container_removal(self):
        result, backend = self.run_probe("print('x'*2000000,flush=True)\n", output=1024)
        self.assertEqual(result["status"], "output_limit", result)
        self.assertLessEqual(len(result["stdout_preview"].encode()), 1024)
        self.assertTrue(result["isolation"]["cleanup_complete"])
        self.assertTrue(backend.wait_closed(0.2))

    def test_deadline_stops_descendants_that_create_new_session(self):
        code = """import os,time
pid=os.fork()
if pid==0:os.setsid()
print('started',flush=True)
while True:time.sleep(.1)
"""
        result, backend = self.run_probe(code, timeout=2)
        self.assertEqual(result["status"], "timeout", result)
        self.assertTrue(result["isolation"]["cleanup_complete"])
        self.assertTrue(backend.wait_closed(0.2))

    def test_cancellation_is_bounded_and_removes_task(self):
        runner, backend = self.runner(
            "import time\nprint('ready',flush=True)\ntime.sleep(20)\n", timeout=15
        )
        results = []
        thread = threading.Thread(
            target=lambda: results.append(runner.run("isolation-probe"))
        )
        thread.start()
        self.assertTrue(backend.started.wait(5))
        time.sleep(0.2)
        start = time.monotonic()
        runner.close(timeout=0.5)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(results[0]["status"], "cancelled", results)
        self.assertTrue(results[0]["isolation"]["cleanup_complete"])
        self.assertTrue(runner.wait_closed(0.5))

    def test_cleanup_failure_retains_state_until_reconciled(self):
        flag = self.root / "fail-removal"
        flag.write_text("yes")
        wrapper = self.root / "docker-control"
        wrapper.write_text(
            "#!/usr/local/bin/python\nimport sys,subprocess\nfrom pathlib import Path\nif sys.argv[1:2]==['rm'] and Path("
            + repr(str(flag))
            + ").exists():sys.exit(75)\nsys.exit(subprocess.call(["
            + repr(CLI)
            + ",*sys.argv[1:]]))\n"
        )
        wrapper.chmod(0o700)
        result, backend = self.run_probe("print('done')\n", cli=str(wrapper))
        self.assertFalse(result["isolation"]["cleanup_complete"])
        self.assertTrue(backend.pending)
        self.assertFalse(backend.wait_closed(0))
        self.assertTrue(all(path.exists() for path, _ in backend.pending.values()))
        flag.unlink()
        self.assertTrue(backend.wait_closed(3))
        self.assertFalse(backend.pending)

    def test_missing_backend_does_not_execute_host_command(self):
        result, _ = self.run_probe(
            "open('native-side-effect','w').write('must not run')\n",
            cli=str(self.root / "missing-cli"),
        )
        self.assertEqual(result["status"], "error", result)
        self.assertFalse((self.workspace / "native-side-effect").exists())

    def test_tmpfs_capacity_is_enforced(self):
        code = """import json
limited=False
try:
 for number in range(3):
  with open('/tmp/quota-'+str(number),'wb') as stream:
   for chunk in range(24):stream.write(b'x'*1048576)
except OSError:limited=True
print(json.dumps({'limited':limited}))
"""
        result, _ = self.run_probe(code, memory=192)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(json.loads(result["stdout_preview"])["limited"])

    def test_cancel_during_create_releases_caller_and_reconciles_stopped_container(
        self,
    ):
        ready = self.root / "creation-ready"
        wrapper = self.root / "slow-control"
        wrapper.write_text(
            "#!/usr/local/bin/python\nimport sys,subprocess,time\nfrom pathlib import Path\nresult=subprocess.call(["
            + repr(CLI)
            + ",*sys.argv[1:]])\nif sys.argv[1:2]==['create'] and result==0:\n Path("
            + repr(str(ready))
            + ").write_text('ready')\n time.sleep(20)\nsys.exit(result)\n"
        )
        wrapper.chmod(0o700)
        runner, backend = self.runner(
            "print('must not start')\n", timeout=15, cli=str(wrapper)
        )
        results = []
        thread = threading.Thread(
            target=lambda: results.append(runner.run("isolation-probe"))
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        start = time.monotonic()
        runner.close(timeout=0.3)
        self.assertLess(time.monotonic() - start, 0.7)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "cancelled", results)
        self.assertTrue(runner.wait_closed(2))
        self.assertFalse(backend.pending)

    def test_receipt_is_bound_to_current_inputs_and_verified_configuration(self):
        runner, backend = self.runner("print('verified')\n")
        result = runner.run("isolation-probe")
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["source_stable"])
        self.assertTrue(result["isolation"]["configuration_verified"])
        self.assertTrue(result["isolation"]["os_isolation_verified"])
        self.assertTrue(runner.verify("isolation-probe", self.workspace)["ok"])
        (self.workspace / "probe.py").write_text("print('changed')\n")
        self.assertFalse(runner.verify("isolation-probe", self.workspace)["ok"])

    def test_missing_host_grant_cannot_create_container(self):
        runner, backend = self.runner("print('not authorized')\n")
        runner.revoke("isolation-probe")
        with self.assertRaises(TestRunnerError):
            runner.run("isolation-probe")
        self.assertFalse(backend.started.is_set())
        self.assertFalse(backend.active)
        self.assertFalse(backend.pending)

    def test_readonly_snapshot_preserves_script_execute_and_strips_special_bits(self):
        script = self.workspace / "probe.py"
        script.write_text("#!/bin/sh\nprintf executable-test\n")
        script.chmod(0o6755)
        argv = ("/bin/sh", str(script))
        backend = ObservedBackend(
            ContainerPolicy(CLI, IMAGE, ("/workspace/probe.py",), argv)
        )
        recipe = TestRecipe(
            "script",
            argv,
            input_globs=("probe.py",),
            locked_globs=("probe.py",),
            timeout=10,
        )
        runner = TestRunner(self.workspace, [recipe], backend=backend)
        self.runners.append(runner)
        self.backends.append(backend)
        runner.approve("script")
        result = runner.run("script")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["exit_code"], 0)
