"""P4 extension of the shared local Git/model service demonstration."""

import json
import shutil
import time
from pathlib import Path

import homebrew_harness


def wait(function, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = function()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError(
        "Service operations demonstration did not reach its expected state."
    )


def completed(service, row):
    def done():
        value = service.operation(row["id"])
        if value["state"] == "blocked":
            raise RuntimeError(str(value))
        return value if value["state"] == "completed" else None

    return wait(done)


def configure(directory, registry, git):
    backups = directory / "backups"
    backups.mkdir(mode=0o700)
    source = directory / "runtime-candidate"
    pkg = source / "src/homebrew_harness"
    shutil.copytree(
        Path(homebrew_harness.__file__).parent,
        pkg,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    init = pkg / "__init__.py"
    init.write_text(
        init.read_text().replace('__version__ = "0.1.0"', '__version__ = "0.1.1"')
    )
    installed = directory / "installed/homebrew_harness"
    shutil.copytree(pkg, installed)
    (source / "tests").mkdir()
    (source / "tests/__init__.py").write_text("")
    (source / "tests/test_contract.py").write_text(
        'import unittest,sys,os\nsys.path.insert(0,os.path.abspath("src"))\nfrom homebrew_harness.services.protocol import METHODS\nclass Contracts(unittest.TestCase):\n def test_control_operations_remain_available(self):\n  self.assertTrue({"ops_health","ops_quiesce","ops_read"}<=set(METHODS))\n'
    )
    data = json.loads(registry.read_text())
    data["recipes"].append(
        {
            "name": "upgrade-tests",
            "builtin": "unittest",
            "description": "Installed runtime control contract",
            "roles": ["lead"],
            "input_globs": ["src/**", "tests/**"],
            "locked_globs": ["tests/**"],
            "authorization": "allow",
            "timeout": 20,
        }
    )
    registry.write_text(json.dumps(data))
    return {
        "mode": "local",
        "git_executable": git,
        "backup_directory": str(backups),
        "upgrade_candidates": {
            "candidate": {
                "workspace": str(source),
                "package": str(installed),
                "version": "0.1.1",
                "recipe": "upgrade-tests",
            }
        },
    }


def exercise(service, peer, first_work, directory):
    wait(
        lambda: (
            (
                service.work.get(first_work.id).snapshot().details.get("workspace_sync")
                or {}
            ).get("state")
            == "synchronized"
        )
    )
    peer.audit_findings = [
        {
            "severity": "blocking",
            "path": "src/value.txt",
            "message": "Provide value 3 for the next requested behavior, preserving the public file and fixed tests.",
        }
    ]
    peer.coding_value = "3\n"
    audit = completed(service, service.audit(request_id="audit-new-behavior"))
    wait(
        lambda: next(
            (
                a
                for a in service.records("audits")["items"]
                if a["id"] == audit["result"]["audit_id"] and a["state"] == "completed"
            ),
            None,
        )
    )
    target = wait(
        lambda: next(
            (r for r in service.requests() if r.kind == "delivery_authority"), None
        )
    )
    service.decide(target, "approve", decision_id="audit-publication")
    wait(
        lambda: (
            len([w for w in service.work.list()["items"] if w["state"] == "delivered"])
            == 2
        )
    )
    peer.audit_findings = []
    checked = completed(service, service.audit(force=True, request_id="audit-current"))
    wait(
        lambda: next(
            (
                a
                for a in service.records("audits")["items"]
                if a["id"] == checked["result"]["audit_id"]
                and a["state"] == "completed"
            ),
            None,
        )
    )
    before = len(peer.model_requests)
    skipped = completed(service, service.audit(request_id="audit-unchanged"))
    if not skipped["result"]["skipped"] or len(peer.model_requests) != before:
        raise RuntimeError("Unchanged audit made redundant model calls.")
    discussion = service.converse(
        "Summarize the two iterations and their remaining operator decisions.",
        request_id="iteration-discussion",
    )
    wait(lambda: service.conversation(discussion["id"]).get("result"))
    completed(service, service.quiesce(request_id="operations-park"))
    backup = completed(service, service.backup(request_id="operations-backup"))
    completed(
        service, service.verify_backup(backup["id"], request_id="operations-verify")
    )
    preview = completed(
        service, service.retention_preview(request_id="operations-retention")
    )
    completed(
        service, service.retention_apply(preview["id"], request_id="operations-prune")
    )
    completed(
        service, service.restore_backup(backup["id"], request_id="operations-restore")
    )
    upgrade = completed(
        service,
        service.prepare_upgrade("candidate", request_id="operations-upgrade-plan"),
    )
    completed(
        service, service.arm_upgrade(upgrade["id"], request_id="operations-upgrade-arm")
    )
    return {
        "p4_second_work_delivered": True,
        "p4_audit_unchanged_skipped": True,
        "p4_idle_audit_model_calls": 0,
        "p4_discussion_recorded": True,
        "p4_parked_http_live": service.health()["live"],
        "p4_backup_verified": True,
        "p4_backup_restored": True,
        "p4_retention_applied": True,
        "p4_upgrade_prepared": True,
        "p4_upgrade_armed": True,
        "p4_upgrade_plan_id": upgrade["id"],
        "p4_new_runtime_activation": "Covered by separate actual-process upgrade tests.",
        "p4_output": (directory / "project/src/value.txt").read_text(),
    }
