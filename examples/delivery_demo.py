"""Offline P3 scenario with real Git refs, CI repair, restart and HTTP approval."""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from homebrew_harness import Harness, HostConfig, RemoteService, ServiceSpec
from homebrew_harness.platforms.contracts import ObjectRef
from delivery_fixture import Peer


def wait_for(function, seconds=45):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = function()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("The demonstration did not reach its expected state.")


def run_demo(directory, *, operations_demo=False):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    workspace, control = directory / "project", directory / "control"
    workspace.mkdir()
    control.mkdir(mode=0o700)
    git = shutil.which("git")
    if not git:
        raise ValueError("The demonstration requires Git.")

    def command(*args, cwd=workspace):
        return (
            subprocess.check_output(
                [
                    git,
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=" + os.devnull,
                    *args,
                ],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    (workspace / "tests/__init__.py").write_text("")
    (workspace / "src/value.txt").write_text("0\n")
    (workspace / "tests/test_value.py").write_text(
        "import unittest\nfrom pathlib import Path\nclass Value(unittest.TestCase):\n"
        '    def test_value(self):\n        self.assertGreaterEqual(int(Path("src/value.txt").read_text()),1)\n'
    )
    command("init", "-q")
    command("add", "src", "tests")
    command("commit", "-qm", "baseline")
    command("branch", "-M", "main")
    remote_repo = directory / "remote.git"
    command("clone", "--bare", str(workspace), str(remote_repo))
    peer = Peer(git, remote_repo)
    peer.ci_failure = True
    env_file = directory / "model.env"
    env_file.write_text(
        "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
        + peer.url
        + "/v1\n"
        + "REMOTE="
        + str(remote_repo)
        + "\nAPI="
        + peer.url
        + "\nTOKEN=synthetic\n"
        + "AUTHOR=fixture\nEMAIL=fixture@invalid\n"
    )
    env_file.chmod(0o600)
    tests = directory / "tests.json"
    tests.write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "unit-tests",
                        "builtin": "unittest",
                        "description": "Fixed regression",
                        "roles": ["lead"],
                        "input_globs": ["src/**", "tests/**"],
                        "locked_globs": ["tests/**"],
                        "authorization": "allow",
                        "timeout": 10,
                        "max_runs": 10,
                    }
                ],
            }
        )
    )
    operation_options = {}
    if operations_demo:
        from service_operations_fixture import configure

        operation_options = configure(directory, tests, git)
    spec = ServiceSpec(
        str(workspace),
        profile="repository_maintenance",
        run_defaults={
            "total_seconds": 60,
            "request_seconds": 5,
            "episode_model_calls": 4,
            "write_paths": ["src"],
            "tools": ["read_file", "write_file"],
            "shutdown_seconds": 3,
        },
        maintenance={
            "scopes": {
                "code": {"write_paths": ["src"], "tools": ["read_file", "write_file"]}
            },
            "auto_categories": ["bug"],
            "job_seconds": 20,
            "report_seconds": 3600,
        },
        operations=operation_options,
        delivery={
            "mode": "local",
            "git_executable": git,
            "provider": "github",
            "instance": "primary",
            "project": "example/project",
            "remote_env": "REMOTE",
            "api_url_env": "API",
            "api_token_env": "TOKEN",
            "author_name_env": "AUTHOR",
            "author_email_env": "EMAIL",
            "required_tests": ["unit-tests"],
            "required_checks": ["CI"],
            "check_app_ids": [11],
            "ci_poll_seconds": 0.1,
            "auto_actions": ["publish_branch", "ensure_change_request"],
            "auto_ci_fix": True,
        },
    )
    saved = dict(os.environ)
    os.environ.clear()
    service = None
    try:
        with Harness(
            workdir=control,
            config=HostConfig.from_files(env_file=env_file, test_config=tests),
        ) as host:
            service = host.services.start(spec)
            with RemoteService.from_root(control, control=True) as client:
                work = client.work.submit(
                    "Fix the value regression without changing the fixed tests.",
                    request_id="regression",
                )
                target = wait_for(
                    lambda: next(
                        (
                            r
                            for r in client.requests()
                            if r.kind == "delivery_authority"
                        ),
                        None,
                    )
                )
                candidate = client.delivery(work.snapshot().details["changeset_id"])
                client.stop(request_id="restart")
            if not service.wait_closed(15):
                raise RuntimeError("Service resources did not close.")
            service = host.services.start(identity=service.id)
            with RemoteService.from_root(control, control=True) as client:
                restored = next(
                    r for r in client.requests() if r.ref.id == target.ref.id
                )
                batch = client.batch([restored], request_id="publication-batch")
                receipt = client.decide_batch(
                    batch["id"],
                    [{"id": restored.ref.id, "decision": "approve"}],
                    request_id="publication-decision",
                )
                wait_for(
                    lambda: client.work.get(work.id).snapshot().state == "delivered"
                )
                action = client.plan_delivery(
                    candidate["id"],
                    "post_comment",
                    body="The fixed candidate passed local and CI checks and was delivered.",
                    subject=ObjectRef(
                        "github", "primary", "example/project", "issue", "1", 1
                    ).data(),
                    request_id="comment-plan",
                )
                request = next(
                    r for r in client.requests() if r.arguments["scope"] == "delivery"
                )
                client.decide(request, "approve", decision_id="comment")
                wait_for(
                    lambda: (
                        client.delivery(action["id"], kind="actions")["state"]
                        == "confirmed"
                    )
                )
                result = {
                    "delivered": True,
                    "ci_repairs": client.work.get(work.id)
                    .snapshot()
                    .details["ci_fix_attempts"],
                    "same_target_request_after_restart": restored.ref.id
                    == target.ref.id,
                    "group_decision_confirmed": receipt["results"][restored.ref.id][
                        "state"
                    ]
                    == "confirmed",
                    "target_matches_candidate": command(
                        "--git-dir=" + str(remote_repo), "rev-parse", "main"
                    )
                    == candidate["candidate"]["commit"],
                    "one_change_request": len(peer.pulls) == 1,
                    "public_comment_confirmed": len(peer.comments) == 1,
                    "fixed_tests_passed": len(candidate["quality"]),
                    "paid_model_calls": 0,
                }
                if operations_demo:
                    from service_operations_fixture import exercise

                    result.update(exercise(client, peer, work, directory))
                client.stop(request_id="finish")
            result["resources_closed"] = bool(service.wait_closed(15))
            if operations_demo:
                rollback = host.admin.service_upgrade_rollback(
                    service.ref,
                    result["p4_upgrade_plan_id"],
                    request_id="operations-rollback",
                )
                result["p4_upgrade_rollback"] = rollback["state"] == "completed"
            if not all(
                result[k]
                for k in (
                    "delivered",
                    "same_target_request_after_restart",
                    "group_decision_confirmed",
                    "target_matches_candidate",
                    "one_change_request",
                    "public_comment_confirmed",
                    "resources_closed",
                )
            ):
                raise RuntimeError("The demonstration lacks confirmed delivery facts.")
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        if service:
            service.close()
            service.wait_closed(15)
        peer.close()
        os.environ.update(saved)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run_demo(parser.parse_args().output), indent=2))
