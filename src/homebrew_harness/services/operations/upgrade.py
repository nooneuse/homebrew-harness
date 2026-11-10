"""Explicit compatible-version handoff; no hot import or automatic installation."""

import copy
import hashlib
import re
import time
from pathlib import Path

from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.execution.source_snapshot import matches
from homebrew_harness.execution.test_runner import load_test_runner, test_context
from homebrew_harness.security.safe_files import SafeFile, canonical_root
from homebrew_harness.services.protocol import fingerprint, request_key
from .retention import business_fingerprint


def inventory(manager, alias):
    if alias not in manager.spec["upgrade_candidates"]:
        raise ValueError("Select a registered upgrade candidate.")
    spec = manager.spec["upgrade_candidates"][alias]
    root = canonical_root(Path(spec["package"]))
    source = canonical_root(Path(spec["workspace"]))
    entries = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Upgrade package contains a symlink.")
        if not path.is_file() or path.suffix not in {".py", ".md", ".yaml"}:
            continue
        name = path.relative_to(root).as_posix()
        with SafeFile(root, name) as file:
            raw = file.snapshot(max_bytes=32_000_000)[0]
        with SafeFile(source, "src/homebrew_harness/" + name) as file:
            original = file.snapshot(max_bytes=32_000_000)[0]
        if raw != original:
            raise ValueError("Installed candidate differs from verified source.")
        entries[name] = hashlib.sha256(raw).hexdigest()
    source_names = {
        p.relative_to(source / "src/homebrew_harness").as_posix()
        for p in (source / "src/homebrew_harness").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".md", ".yaml"}
        and "__pycache__" not in p.parts
    }
    if set(entries) != source_names or "__init__.py" not in entries:
        raise ValueError("Upgrade package file set is incomplete.")
    with SafeFile(root, "__init__.py") as file:
        header = file.snapshot(max_bytes=100000)[0].decode()
    version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', header, re.M)
    if version is None or version.group(1) != spec["version"]:
        raise ValueError("Candidate version differs from its host declaration.")
    digest = hashlib.sha256()
    for name in sorted(n for n in entries if n.endswith(".py")):
        digest.update(name.encode())
        with SafeFile(root, name) as file:
            digest.update(file.snapshot(max_bytes=32_000_000)[0])
    skills = SkillLoader(root / "services/skills")
    return {
        "runtime": {"version": spec["version"], "fingerprint": digest.hexdigest()},
        "files": entries,
        "skills": fingerprint({k: v.content for k, v in skills.skills.items()}),
        "package_hash": fingerprint(entries),
    }


def validate(manager, alias, *, run=False):
    spec = manager.spec["upgrade_candidates"][alias]
    data = inventory(manager, alias)
    config = manager.runtime.core.config.settings.test_config
    if not config:
        raise ValueError("Upgrade needs a fixed host test registry.")
    runner = load_test_runner(config, Path(spec["workspace"]))
    scope = {
        "session_id": request_key("upgrade-validation", manager.runtime.id, alias),
        "checkpoint": "lead:"
        + request_key("upgrade-validation", manager.runtime.id, alias),
    }
    runner.receipt_scope = lambda: scope
    try:
        if manager.spec["mode"] == "isolated" and runner.backend.name != "container":
            raise ValueError("Upgrade candidate execution requires ContainerBackend.")
        recipe = runner.recipes.get(spec["recipe"])
        if recipe is None:
            raise ValueError("Upgrade recipe is not registered.")
        if any(
            not matches("src/homebrew_harness/" + n, recipe.input_globs)
            for n in data["files"]
        ):
            raise ValueError(
                "Upgrade tests do not cover every installed candidate file."
            )
        if run:
            runner.approve(recipe.name, role="lead", max_runs=1)
            with test_context("lead"):
                receipt = runner.run(recipe.name)
            if receipt["status"] != "passed":
                raise ValueError("Candidate upgrade tests did not pass.")
        proof = runner.verify(recipe.name, Path(spec["workspace"]))
        if not proof["ok"]:
            raise ValueError("Upgrade needs fresh passing candidate evidence.")
        if inventory(manager, alias) != data:
            raise ValueError("Upgrade candidate changed during verification.")
        return {**data, "proof": proof}
    finally:
        runner.close()
        runner.wait_closed(None)


