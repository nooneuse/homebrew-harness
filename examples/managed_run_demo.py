"""Run a deterministic A-E integration scenario with local protocol services."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from homebrew_harness.runs import RunStore
from homebrew_harness.runs.requests import HumanRequests
from homebrew_harness.runs.sources import iso_time

PROMPT = "Implement normalize_title: lowercase text, trim and collapse whitespace. Ask how empty input should behave. Run the registered checks and write REPORT.md after verification. Use current pull feedback as reference. Then implement prefix and a dependent suffix using the declared read-only input, integrate, verify and publish their outputs."
CANCEL_PROMPT = "Prepare a cancellable task, then ask the host before continuing."
FOLLOWUP_PROMPT = "Create followup.txt after resuming the same registered model."
PREFIX = "def add_prefix(value):\n    return '[' + value\n"
SUFFIX = "from prefix import add_prefix\ndef add_suffix(value):\n    return add_prefix(value) + ']'\n"
INITIAL = "def normalize_title(value):\n    return value.strip().lower()\n"
FIXED = "def normalize_title(value):\n    return ' '.join(value.split()).lower()\n"
REVISED = FIXED + "\n# Whitespace normalization is intentional.\n"
CHECKS = """import unittest
from normalize import normalize_title
class Normalization(unittest.TestCase):
    def test_spacing(self): self.assertEqual(normalize_title('  Mixed   TITLE  '), 'mixed title')
    def test_empty(self): self.assertEqual(normalize_title('   '), '')
