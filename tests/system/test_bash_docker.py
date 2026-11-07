"""Opt-in actual Docker Bash policy probes; no fake CLI counts as isolation."""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from homebrew_harness.execution.background import BackgroundManager
from homebrew_harness.execution.bash_policy import BashPolicy
from homebrew_harness.execution.bash_runner import ContainerShellRunner
from homebrew_harness.tools.shell import run_bash

IMAGE = os.getenv("HARNESS_DOCKER_IMAGE", "")
CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


@unittest.skipUnless(
    IMAGE and sys.platform == "linux",
    "requires explicit HARNESS_DOCKER_IMAGE and a Linux Docker controller",
)
class BashDockerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="harness-bash-case-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.runners = []
        self.policy = BashPolicy(mode="container", cli=CLI, image=IMAGE, memory_mb=192)
        for context in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def tearDown(self):
        for runner in self.runners:
            runner.close(0.5)
            self.assertTrue(runner.wait_closed(5))

    def run(self, *args, **kwargs):
        return super().run(*args, **kwargs)

    def execute(self, command, **fields):
        runner = ContainerShellRunner(replace(self.policy, **fields))
        self.runners.append(runner)
        output = json.loads(run_bash(command, self.cwd, runner=runner))
        return output, runner

    def test_current_copy_is_writable_and_host_files_are_unchanged(self):
        (self.cwd / "input.txt").write_text("current uncommitted input")
        (self.cwd / "untracked.txt").write_text("new source")
        output, _ = self.execute(
            "cat input.txt untracked.txt; printf changed > input.txt; printf result > artifact.txt; pwd"
        )
        self.assertEqual(output["exit_code"], 0, output)
        self.assertIn("current uncommitted input", output["stdout"])
        self.assertIn("/workspace", output["stdout"])
        self.assertEqual(
            (self.cwd / "input.txt").read_text(), "current uncommitted input"
        )
        self.assertFalse((self.cwd / "artifact.txt").exists())
        self.assertFalse(output["execution"]["writeback"])
        self.assertTrue(output["execution"]["isolation"]["configuration_verified"])

    def test_quoted_command_and_image_environment_do_not_expand_on_host(self):
        output, _ = self.execute("printf '%s' '$(touch host-side-effect);\"quoted\"' ")
        self.assertEqual(output["exit_code"], 0, output)
        self.assertEqual(output["stdout"], '$(touch host-side-effect);"quoted"')
        self.assertFalse((self.cwd / "host-side-effect").exists())

    def test_host_credentials_environment_network_and_privileges_are_isolated(self):
        (self.root / "outside").write_text("host-only")
        (self.cwd / ".env").write_text("private configuration")
        (self.cwd / ".harness").mkdir()
        (self.cwd / ".harness/private").write_text("host runtime")
        os.environ["HARNESS_ENV_CANARY"] = "synthetic-env"
        self.addCleanup(os.environ.pop, "HARNESS_ENV_CANARY", None)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        code = """import os,socket,json
checks={}
for name,path in %r.items():
 try:open(path).read();checks[name]=False
 except OSError:checks[name]=True
checks['environment']='HARNESS_ENV_CANARY' not in os.environ
checks['uid']=os.getuid()!=0
status=dict(line.split(':',1) for line in open('/proc/self/status') if ':' in line)
checks['caps']=int(status['CapEff'].strip(),16)==0
checks['nonew']=status['NoNewPrivs'].strip()=='1'
s=socket.socket();s.settimeout(.3)
try:s.connect(('127.0.0.1',%d));checks['network']=False
except OSError:checks['network']=True
print(json.dumps(checks))
""" % (
            {
                "host": str(self.root / "outside"),
                "credential": "/inputs/.env",
                "state": "/inputs/.harness/private",
                "socket": "/var/run/docker.sock",
            },
            listener.getsockname()[1],
        )
        (self.cwd / "probe.py").write_text(code)
        output, _ = self.execute("python /workspace/probe.py")
        self.assertEqual(output["exit_code"], 0, output)
        self.assertTrue(all(json.loads(output["stdout"]).values()), output)

    def test_workspace_total_capacity_is_bounded(self):
        code = """import json
limited=False
try:
 for i in range(12):
  with open('output-'+str(i),'wb') as f:f.write(b'x'*1048576)
except OSError:limited=True
print(json.dumps({'limited':limited}))
"""
        (self.cwd / "probe.py").write_text(code)
        output, _ = self.execute(
            "python /workspace/probe.py", workspace_bytes=8 * 1024 * 1024
        )
        self.assertEqual(output["exit_code"], 0, output)
        self.assertTrue(json.loads(output["stdout"])["limited"])
        self.assertFalse(list(self.cwd.glob("output-*")))

    def test_output_limit_is_shared_and_container_is_removed(self):
        output, runner = self.execute(
            "python -c \"import sys;print('x'*2000000);print('y'*2000000,file=sys.stderr)\"",
            output_bytes=1024,
        )
        self.assertTrue(output["output_limit"], output)
        self.assertLessEqual(
            len(output["stdout"].encode()) + len(output["stderr"].encode()), 1024
        )
        self.assertTrue(runner.wait_closed(1))
        self.assertTrue(output["execution"]["isolation"]["cleanup_complete"])

    def test_timeout_stops_children_with_new_process_sessions(self):
        (self.cwd / "probe.py").write_text(
            'import os,time\nif os.fork()==0:os.setsid()\nprint("ready",flush=True)\nwhile True:time.sleep(.1)\n'
        )
        output, runner = self.execute("python /workspace/probe.py", timeout=2)
        self.assertEqual(output["outcome"], "unknown", output)
        self.assertTrue(output["execution"]["isolation"]["cleanup_complete"])
        self.assertTrue(runner.wait_closed(1))

    def test_unavailable_image_never_falls_back_to_host(self):
        output, runner = self.execute("touch native-effect", image="sha256:" + "b" * 64)
        self.assertIn("error", output)
        self.assertFalse((self.cwd / "native-effect").exists())
        self.assertTrue(runner.wait_closed(1))

    def wrapper(self, kind):
        flag = self.root / "control-flag"
        wrapper = self.root / "docker-control"
        if kind == "delay":
            wrapper.write_text(
                "#!/usr/local/bin/python\nimport sys,subprocess,time\nfrom pathlib import Path\nr=subprocess.call(["
                + repr(CLI)
                + ',*sys.argv[1:]])\nif sys.argv[1:2]==["create"] and r==0:\n Path('
                + repr(str(flag))
                + ').write_text("ready")\n time.sleep(20)\nsys.exit(r)\n'
            )
        else:
            flag.write_text("fail")
            wrapper.write_text(
                '#!/usr/local/bin/python\nimport sys,subprocess\nfrom pathlib import Path\nif sys.argv[1:2]==["rm"] and Path('
                + repr(str(flag))
                + ").exists():sys.exit(75)\nsys.exit(subprocess.call(["
                + repr(CLI)
                + ",*sys.argv[1:]]))\n"
            )
        wrapper.chmod(0o700)
        return wrapper, flag

    def test_cancel_during_creation_is_bounded_and_cleanup_is_owned(self):
        wrapper, flag = self.wrapper("delay")
        runner = ContainerShellRunner(
            replace(self.policy, cli=str(wrapper), timeout=15)
        )
        self.runners.append(runner)
        outcomes = []

        def execute():
            try:
                outcomes.append(
                    run_bash("printf must-not-run", self.cwd, runner=runner)
                )
            except Exception as error:
                outcomes.append(error)

        thread = threading.Thread(target=execute)
        thread.start()
        deadline = time.monotonic() + 5
        while not flag.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(flag.exists())
        started = time.monotonic()
        runner.close(0.2)
        self.assertLess(time.monotonic() - started, 0.6)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(runner.wait_closed(3))

    def test_failed_container_removal_retains_pending_until_repaired(self):
        wrapper, flag = self.wrapper("fail")
        output, runner = self.execute("printf completed", cli=str(wrapper))
        self.assertEqual(output["outcome"], "unknown", output)
        self.assertFalse(runner.wait_closed(0.01))
        flag.unlink()
        self.assertTrue(runner.wait_closed(5))

    def test_background_command_uses_real_container_without_host_writeback(self):
        manager = BackgroundManager(self.cwd, run_bash, bash_policy=self.policy)
        self.addCleanup(manager.close)
        identity = manager.start("printf generated > new.txt; printf finished")["bg_id"]
        self.assertTrue(manager._workers.wait(5))
        output = json.loads(manager.check(identity, workdir=self.cwd))
        self.assertEqual(output["status"], "completed", output)
        self.assertEqual(output["result"]["execution"]["mode"], "container")
        self.assertFalse((self.cwd / "new.txt").exists())
        self.assertTrue(manager.wait_closed(1))

    def test_executable_input_runs_without_granting_execute_to_plain_files(self):
        script = self.cwd / "run.sh"
        script.write_text("#!/bin/bash\nprintf executable-ok")
        script.chmod(0o755)
        plain = self.cwd / "plain.txt"
        plain.write_text("ordinary")
        plain.chmod(0o644)
        output, _ = self.execute("./run.sh && test ! -x plain.txt")
        self.assertEqual(output["exit_code"], 0, output)
        self.assertEqual(output["stdout"], "executable-ok", output)
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)
