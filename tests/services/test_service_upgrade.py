"""Real installed-version process handoff and bounded compatible rollback."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import homebrew_harness
from tests.services import test_operations as fixture
from tests.support.clients import reply


DOCKER_IMAGE = os.getenv("HARNESS_DOCKER_IMAGE", "")
DOCKER_CLI = os.getenv("HARNESS_DOCKER_CLI", "/usr/bin/docker")


class UpgradeTests(unittest.TestCase):
    setUp = fixture.OperationTests.setUp
    host = fixture.OperationTests.host

    def scenario(self, mode, *, validation_mode="local"):
        source = Path(homebrew_harness.__file__).resolve().parent
        candidate = self.base / "candidate"
        pkg = candidate / "src/homebrew_harness"
        shutil.copytree(source, pkg, ignore=shutil.ignore_patterns("__pycache__"))
        p = pkg / "__init__.py"
        p.write_text(
            p.read_text().replace('__version__ = "0.1.0"', '__version__ = "0.1.1"')
        )
        installed = self.base / "installed/homebrew_harness"
        shutil.copytree(pkg, installed)
        (candidate / "app").mkdir()
        (candidate / "tests").mkdir()
        (candidate / "tests/__init__.py").write_text("")
        (candidate / "tests/test_contract.py").write_text(
            'import unittest,sys,os\nsys.path.insert(0,os.path.abspath("src"))\n'
            "from homebrew_harness import ServiceSpec\n"
            "class Contracts(unittest.TestCase):\n"
            " def test_operations_contract(self):\n"
            '  self.assertEqual(ServiceSpec(os.path.abspath(".")).operations["keep_recent"],10)\n'
            '  with self.assertRaises(ValueError):ServiceSpec(os.path.abspath("."),operations={"operation_seconds":0})\n'
        )
        (self.base / "app").mkdir()
        (self.base / "registry.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "candidate-tests",
                            "builtin": "unittest",
                            "description": "Candidate public contracts",
                            "roles": ["lead"],
                            "input_globs": ["src/**", "tests/**"],
                            "locked_globs": ["tests/**"],
                            "authorization": "allow",
                            "timeout": 20,
                        }
                    ],
                }
            )
        )
        if validation_mode == "isolated":
            from homebrew_harness.execution.test_runner import _UNITTEST

            registry = json.loads((self.base / "registry.json").read_text())
            registry["execution"] = {
                "backend": "container",
                "policy": {
                    "cli": DOCKER_CLI,
                    "image": DOCKER_IMAGE,
                    "native_argv": ["{python}", "-I", "-c", _UNITTEST],
                    "argv": [
                        "/usr/local/bin/python",
                        "-I",
                        "-c",
                        "import sys;sys.path.insert(0,'/opt/service-libs');exec("
                        + repr(_UNITTEST)
                        + ")",
                    ],
                    "memory_mb": 256,
                    "pids": 64,
                },
            }
            (self.base / "registry.json").write_text(json.dumps(registry))
        _, model = self.host([reply("After upgrade")])
        env = self.base / "model.env"
        runtime = self.base / "runtime"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(runtime)],
            check=True,
            capture_output=True,
            timeout=20,
        )
        python = runtime / "bin/python"
        site = Path(
            subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import site;print(site.getsitepackages()[0])",
                ],
                text=True,
            ).strip()
        )
        deps = [
            p
            for p in sys.path
            if p and ("site-packages" in p or p.endswith("/service-libs"))
        ]
        (site / "deps.pth").write_text("\n".join(deps) + "\n")
        pth = site / "source.pth"
        pth.write_text(
            "import sys; sys.path.insert(0, " + repr(str(source.parent)) + ")\n"
        )
        config = self.base / "driver.json"
        config.write_text(
            json.dumps(
                {"base": str(self.base), "env": str(env), "pth": str(pth), "mode": mode}
            )
        )
        script = Path(__file__).parents[1] / "support/service_upgrade_driver.py"
        result = subprocess.run(
            [str(python), "-I", "-B", str(script), str(config)],
            capture_output=True,
            text=True,
            timeout=100,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(data["closed"])
        self.assertTrue(data["initially_parked"])
        return data

    def test_new_version_stays_parked_until_confirmation_and_runs_native_work(self):
        self.assertEqual(self.scenario("confirm")["text"], "After upgrade")

    def test_unconfirmed_version_can_return_to_recorded_old_runtime(self):
        self.assertEqual(self.scenario("rollback")["mode"], "rollback")
