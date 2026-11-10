"""Actual version-handoff driver; a private interpreter keeps the test suite isolated."""

import json
import sys
import time
from pathlib import Path

from homebrew_harness import Harness, HostConfig, ServiceSpec, RemoteService


def wait(function, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = function()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Upgrade scenario did not reach its expected state.")


def main(config):
    base = Path(config["base"])

    def completed(service, row):
        def check():
            value = service.operation(row["id"])
            if value["state"] == "blocked":
                raise AssertionError(value)
            return value if value["state"] == "completed" else None

        return wait(check)

    with Harness(
        workdir=base / "control",
        config=HostConfig.from_files(
            env_file=Path(config["env"]),
            test_config=base / "registry.json",
            shutdown_seconds=5,
        ),
    ) as host:
        service = host.services.start(
            ServiceSpec(
                str(base / "app"),
                run_defaults={
                    "total_seconds": 30,
                    "request_seconds": 5,
                    "episode_model_calls": 2,
                    "shutdown_seconds": 3,
                },
                operations={
                    "mode": config.get("validation_mode", "local"),
                    "upgrade_candidates": {
                        "candidate": {
                            "workspace": str(base / "candidate"),
                            "package": str(base / "installed/homebrew_harness"),
                            "version": "0.1.1",
                            "recipe": "candidate-tests",
                        }
                    },
                },
            )
        )
        completed(service, service.quiesce())
        plan = completed(service, service.prepare_upgrade("candidate"))
        completed(service, service.arm_upgrade(plan["id"]))
        service.stop()
        if not service.wait_closed(20):
            raise AssertionError("Old version did not close.")
        pth = Path(config["pth"])
        original = pth.read_text()
        pth.write_text(
            "import sys; sys.path.insert(0, " + repr(str(base / "installed")) + ")\n"
        )
        new = None
        try:
            new = host.services.start(identity=service.id, lifetime="detached")
            with RemoteService.from_root(base / "control", control=True) as remote:
                state = remote.query("snapshot")
                if (
                    state["binding"]["runtime"]["version"] != "0.1.1"
                    or remote.health()["operations_phase"] != "parked"
                ):
                    raise AssertionError(
                        "Unapproved runtime or premature business resume."
                    )
                if config["mode"] == "confirm":
                    completed(remote, remote.confirm_upgrade(plan["id"]))
                    completed(remote, remote.resume_operations())
                    work = remote.work.submit(
                        "Reply after the compatible version handoff."
                    )
                    result = work.wait(25)
                    if result.snapshot.state != "review":
                        raise AssertionError(result.data())
                    text = work.result().text
                    remote.stop()
                else:
                    remote.stop()
                    text = None
            wait(lambda: new.snapshot().cleanup["resources_closed"])
            if config["mode"] == "rollback":
                result = host.admin.service_upgrade_rollback(
                    service.ref, plan["id"], request_id="rollback"
                )
                if result["state"] != "completed":
                    raise AssertionError(result)
                pth.write_text(original)
                restored = host.services.start(identity=service.id, lifetime="detached")
                with RemoteService.from_root(base / "control", control=True) as remote:
                    if (
                        remote.query("snapshot")["binding"]["runtime"]["version"]
                        != "0.1.0"
                    ):
                        raise AssertionError(
                            "Old version did not regain its recorded binding."
                        )
                    remote.stop()
                wait(lambda: restored.snapshot().cleanup["resources_closed"])
            return {
                "new_version": "0.1.1",
                "initially_parked": True,
                "mode": config["mode"],
                "text": text,
                "closed": True,
            }
        finally:
            pth.write_text(original)
            if new and not new.snapshot().cleanup["resources_closed"]:
                try:
                    with RemoteService.from_root(
                        base / "control", control=True
                    ) as remote:
                        remote.stop()
                finally:
                    wait(lambda: new.snapshot().cleanup["resources_closed"])


if __name__ == "__main__":
    print(json.dumps(main(json.loads(Path(sys.argv[1]).read_text()))))