"""


def completion(call_id, name, arguments):
    return {
        "id": "fixture",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
    }


class DemoService:
    def __init__(self, store, head):
        self.store, self.head = store, head
        self.messages, self.notices = [], []
        self.step = 0
        self.proof_restart = False
        self.worker_steps = {}
        self.primary_failures = 0
        self.actor_lock = threading.Lock()
        self.prefix_paused = False
        self.direction_seen = False
        self.direction_count = 0
        self.steering_id = None
        self.cooperative_wait_seen = False
        self.steering_rejection_seen = False
        self.dependent_input_seen = False
        self.tail_messages = []
        self.same_model_fault = False
        self.request_timeout_seen = False
        self.tail_steps = 0
        self.comment_version = iso_time(time.time())
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path.endswith("/pulls/1"):
                    self.send({"head": {"sha": outer.head}})
                elif path.endswith("/comments"):
                    self.send(
                        [
                            {
                                "id": 1,
                                "user": {"id": 7},
                                "body": "Remember to collapse internal whitespace, as requested.",
                                "updated_at": outer.comment_version,
                            }
                        ]
                    )
                elif path.endswith("/reviews"):
                    self.send([])
                elif path.endswith("/check-runs"):
                    self.send(
                        {
                            "check_runs": [
                                {
                                    "id": 2,
                                    "app": {"id": 11},
                                    "head_sha": "0" * 40,
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "output": {"summary": "Old commit feedback"},
                                },
                                {
                                    "id": 3,
                                    "app": {"id": 11},
                                    "head_sha": outer.head,
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "output": {
                                        "summary": "Collapse whitespace in the implementation"
                                    },
                                },
                            ]
                        }
                    )
                else:
                    self.send([])

            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/notice":
                    outer.notices.append(raw)
                    self.send({})
                    return
                answer = outer.model_response(raw)
                if isinstance(answer, tuple):
                    self.send(answer[1], answer[0])
                else:
                    self.send(answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "/", "", "")
        )

    @staticmethod
    def text_reply(text):
        answer = completion("unused", "list_tasks", {})
        answer["choices"][0].update(
            finish_reason="stop", message={"role": "assistant", "content": text}
        )
        return answer

    def model_response(self, raw):
        encoded = json.dumps(raw["messages"])
        if CANCEL_PROMPT in encoded or FOLLOWUP_PROMPT in encoded:
            self.tail_messages.append(raw["messages"])
            return self.tail_response(
                raw, FOLLOWUP_PROMPT if FOLLOWUP_PROMPT in encoded else CANCEL_PROMPT
            )
        self.messages.append(raw["messages"])
        if not raw.get("tools"):
            return self.text_reply(
                "Preserve original constraints and use current host stage state; read archived evidence before repeating work."
            )
        if (
            raw.get("model") == "fixture-lead"
            and self.step == 0
            and not self.request_timeout_seen
        ):
            self.request_timeout_seen = True
            time.sleep(10.2)
            return self.text_reply("Late fixture output must be discarded.")
        if raw.get("model") == "fixture-worker" or self.step >= 25:
            return self.extended_response(raw)
        step = self.step
        self.step += 1
        run = next(iter(self.store.runtime.items("runs").values()))
        tasks = {
            r["stage"]["key"]: r
            for r in self.store.runtime.items("tasks").values()
            if r.get("stage")
        }
        if step == 0:
            answer = completion(
                "question",
                "request_attention",
                {
                    "question": "Should empty input return an empty string?",
                    "reason": "Need a requirement decision",
                    "impact": "Empty input behavior",
                },
            )
        elif step in {1, 2, 11, 16, 21}:
            key = "report" if step in {1, 11, 21} else "implement"
            answer = completion(
                "claim-" + str(step),
                "claim_task",
                {"task_id": tasks[key]["id"]},
            )
        elif step in {3, 7, 15}:
            answer = completion(
                "write-" + str(step),
                "write_file",
                {
                    "path": "normalize.py",
                    "content": INITIAL
                    if step == 3
                    else FIXED
                    if step == 7
                    else REVISED,
                },
            )
        elif step in {4, 10, 17, 20, 24}:
            key = "report" if step == 24 else "implement"
            answer = completion(
                "submit-" + str(step),
                "complete_task",
                {
                    "task_id": tasks[key]["id"],
                    "summary": "Submit the current stage for host verification",
                    "next_steps": "Read the handoff before the next stage",
                },
            )
        elif step in {5, 8, 13, 18}:
            answer = completion(
                "test-" + str(step),
                "run_tests",
                {"name": "report-check" if step == 13 else "unit-checks"},
            )
        elif step in {6, 9, 14, 19}:
            approved = [
                r
                for r in HumanRequests(self.store).records(run["id"])
                if r["state"] == "approved"
            ]
            if not approved:
                raise RuntimeError("Demo model call arrived without its approval")
            if step == 14:
                self.proof_restart = tasks["implement"]["status"] == "completed"
            answer = completion(
                "approved-" + str(step),
                "execute_approved",
                {"request_id": approved[-1]["id"]},
            )
        elif step in {12, 22}:
            answer = completion(
                "handoff-" + str(step),
                "get_handoff",
                {"handoff_id": tasks["implement"]["stage"]["handoff"]},
            )
        elif step == 23:
            answer = completion(
                "report",
                "write_file",
                {
                    "path": "REPORT.md",
                    "content": "Whitespace normalization implemented; empty input returns an empty string. Protected checks passed.",
                },
            )
        elif step == 25:
            answer = completion("unused", "list_tasks", {})
            answer["choices"][0].update(
                finish_reason="stop",
                message={
                    "role": "assistant",
                    "content": "Implementation and report verified.",
                },
            )
        else:
            raise RuntimeError("Unexpected additional demo model request")
        return answer

    def extended_response(self, raw):
        tasks = {
            value["stage"]["key"]: value
            for value in self.store.runtime.items("tasks").values()
            if value.get("stage")
        }
        run = next(iter(self.store.runtime.items("runs").values()))
        if raw.get("model") == "fixture-lead":
            self.primary_failures += 1
            return 503, {
                "error": {
                    "message": "Synthetic primary-model outage",
                    "type": "server_error",
                }
            }
        if raw.get("model") == "fixture-worker":
            name = (
                "prefix"
                if "You are teammate prefix" in json.dumps(raw["messages"])
                else "suffix"
            )
            with self.actor_lock:
                n = self.worker_steps.get(name, 0)
                self.worker_steps[name] = n + 1
            if name == "prefix" and n == 0:
                return 503, {
                    "error": {
                        "message": "Synthetic recoverable member outage",
                        "type": "server_error",
                    }
                }
            task = tasks[name]
            root = self.store.root / ".worktrees" / task["worktree"]
            if name == "prefix" and not self.prefix_paused:
                self.prefix_paused = True
                return self.text_reply(
                    "I need a Lead instruction to continue this unfinished stage."
                )
            if name == "prefix":
                self.steering_rejection_seen |= any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == "prefix2"
                    and "superseded" in message.get("content", "").lower()
                    for message in raw["messages"]
                )
                prior = self.store.runtime.get(
                    "run_member_outcomes", run["id"] + ":prefix", {}
                )
                if prior.get("kind") == "waiting_input":
                    self.cooperative_wait_seen = True
                if (root / "prefix.py").read_text() != PREFIX:
                    if self.steering_id is None:
                        self.steering_id = self.store.submit(
                            run["id"], "continue", mode="steer"
                        )["input_id"]
                    return completion(
                        name + str(n),
                        "write_file",
                        {"path": "prefix.py", "content": PREFIX},
                    )
                receipts = [
                    value
                    for value in self.store.runtime.items("test_receipts").values()
                    if value["recipe"] == "member-checks"
                    and value.get("task_id") == task["id"]
                    and value["status"] == "passed"
                ]
                if len(receipts) < 2:
                    return completion(
                        name + str(n), "run_tests", {"name": "member-checks"}
                    )
            else:
                if (root / "prefix.py").read_text() != PREFIX:
                    raise RuntimeError(
                        "The dependent member did not receive its accepted predecessor input"
                    )
                self.dependent_input_seen = True
                if (root / "suffix.py").read_text() != SUFFIX:
                    if n == 0:
                        return completion(
                            name + str(n),
                            "get_handoff",
                            {"handoff_id": tasks["prefix"]["stage"]["handoff"]},
                        )
                    return completion(
                        name + str(n),
                        "write_file",
                        {"path": "suffix.py", "content": SUFFIX},
                    )
            return completion(
                name + str(n),
                "complete_task",
                {"task_id": task["id"], "summary": "Owned output verified"},
            )
        if raw.get("model") != "fixture-backup":
            raise RuntimeError("Unexpected model alias in the extended demo")
        for message in raw["messages"]:
            if message.get("role") == "tool" and message.get(
                "tool_call_id", ""
            ).startswith("direct-prefix"):
                try:
                    result = json.loads(message["content"])
                    if not isinstance(result, dict) or "error" not in result:
                        self.direction_seen = True
                except ValueError:
                    pass
        prefix_outcome = self.store.runtime.get(
            "run_member_outcomes", run["id"] + ":prefix", {}
        )
        if prefix_outcome.get("kind") == "needs_direction" and not self.direction_seen:
            self.direction_count += 1
            return completion(
                "direct-prefix-" + str(self.direction_count),
                "send_message",
                {
                    "to": "prefix",
                    "content": "Continue only the assigned prefix.py implementation and submit its verified stage.",
                },
            )
        members = self.store.runtime.items("run_members")
        for declaration in run["spec"]["members"]:
            name = declaration["name"]
            if not any(member["name"] == name for member in members.values()):
                if name == "suffix" and tasks["prefix"]["status"] != "completed":
                    return self.text_reply("Waiting for the active predecessor stage.")
                return completion(
                    "spawn-" + name,
                    "spawn_teammate",
                    {
                        "name": name,
                        "role": declaration["role"],
                        "prompt": declaration["prompt"],
                        "task_id": tasks[name]["id"],
                        "require_plan": False,
                        "model_profile": "worker",
                    },
                )
        if any(tasks[name]["status"] != "completed" for name in ("prefix", "suffix")):
            return self.text_reply("Awaiting current member results.")
        task = tasks["integrate"]
        transfers = list(self.store.runtime.items("run_transfers").values())
        if task["status"] == "pending":
            return completion(
                "claim-integration", "claim_task", {"task_id": task["id"]}
            )
        if not any(
            value["kind"] == "integrate" and value["state"] == "done"
            for value in transfers
        ):
            return completion("integrate", "integrate_stage", {"task_id": task["id"]})
        receipts = list(self.store.runtime.items("test_receipts").values())
        if not any(
            value["recipe"] == "team-checks" and value["status"] == "passed"
            for value in receipts
        ):
            return completion("team-checks", "run_tests", {"name": "team-checks"})
        if task["status"] == "in_progress":
            return completion(
                "accept-integration", "complete_task", {"task_id": task["id"]}
            )
        if not any(
            value["kind"] == "publish" and value["state"] == "done"
            for value in transfers
        ):
            return completion("publish", "publish_stage", {"task_id": task["id"]})
        return self.text_reply(
            "All declared stages integrated, verified and published."
        )

    def tail_response(self, raw, prompt):
        run = next(
            value
            for value in self.store.runtime.items("runs").values()
            if value["spec"]["prompt"] == prompt
        )
        task = next(
            value
            for value in self.store.runtime.items("tasks").values()
            if (value.get("stage") or {}).get("run_id") == run["id"]
        )
        self.tail_steps += 1
        if prompt == FOLLOWUP_PROMPT and not self.same_model_fault:
            self.same_model_fault = True
            return 429, {
                "error": {
                    "code": "insufficient_quota",
                    "type": "insufficient_quota",
                    "message": "Synthetic quota interruption",
                }
            }
        if task["status"] == "pending":
            return completion(
                "tail-claim-" + str(self.tail_steps),
                "claim_task",
                {"task_id": task["id"]},
            )
        if prompt == CANCEL_PROMPT:
            return completion(
                "tail-question-" + str(self.tail_steps),
                "request_attention",
                {
                    "question": "May this prepared task continue?",
                    "reason": "Cancellation boundary test",
                    "impact": "Prepared task",
                },
            )
        if not (self.store.root / "followup.txt").exists():
            return completion(
                "tail-write-" + str(self.tail_steps),
                "write_file",
                {"path": "followup.txt", "content": "same model resumed"},
            )
        return completion(
            "tail-complete-" + str(self.tail_steps),
            "complete_task",
            {"task_id": task["id"]},
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def run_demo(base, *, timeout=360):
    base = Path(base).resolve()
    if base.exists() and any(base.iterdir()):
        raise ValueError("Choose an empty demo output directory.")
    base.mkdir(parents=True, exist_ok=True)
    workspace, private = base / "workspace", base / "private"
    workspace.mkdir()
    private.mkdir(mode=0o700)
    (workspace / "normalize.py").write_text(
        "def normalize_title(value):\n    return value\n"
    )
    (workspace / "tests").mkdir()
    (workspace / "tests/__init__.py").write_text("")
    (workspace / "tests/test_normalize.py").write_text(CHECKS)
    (workspace / ".gitignore").write_text(".harness/\n.worktrees/\n")
    (workspace / "prefix.py").write_text("def add_prefix(value):\n    return value\n")
    (workspace / "suffix.py").write_text("def add_suffix(value):\n    return value\n")
    (workspace / "tests/test_team.py").write_text(
        "import unittest\nfrom prefix import add_prefix\nfrom suffix import add_suffix\nclass TeamChecks(unittest.TestCase):\n    def test_brackets(self): self.assertEqual(add_suffix('x'), '[x]')\n"
    )
    unrelated = workspace / "unrelated"
    unrelated.mkdir()
    for index in range(5001):
        (unrelated / ("file" + str(index) + ".txt")).write_text("unrelated content")
    for command in (
        ("init", "-q"),
        (
            "add",
            "normalize.py",
            "tests",
            ".gitignore",
            "prefix.py",
            "suffix.py",
            "unrelated",
        ),
        ("commit", "-qm", "fixture"),
    ):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *command,
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True
    ).strip()
    store = RunStore(workspace)
    server = DemoService(store, head)
    processes, handles = [], []
    deadline = time.monotonic() + timeout
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENAI_", "ANTHROPIC_", "DASHSCOPE_"))
    }
    env.update(
        OPENAI_API_KEY="fixture-model-credential",
        OPENAI_MODEL="fixture",
        DEMO_LEAD_MODEL="fixture-lead",
        DEMO_BACKUP_MODEL="fixture-backup",
        DEMO_WORKER_MODEL="fixture-worker",
        OPENAI_BASE_URL=server.url + "v1",
        HARNESS_GH_API=server.url,
        HARNESS_GH_TOKEN="fixture-source-credential",
        HARNESS_NOTICE_URL=server.url + "notice",
    )
    env_file = private / "demo.env"
    env_file.write_text("")
    spec = private / "run.json"
    spec.write_text(
        json.dumps(
            {
                "prompt": PROMPT,
                "write_paths": ["normalize.py", "REPORT.md", "prefix.py", "suffix.py"],
                "episode_model_calls": 1,
                "total_seconds": timeout,
                "request_seconds": 10,
                "output_tokens": 1024,
                "heartbeat_seconds": 0.1,
                "heartbeat_timeout": 15,
                "shutdown_seconds": 2,
                "stages": [
                    {
                        "key": "implement",
                        "subject": "Implement normalization",
                        "acceptance": {
                            "condition": "Protected implementation checks pass",
                            "judge": False,
                            "predicates": [
                                {
                                    "id": "checks",
                                    "type": "tests_passed",
                                    "recipe": "unit-checks",
                                }
                            ],
                        },
                    },
                    {
                        "key": "report",
                        "subject": "Report verified behavior",
                        "depends_on": ["implement"],
                        "acceptance": {
                            "condition": "Report exists",
                            "judge": False,
                            "predicates": [
                                {
                                    "id": "report",
                                    "type": "file_exists",
                                    "path": "REPORT.md",
                                    "min_bytes": 1,
                                }
                            ],
                        },
                    },
                ],
                "goal": {
                    "condition": "Feature verified and reported",
                    "judge": False,
                    "predicates": [
                        {
                            "id": "report",
                            "type": "file_exists",
                            "path": "REPORT.md",
                            "min_bytes": 1,
                        },
                        {
                            "id": "checks",
                            "type": "tests_passed",
                            "recipe": "unit-checks",
                        },
                    ],
                },
            }
        )
    )
    value = json.loads(spec.read_text())
    value.update(
        model_profile="lead",
        fallback_models=["backup"],
        retry_delay=0.01,
        allow_tests=["team-checks", "member-checks"],
        snapshot_limits={"max_files": 16, "max_bytes": 16000, "max_paths": 10000},
        context_chars=80000,
    )
    value["stages"][0]["write_paths"] = ["normalize.py"]
    value["stages"][1]["write_paths"] = ["REPORT.md"]
    value["members"] = []
    for name in ("prefix", "suffix"):
        value["members"].append(
            {
                "name": name,
                "role": "coder",
                "prompt": "Implement add_"
                + name
                + "(value) in "
                + name
                + ".py to add the corresponding square bracket. Submit the owned stage.",
                "model_profile": "worker",
                "retries": 1,
            }
        )
        value["stages"].append(
            {
                "key": name,
                "subject": "Implement " + name,
                "executor": name,
                "isolated": True,
                "write_paths": [name + ".py"],
                "depends_on": ["report"] if name == "prefix" else ["prefix"],
                "inputs": []
                if name == "prefix"
                else [{"stage": "prefix", "paths": ["prefix.py"]}],
                "acceptance": {
                    "condition": "Owned function implemented",
                    "judge": False,
                    "predicates": [
                        {
                            "id": "function",
                            "type": "file_contains",
                            "path": name + ".py",
                            "text": "'[' + value"
                            if name == "prefix"
                            else "add_prefix(value) + ']'",
                        }
                    ],
                },
            }
        )
    next(stage for stage in value["stages"] if stage["key"] == "prefix")["acceptance"][
        "predicates"
    ].append({"id": "member-tests", "type": "tests_passed", "recipe": "member-checks"})
    value["stages"].append(
        {
            "key": "integrate",
            "subject": "Integrate verified outputs",
            "isolated": True,
            "depends_on": ["implement", "report", "prefix", "suffix"],
            "integrates": ["implement", "report", "prefix", "suffix"],
            "write_paths": ["normalize.py", "REPORT.md", "prefix.py", "suffix.py"],
            "publish": True,
            "acceptance": {
                "condition": "Combined protected checks pass",
                "judge": False,
                "predicates": [
                    {"id": "team", "type": "tests_passed", "recipe": "team-checks"}
                ],
            },
        }
    )
    spec.write_text(json.dumps(value))
    models = private / "models.json"
    models.write_text(
        json.dumps(
            {
                "version": 2,
                "connections": {
                    name: {
                        "protocol": "chat_completions",
                        "api_key_env": "OPENAI_API_KEY",
                        "base_url_env": "OPENAI_BASE_URL",
                    }
                    for name in ("lead", "backup", "worker")
                },
                "profiles": {
                    name: {
                        "connection": name,
                        "model_env": "DEMO_" + name.upper() + "_MODEL",
                        "context_chars": 80000,
                        "max_output_tokens": 1024,
                    }
                    for name in ("lead", "backup", "worker")
                },
                "defaults": {"lead": "lead"},
            }
        )
    )
    tests = private / "tests.json"
    tests.write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "unit-checks",
                        "argv": [
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            "import sys,unittest;from pathlib import Path;sys.path.insert(0,str(Path.cwd()));r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests', pattern='test_normalize.py'));raise SystemExit(0 if r.wasSuccessful() else 1)",
                        ],
                        "input_globs": ["normalize.py", "tests/**"],
                        "locked_globs": ["tests/**"],
                        "max_runs": 1,
                    }
                ],
            }
        )
    )
    recipes = json.loads(tests.read_text())
    recipes["recipes"].append({**recipes["recipes"][0], "name": "report-check"})
    recipes["recipes"].append(
        {
            **recipes["recipes"][0],
            "name": "team-checks",
            "argv": [
                *recipes["recipes"][0]["argv"][:-1],
                recipes["recipes"][0]["argv"][-1].replace(
                    "test_normalize.py", "test_team.py"
                ),
            ],
            "input_globs": ["prefix.py", "suffix.py", "tests/test_team.py"],
            "allow_worktrees": True,
            "max_runs": 2,
        }
    )
    recipes["recipes"].append(
        {
            "name": "member-checks",
            "argv": [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,'.');from prefix import add_prefix;assert add_prefix('x')=='[x';print('verified')",
            ],
            "roles": ["teammate"],
            "input_globs": ["prefix.py"],
            "locked_globs": [],
            "max_runs": 1,
        }
    )
    tests.write_text(json.dumps(recipes))
    config = private / "control.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "github": {
                    "repository": "fixture/project",
                    "pull_number": 1,
                    "actor_ids": [7],
                    "app_ids": [11],
                    "api_url_env": "HARNESS_GH_API",
                    "token_env": "HARNESS_GH_TOKEN",
                    "poll_seconds": 0.5,
                },
                "notification": {"url_env": "HARNESS_NOTICE_URL"},
            }
        )
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "homebrew_harness",
        "run",
        "--cwd",
        str(workspace),
        "--env-file",
        str(env_file),
        "--test-config",
        str(tests),
        "--models-config",
        str(models),
        "--control-config",
        str(config),
    ]

    def launch(arguments, name):
        handle = (private / (name + ".log")).open("wb")
        handles.append(handle)
        process = subprocess.Popen(
            [*command, *arguments],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
        )
        processes.append(process)
        return process

    def wait_for(predicate):
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if processes and processes[-1].poll() is not None:
                raise RuntimeError(
                    "Supervisor exited before the expected stage; inspect private logs."
                )
            time.sleep(0.05)
        raise TimeoutError("A-E demo exceeded its deadline; inspect the private logs.")

    try:
        first = launch(["start", "--spec", str(spec)], "first-supervisor")
        run = wait_for(lambda: next(iter(store.runtime.items("runs").values()), None))
        identity = run["id"]
        requests = HumanRequests(store)

        def waiting_request(kind):
            value = store.get(identity)
            if value["status"] != "waiting" or not value["cleanup_complete"]:
                return None
            candidates = [
                r
                for r in requests.records(identity)
                if r["state"] == "pending" and r["kind"] == kind
            ]
            if not candidates:
                return None
            request = candidates[-1]
            delivered = any(
                e["state"] == "delivered"
                and e["payload"].get("request_id") == request["id"]
                for e in store.runtime.items("run_outbox").values()
            )
            return request if delivered else None

        question = wait_for(lambda: waiting_request("attention"))
        first.terminate()
        first.wait(timeout=3)
        answer = subprocess.run(
            [
                *command,
                "answer",
                identity,
                question["id"],
                str(question["revision"]),
                "Return an empty string",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if answer.returncode:
            raise RuntimeError("Demo answer failed: " + answer.stderr)
        answered_from = len(server.messages)
        second = launch(["supervise", identity], "second-supervisor")
        for index in range(4):
            request = wait_for(lambda: waiting_request("tool"))
            if index == 2:
                second.terminate()
                second.wait(timeout=3)
                second = launch(["supervise", identity], "proof-restart-supervisor")
            approved = subprocess.run(
                [
                    *command,
                    "approve",
                    identity,
                    request["id"],
                    str(request["revision"]),
                    "yes",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if approved.returncode:
                raise RuntimeError("Demo approval failed: " + approved.stderr)
            wait_for(
                lambda: requests.get(identity, request["id"])["state"] == "consumed"
            )
        renewal = wait_for(lambda: waiting_request("test_grant"))
        approved = subprocess.run(
            [
                *command,
                "approve",
                identity,
                renewal["id"],
                str(renewal["revision"]),
                "yes",
                "--runs",
                "1",
                "--seconds",
                "120",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if approved.returncode:
            raise RuntimeError("Member test renewal failed: " + approved.stderr)
        wait_for(lambda: requests.get(identity, renewal["id"])["state"] == "consumed")
        wait_for(lambda: second.poll() is not None)
        second.wait(timeout=2)
        final = store.status(identity)
        if (
            final["status"] != "completed"
            or not final["cleanup_complete"]
            or second.returncode
        ):
            raise RuntimeError(
                "A-E demo did not complete; inspect private supervisor logs. "
                + json.dumps(
                    {"status": final["status"], "outcome": final.get("last_outcome")}
                )
            )
        if (
            len(server.messages) > 80
            or (workspace / "normalize.py").read_text() != REVISED
        ):
            raise RuntimeError("A-E demo took an unexpected model/tool path.")
        for index, messages in enumerate(server.messages):
            constraints = [
                m
                for m in messages
                if m.get("name") in {"task_intent", "inherited_task_constraints"}
                and PROMPT in m.get("content", "")
            ]
            if len(constraints) != 1 or PROMPT not in constraints[0]["content"]:
                raise RuntimeError(
                    "Original task was duplicated or lost from host constraints."
                )
            if (
                index >= answered_from
                and "Return an empty string" not in constraints[0]["content"]
            ):
                raise RuntimeError(
                    "The answered requirement was lost after continuation."
                )
            if (
                sum(
                    m.get("role") == "user" and m.get("content") == PROMPT
                    for m in messages
                )
                > 1
            ):
                raise RuntimeError("Original task was submitted more than once.")
        records = requests.records(identity)
        events = {
            key: value
            for key, value in store.runtime.items("run_events").items()
            if value["run_id"] == identity
        }
        tasks = [
            v
            for v in store.runtime.items("tasks").values()
            if (v.get("stage") or {}).get("run_id") == identity
        ]
        submissions = [
            value
            for value in store.runtime.items("stage_submissions").values()
            if value["run_id"] == identity
        ]
        handoffs = [
            value
            for value in store.runtime.items("stage_handoffs").values()
            if value["run_id"] == identity
        ]
        if (
            not server.proof_restart
            or len(handoffs) != 6
            or sum(not v["verification"]["ok"] for v in submissions) != 2
        ):
            raise RuntimeError(
                "Stage recovery or evidence invalidation did not follow the expected path."
            )
        from homebrew_harness.runs.maintenance import (
            write_export,
            backup_run,
            verify_backup,
        )

        write_export(store, identity, base / "run-export.json")
        backup_run(store, identity, base / "backup")
        backup_valid = verify_backup(base / "backup")["valid"]
        member_records = list(store.runtime.items("run_members").values())
        model_handoffs = list(store.runtime.items("run_handoffs").values())
        if len(model_handoffs) != 1 or not any(
            r.get("recoveries") == 1 for r in member_records
        ):
            raise RuntimeError("Expected model/member recovery did not occur.")
        # These Runs share the workspace but use no external source/notification config.
        simple = list(command)
        index = simple.index("--control-config")
        del simple[index : index + 2]

        def simple_launch(arguments, name):
            handle = (private / (name + ".log")).open("wb")
            handles.append(handle)
            process = subprocess.Popen(
                [*simple, *arguments],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
            )
            processes.append(process)
            return process

        def simple_spec(prompt, key, path):
            value = {
                "prompt": prompt,
                "model_profile": "backup",
                "write_paths": [path],
                "total_seconds": 60,
                "request_seconds": 10,
                "shutdown_seconds": 2,
                "heartbeat_seconds": 0.1,
                "heartbeat_timeout": 15,
                "goal": {
                    "condition": "Output exists",
                    "judge": False,
                    "predicates": [{"id": "out", "type": "file_exists", "path": path}],
                },
            }
            value["stages"] = [
                {"key": key, "subject": key, "acceptance": value["goal"]}
            ]
            target = private / (key + ".json")
            target.write_text(json.dumps(value))
            return target

        cancel_spec = simple_spec(CANCEL_PROMPT, "cancel-check", "cancelled-task.txt")
        cancelled = simple_launch(
            ["start", "--spec", str(cancel_spec)], "cancel-supervisor"
        )
        cancel_run = wait_for(
            lambda: next(
                (
                    r
                    for r in store.runtime.items("runs").values()
                    if r["spec"]["prompt"] == CANCEL_PROMPT
                ),
                None,
            )
        )
        wait_for(
            lambda: (
                store.get(cancel_run["id"])["status"] == "waiting"
                and store.get(cancel_run["id"])["cleanup_complete"]
            )
        )
        subprocess.run(
            [*simple, "cancel", cancel_run["id"]],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
        )
        wait_for(lambda: cancelled.poll() is not None)
        cancel_final = store.get(cancel_run["id"])
        cancelled_task = next(
            value
            for value in store.runtime.items("tasks").values()
            if (value.get("stage") or {}).get("run_id") == cancel_run["id"]
        )
        follow_spec = simple_spec(FOLLOWUP_PROMPT, "followup", "followup.txt")
        failed = simple_launch(
            ["start", "--spec", str(follow_spec)], "quota-supervisor"
        )
        wait_for(lambda: failed.poll() is not None)
        follow_run = next(
            r
            for r in store.runtime.items("runs").values()
            if r["spec"]["prompt"] == FOLLOWUP_PROMPT
        )
        if (
            follow_run["status"] != "blocked"
            or follow_run["last_outcome"]["reason_code"] != "provider_quota"
        ):
            raise RuntimeError("Expected same-model quota interruption did not occur")
        resumed = simple_launch(
            ["resume", follow_run["id"]], "same-model-resume-supervisor"
        )
        wait_for(lambda: resumed.poll() is not None)
        follow_final = store.get(follow_run["id"])
        steering = store.runtime.get("input_receipts", server.steering_id)
        if (
            cancelled_task["status"] != "abandoned"
            or not cancel_final["finalized_at"]
            or follow_final["status"] != "completed"
            or not follow_final["finalized_at"]
            or follow_final["session_id"] != follow_run["session_id"]
            or not follow_final.get("resume_applied")
            or not server.dependent_input_seen
            or not (server.cooperative_wait_seen or server.steering_rejection_seen)
            or not steering
            or steering["state"] != "applied"
        ):
            raise RuntimeError(
                "P1 lifecycle, input or cooperative waiting scenario did not verify"
            )
        timeout_recorded = False
        for log in (workspace / ".harness/runs" / identity).glob("*.log"):
            for line in log.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if (
                        isinstance(row, dict)
                        and row.get("reason_code") == "request_timeout"
                    ):
                        timeout_recorded = True
                except ValueError:
                    pass
        if not timeout_recorded:
            raise RuntimeError(
                "The real request timeout was not classified and recovered"
            )
        result = {
            "status": final["status"],
            "request_timeout_verified": timeout_recorded,
            "member_test_renewal_verified": True,
            "large_repository_verified": True,
            "unrelated_files": 5001,
            "dependent_inputs_verified": server.dependent_input_seen,
            "cooperative_input_verified": server.cooperative_wait_seen
            or server.steering_rejection_seen,
            "lead_direction_verified": server.direction_seen,
            "same_model_resume_verified": True,
            "cancelled_task_abandoned": True,
            "new_run_after_cancel": True,
            "terminal_finalized": bool(final["finalized_at"]),
            "tail_model_http_calls": len(server.tail_messages),
            "model_handoffs": len(model_handoffs),
            "member_recoveries": sum(r.get("recoveries", 0) for r in member_records),
            "members": len(member_records),
            "transfers": len(store.runtime.items("run_transfers")),
            "backup_verified": backup_valid,
            "exported": True,
            "cleanup_complete": final["cleanup_complete"],
            "episodes": final["episode"],
            "attempts": final["generation"],
            "supervisor_restart": True,
            "proof_survived_restart": server.proof_restart,
            "stages_completed": sum(v["status"] == "completed" for v in tasks),
            "stage_submissions": len(submissions),
            "rejected_submissions": sum(
                not v["verification"]["ok"] for v in submissions
            ),
            "stage_handoffs": len(handoffs),
            "invalidated_proof_reverified": len(
                [v for v in handoffs if v["stage"]["stage"]["key"] == "implement"]
            )
            == 2,
            "attention_answers": sum(r["state"] == "answered" for r in records),
            "test_approvals_consumed": sum(
                r["state"] == "consumed" and r["kind"] == "tool" for r in records
            ),
            "test_grant_renewals": sum(
                r["state"] == "consumed" and r["kind"] == "test_grant" for r in records
            ),
            "test_attempts": sum(final["test_attempts"].values()),
            "source_applied": sum(e["state"] == "applied" for e in events.values()),
            "old_head_ignored": sum(
                e["ignore_reason"] == "old_head" for e in events.values()
            ),
            "model_http_calls": len(server.messages),
            "notifications": len(server.notices),
            "original_constraints_preserved": True,
            "paid_model_calls": 0,
        }
        (base / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for handle in handles:
            handle.close()
        server.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(
        description="Exercise managed A-E flow with local deterministic services"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    directory = args.output or Path(tempfile.mkdtemp(prefix="harness-ae-demo-"))
    print(json.dumps(run_demo(directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
