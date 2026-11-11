"""Read-only remote evidence and bounded Git content proof for native merges."""

import json
import tempfile
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.platforms.merges import commit
from homebrew_harness.services.protocol import fingerprint


def _git(git, repo, *args, **kwargs):
    return git.run(["--git-dir=" + str(repo), *args], **kwargs)


def _oid(git, repo, value):
    return commit(_git(git, repo, "rev-parse", value).decode().strip())


def _ancestor(git, repo, parent, child):
    return _git(git, repo, "merge-base", parent, child).decode().strip() == parent


def _merge_tree(git, repo, base, head):
    raw = _git(git, repo, "merge-tree", "--write-tree", base, head)
    return commit(raw.decode().splitlines()[0])


def _patch(git, repo, oid):
    return _git(
        git,
        repo,
        "diff-tree",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-commit-id",
        "-p",
        oid + "^",
        oid,
    )


def _patch_id(git, raw):
    value = git.run(["patch-id", "--stable"], data=raw).decode().split()
    return value[0] if value else None


def _rebase_tree(git, repo, base, source, directory):
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        _git(git, repo, "read-tree", base, env_extra=env)
        for oid in source:
            raw = _patch(git, repo, oid)
            if not raw:
                continue
            try:
                _git(
                    git,
                    repo,
                    "apply",
                    "--cached",
                    "--reverse",
                    "--check",
                    "--binary",
                    data=raw,
                    env_extra=env,
                )
            except ValueError:
                _git(
                    git,
                    repo,
                    "apply",
                    "--cached",
                    "--3way",
                    "--binary",
                    "--whitespace=nowarn",
                    data=raw,
                    env_extra=env,
                )
        return commit(_git(git, repo, "write-tree", env_extra=env).decode().strip())


def prove(git, candidate, observed, directory):
    """Return a fixed actual result workspace; never update a remote reference."""
    git.verify(candidate)
    head, result = commit(candidate["commit"]), commit(observed["result_commit"])
    if observed["head"] != head or observed.get("merged") is not True:
        raise ValueError("Merge fact is not for the accepted candidate.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    repo = directory / "repo.git"
    if not repo.exists():
        git.run(["init", "--bare", "--template=", str(repo)])
    _git(
        git,
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        git.values["remote"],
        "refs/heads/" + git.spec["target_branch"],
        credentials=True,
    )
    tip = _oid(git, repo, "FETCH_HEAD")
    _git(
        git,
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        candidate["repo"],
        head,
    )
    if not _ancestor(git, repo, result, tip):
        raise ValueError("Platform merge result is not reachable from the target.")
    tree = _oid(git, repo, result + "^{tree}")
    parents = (
        _git(git, repo, "rev-list", "--parents", "-n", "1", result).decode().split()[1:]
    )
    source = (
        _git(
            git,
            repo,
            "rev-list",
            "--reverse",
            candidate["target_base"] + ".." + head,
            "--max-count=65",
        )
        .decode()
        .split()
    )
    if len(source) > 64:
        raise ValueError("Merge source history exceeds its proof bound.")
    method = observed.get("method")
    proof = None
    base = None
    if result == head:
        proof, base = "fixed_tree", candidate["target_base"]
    elif len(parents) == 2 and parents[1] == head:
        if _merge_tree(git, repo, parents[0], head) == tree:
            proof, base = "merge_tree", parents[0]
    elif len(parents) == 1:
        aggregate = _git(
            git,
            repo,
            "diff-tree",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-commit-id",
            "-p",
            candidate["target_base"],
            head,
        )
        if (
            method != "rebase"
            and _patch_id(git, _patch(git, repo, result)) == _patch_id(git, aggregate)
            and _merge_tree(git, repo, parents[0], head) == tree
        ):
            proof, base = "squash_tree", parents[0]
        if proof is None and method != "squash":
            expected = [_patch_id(git, _patch(git, repo, oid)) for oid in source]
            matches = []
            cursor = result
            actual = []
            for _ in range(len(source)):
                lineage = (
                    _git(git, repo, "rev-list", "--parents", "-n", "1", cursor)
                    .decode()
                    .split()
                )
                if len(lineage) != 2:
                    break
                actual.insert(0, _patch_id(git, _patch(git, repo, cursor)))
                cursor = lineage[1]
                if not _ancestor(git, repo, candidate["target_base"], cursor):
                    continue
                # Patch identities locate the range; full replay proves its content.
                located = iter(expected)
                if not all(
                    any(item == item_expected for item_expected in located)
                    for item in actual
                ):
                    continue
                try:
                    if _rebase_tree(git, repo, cursor, source, directory) == tree:
                        matches.append((cursor, len(actual)))
                except ValueError:
                    continue
            full = [item for item in matches if item[1] == len(expected)]
            if len(full) == 1:
                proof, base = "rebase_replay", full[0][0]
            elif len(matches) == 1:
                proof, base = "rebase_replay", matches[0][0]
    if proof is None:
        raise ValueError("Native merge content proof is incomplete or ambiguous.")
    return materialize(
        git,
        repo,
        result,
        directory,
        candidate["snapshot_limits"],
        fingerprint([head, result, tree, proof, base]),
        {
            "head": head,
            "target_base": base,
            "target_tip": tip,
            "proof": proof,
            "source_commits": source,
        },
    )


def materialize(git, repo, result, directory, limits, identity, information):
    workspace = directory / "workspace"
    record = directory / "manifest.json"
    if record.exists():
        saved = json.loads(record.read_text())
        if saved["identity"] != identity:
            raise ValueError("Private workspace belongs to another fixed result.")
        git.verify(saved)
        return {**saved, **information}
    if workspace.exists():
        import shutil

        if workspace.is_symlink() or not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("Private result cleanup is not safe.")
        shutil.rmtree(workspace)
    workspace.mkdir()
    files = git.tree(repo, result)
    paths = {
        "/".join(name.split("/")[:i])
        for name in files
        for i in range(1, len(name.split("/")) + 1)
    }
    if len(files) > limits["max_files"] or len(paths) > limits["max_paths"]:
        raise ValueError("Integration file inventory exceeds its bound.")
    remaining = limits["max_bytes"]
    for name, item in files.items():
        from homebrew_harness.execution.execution_control import check

        check(cancel_events=(git.stop,))
        raw = _git(git, repo, "cat-file", "blob", item["oid"], max_output=remaining)
        remaining -= len(raw)
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if item["mode"] == "100755" else 0o644)
    _git(git, repo, "update-ref", "refs/heads/result", result)
    _git(git, repo, "symbolic-ref", "HEAD", "refs/heads/result")
    (workspace / ".git").write_text("gitdir: " + str(repo) + "\n")
    frozen = snapshot(workspace, **limits)
    value = {
        "identity": identity,
        "commit": result,
        "tree": _oid(git, repo, result + "^{tree}"),
        "repo": str(repo),
        "workspace": str(workspace),
        "files": frozen,
        "content_hash": content_digest(frozen),
        "snapshot_limits": limits,
        **information,
    }
    record.write_text(json.dumps(value, sort_keys=True))
    return value


