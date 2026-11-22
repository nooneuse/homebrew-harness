"""Freeze the model-selected branch without choosing another integration target."""

import hashlib
from pathlib import Path
from homebrew_harness.execution.source_snapshot import snapshot
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.services.maintenance_spec import within
from homebrew_harness.services.protocol import fingerprint
from .integration import materialize


def prepare(git, work, workspace, baseline, directory):
    workspace = Path(workspace)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    current = snapshot(workspace, **work["run_spec"]["snapshot_limits"])
    before = baseline["files"]
    changed = [
        p
        for p in set(current) | set(before)
        if (current.get(p) or {}).get("sha256") != (before.get(p) or {}).get("sha256")
        or (current.get(p) or {}).get("executable")
        != (before.get(p) or {}).get("executable")
    ]
    if any(not within(p, work["run_spec"]["write_paths"]) for p in changed):
        raise ValueError("Work edits exceed the accepted write scope.")
    source_repo = (
        git.run(["rev-parse", "--absolute-git-dir"], cwd=workspace).decode().strip()
    )
    unfinished = (
        git.run(
            ["--git-dir=" + source_repo, "diff", "--name-only", "--diff-filter=U"],
            cwd=workspace,
        )
        .decode()
        .splitlines()
    )
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        location = (
            git.run(["--git-dir=" + source_repo, "rev-parse", "--git-path", name])
            .decode()
            .strip()
        )
        if Path(location).exists() or unfinished:
            raise ValueError(
                "Integration is still active; use its continuation control before final delivery."
            )
    origin = git.head(workspace)
    repo = directory / "repo.git"
    if not repo.exists():
        git.run(["init", "--bare", "--template=", str(repo)])
    git.run(["--git-dir=" + str(repo), "fetch", "--no-tags", source_repo, origin])
    git.run(["--git-dir=" + str(repo), "read-tree", origin])
    origin_files = git.tree(repo, origin)
    for path in sorted(set(origin_files) | set(current)):
        value = current.get(path)
        if value is None:
            git.run(
                ["--git-dir=" + str(repo), "update-index", "--force-remove", "--", path]
            )
            continue
        with SafeFile(workspace, path) as source:
            raw = source.snapshot()[0]
        if raw is None or hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("Source changed during capture.")
        oid = (
            git.run(
                ["--git-dir=" + str(repo), "hash-object", "-w", "--stdin"], data=raw
            )
            .decode()
            .strip()
        )
        git.run(
            [
                "--git-dir=" + str(repo),
                "update-index",
                "--add",
                "--cacheinfo",
                "100755" if value["executable"] else "100644",
                oid,
                path,
            ]
        )
    tree = git.run(["--git-dir=" + str(repo), "write-tree"]).decode().strip()
    identity = {
        "GIT_AUTHOR_NAME": git.values["author_name"],
        "GIT_COMMITTER_NAME": git.values["author_name"],
        "GIT_AUTHOR_EMAIL": git.values["author_email"],
        "GIT_COMMITTER_EMAIL": git.values["author_email"],
    }
    head = origin
    if (
        tree
        != git.run(["--git-dir=" + str(repo), "rev-parse", origin + "^{tree}"])
        .decode()
        .strip()
    ):
        head = (
            git.run(
                [
                    "--git-dir=" + str(repo),
                    "commit-tree",
                    tree,
                    "-p",
                    origin,
                    "-m",
                    "Continue accepted Work",
                ],
                env_extra=identity,
            )
            .decode()
            .strip()
        )
    context = work["continuation"]
    base = context["target_commit"]
    target_files = git.tree(repo, base)
    final = git.tree(repo, head)
    delta = {}
    for path in set(target_files) | set(final):
        if target_files.get(path) == final.get(path):
            continue
        item = final.get(path)
        delta[path] = (
            {"deleted": True}
            if item is None
            else {
                "sha256": hashlib.sha256(git.content(repo, item["oid"])).hexdigest(),
                "executable": item["mode"] == "100755",
            }
        )
    if any(not within(path, work["run_spec"]["write_paths"]) for path in delta):
        raise ValueError("Selected feature exceeds its publication scope.")
    if snapshot(workspace, **work["run_spec"]["snapshot_limits"]) != current:
        raise ValueError("Source changed before freezing.")
    result = materialize(
        git,
        repo,
        head,
        directory,
        work["run_spec"]["snapshot_limits"],
        fingerprint([work["id"], head, base]),
        {"parent": origin, "target_base": base, "delta": delta, "conflicts": []},
    )
    if (context.get("integration") or {}).get("method") == "rebase" and context.get(
        "published_head"
    ):
        result.update(review_rewrite=True, origin_candidate=context["published_head"])
    return result