def perform(manager, row):
    meta = manager.store.get(manager.runtime.id)
    if meta.get("operations_phase") != "parked":
        raise ValueError("Park before preparing or authorizing an upgrade.")
    if row["kind"] == "upgrade_prepare":
        alias = row["payload"]["alias"]
        if alias not in manager.spec["upgrade_candidates"]:
            raise ValueError("Unknown upgrade alias.")
        candidate = validate(manager, alias, run=True)
        target = copy.deepcopy(meta["binding"])
        target["runtime"] = candidate["runtime"]
        if "maintenance" in target:
            target["maintenance"]["agent"]["skills"] = candidate["skills"]
        plan = {
            "id": row["id"],
            "alias": alias,
            "old_binding": meta["binding"],
            "new_binding": target,
            "candidate": candidate,
            "business": business_fingerprint(manager),
            "state": "prepared",
            "revision": 0,
            "service_id": manager.runtime.id,
            "checkpoint": "service:" + manager.runtime.id,
        }
        with manager.store.db.write() as conn:
            manager.store.guard(conn, manager.runtime.header)
            manager.store.db._put(conn, "service_upgrade_plans", plan["id"], plan)
        if manager.runtime.maintenance:
            manager.reason(row, "upgrade_assessment", plan["id"])
            business = business_fingerprint(manager)
            with manager.store.db.write() as conn:
                saved = manager.store.db._get(conn, "service_upgrade_plans", plan["id"])
                saved["business"] = business
                manager.store.db._put(conn, "service_upgrade_plans", plan["id"], saved)
        return {
            "plan_id": plan["id"],
            "old_runtime": meta["binding"]["runtime"],
            "new_runtime": candidate["runtime"],
            "package_hash": candidate["package_hash"],
        }
    identity = row["payload"]["id"]
    plan = manager.store.runtime.get("service_upgrade_plans", identity)
    if not plan or plan["service_id"] != manager.runtime.id:
        raise ValueError("Unknown upgrade plan.")
    if row["kind"] == "upgrade_arm":
        if meta["binding"] != plan["old_binding"] or plan["state"] not in {
            "prepared",
            "armed",
        }:
            raise ValueError("Upgrade plan no longer applies to this runtime.")
        observed = validate(manager, plan["alias"])
        if (
            observed != plan["candidate"]
            or business_fingerprint(manager) != plan["business"]
        ):
            raise ValueError("Candidate evidence or business state changed.")
        if manager.runtime.operations.health_snapshot()["unknown_actions"]:
            raise ValueError("Inspect uncertain delivery before version handoff.")
        with manager.store.db.write() as conn:
            current = manager.store.guard(conn, manager.runtime.header)
            plan.update(
                state="armed",
                generation=current["generation"],
                expires_at=time.time() + 3600,
                revision=plan["revision"] + 1,
            )
            manager.store.db._put(conn, "service_upgrade_plans", identity, plan)
            current.update(upgrade_plan=identity, revision=current["revision"] + 1)
            manager.store.db._put(conn, "service_meta", manager.runtime.id, current)
        return {
            "armed": True,
            "plan_id": identity,
            "next": "Stop the service and start the approved installed version; it remains parked.",
        }
    if row["kind"] == "upgrade_confirm":
        if plan["state"] != "probation" or meta["binding"] != plan["new_binding"]:
            raise ValueError("The approved candidate has not entered probation.")
        if manager.runtime.operations.health_snapshot()["issues"]:
            raise ValueError("Candidate service health requires inspection.")
        with manager.store.db.write() as conn:
            manager.store.guard(conn, manager.runtime.header)
            plan.update(
                state="confirmed",
                revision=plan["revision"] + 1,
                confirmed_at=time.time(),
            )
            manager.store.db._put(conn, "service_upgrade_plans", identity, plan)
        return {
            "confirmed": True,
            "plan_id": identity,
            "next": "Explicitly resume service operations.",
        }
    raise ValueError("Unsupported upgrade operation.")


def candidate_binding(store, identity, runtime):
    meta = store.get(identity)
    plan = (
        store.runtime.get("service_upgrade_plans", meta.get("upgrade_plan"))
        if meta.get("upgrade_plan")
        else None
    )
    if plan and plan["state"] == "armed" and plan["new_binding"]["runtime"] == runtime:
        return plan["new_binding"]
    return meta["binding"]