def refresh_candidate(git, candidate, observed, directory, *, linear=False):
    """Adopt only the deterministic integration of accepted code and current base."""
    git.verify(candidate)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    repo = directory / "repo.git"
    if not repo.exists():
        git.run(["init", "--bare", "--template=", str(repo)])
    _git(
        git,
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        candidate["repo"],
        candidate["commit"],
    )
    _git(
        git,
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        git.values["remote"],
        "refs/heads/" + git.spec["target_branch"],
        credentials=True,
    )
    base = _oid(git, repo, "FETCH_HEAD")
    _git(
        git,
        repo,
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        git.values["remote"],
        "refs/heads/" + observed["source_branch"],
        credentials=True,
    )
    published = _oid(git, repo, "FETCH_HEAD")
    if published != observed["head"] or base != observed["base"]:
        raise ValueError("PR changed during candidate refresh.")
    tree = _merge_tree(git, repo, base, candidate["commit"])
    cached = directory / "manifest.json"
    prepared = json.loads(cached.read_text()) if cached.exists() else None
    if (
        prepared
        and prepared.get("origin_candidate") == candidate["commit"]
        and prepared["target_base"] == base
        and prepared["parent"] == published
    ):
        git.verify(prepared)
        head = prepared["commit"]
    elif published != candidate["commit"]:
        if _oid(git, repo, published + "^{tree}") != tree or not _ancestor(
            git, repo, base, published
        ):
            raise ValueError(
                "New PR head is not a pure integration of the accepted candidate."
            )
        head = published
    else:
        identity = {
            "GIT_AUTHOR_NAME": git.values["author_name"],
            "GIT_COMMITTER_NAME": git.values["author_name"],
            "GIT_AUTHOR_EMAIL": git.values["author_email"],
            "GIT_COMMITTER_EMAIL": git.values["author_email"],
        }
        parents = ["-p", base] if linear else ["-p", published, "-p", base]
        head = commit(
            _git(
                git,
                repo,
                "commit-tree",
                tree,
                *parents,
                "-m",
                "Integrate accepted work with current target",
                env_extra=identity,
            )
            .decode()
            .strip()
        )
    result = materialize(
        git,
        repo,
        head,
        directory,
        candidate["snapshot_limits"],
        fingerprint([candidate["commit"], base, head]),
        {
            "head": head,
            "target_base": base,
            "parent": published,
            "origin_candidate": candidate["commit"],
            "review_rewrite": linear and published == candidate["commit"],
        },
    )
    before = git.tree(repo, base)
    after = git.tree(repo, head)
    result["delta"] = {
        name: {
            "sha256": result["files"][name]["sha256"],
            "executable": result["files"][name]["executable"],
        }
        if name in after
        else {"deleted": True}
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    }
    if set(result["delta"]) - set(candidate["delta"]):
        raise ValueError("Refreshed integration widens the accepted work delta.")
    return result, published
