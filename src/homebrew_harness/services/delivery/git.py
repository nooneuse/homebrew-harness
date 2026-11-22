"""Controlled Git object creation and ancestry-checked ref CAS; no repository hooks."""

import base64
import hashlib
import os
import subprocess
import tempfile
import time
import threading
from pathlib import Path

from homebrew_harness.execution.execution_control import check, operation_deadline
from homebrew_harness.execution.execution_leases import ExecutionLeases
from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.services.maintenance_spec import within
from homebrew_harness.runtime.limits import current_limits


class StaleDelivery(ValueError):
    pass


class GitBackend:
    def __init__(self, root, spec, values):
        self.root, self.spec, self.values = Path(root), spec, values
        from homebrew_harness.execution.test_runner import executable_identity

        self.executable = executable_identity(spec["git_executable"])
        self.leases = ExecutionLeases(self.root)
        self.processes = set()
        self.stop = threading.Event()

    def env(self, *, credentials=False):
        env = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        if credentials and self.values.get("git_token"):
            user = self.values.get("git_user")
            if not user:
                raise ValueError("HTTPS Git credentials require a host user value.")
            header = base64.b64encode(
                (user + ":" + self.values["git_token"]).encode()
            ).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0="Authorization: Basic " + header,
            )
        if credentials and self.values.get("ca_file"):
            env["GIT_SSL_CAINFO"] = self.values["ca_file"]
        return env

    def run(
        self,
        args,
        *,
        cwd=None,
        data=None,
        credentials=False,
        deadline=None,
        max_output=64_000_000,
        env_extra=None,
        accepted_codes=(0,),
    ):
        from homebrew_harness.execution.test_runner import executable_identity

        if executable_identity(self.spec["git_executable"]) != self.executable:
            raise ValueError("Delivery Git executable changed.")
        deadline = deadline or operation_deadline(self.spec["request_seconds"])
        check(deadline, (self.stop,))
        argv = [
            self.spec["git_executable"],
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "commit.gpgsign=false",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=always",
            *args,
        ]
        with (
            self.leases.use("delivery-git"),
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
            tempfile.TemporaryFile() as stdin,
        ):
            if data is not None:
                for offset in range(0, len(data), 1024 * 1024):
                    check(deadline, (self.stop,))
                    stdin.write(data[offset : offset + 1024 * 1024])
                stdin.seek(0)
            process = subprocess.Popen(
                argv,
                cwd=cwd or self.root,
                env={**self.env(credentials=credentials), **(env_extra or {})},
                stdin=stdin if data is not None else subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=(self.leases.descriptor("delivery-git"),),
            )
            self.processes.add(process)
            try:
                while True:
                    check(deadline, (self.stop,))
                    try:
                        process.communicate(
                            timeout=min(0.05, max(0.001, deadline.remaining()))
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                from homebrew_harness.execution.background import ShellRunner

                ShellRunner._stop(process, time.monotonic() + 1)
                raise
            finally:
                if process.poll() is not None:
                    self.processes.discard(process)
            stdout.seek(0)
            out = stdout.read(max_output + 1)
            stderr.seek(0)
            stderr.read(65536)
            if len(out) > max_output:
                raise ValueError("Git output exceeds its bound.")
            if process.returncode not in accepted_codes:
                raise ValueError(
                    "Controlled Git operation failed (" + str(process.returncode) + ")."
                )
            return out

    def head(self, workspace):
        return self.run(["rev-parse", "HEAD"], cwd=workspace).decode().strip()

    def ref(self, branch):
        raw = (
            self.run(
                ["ls-remote", "--refs", self.values["remote"], "refs/heads/" + branch],
                credentials=True,
            )
            .decode()
            .strip()
        )
        if not raw:
            return None
        rows = raw.splitlines()
        if len(rows) != 1:
            raise ValueError("Git target reference is ambiguous.")
        return rows[0].split()[0]

    def tree(self, repo, oid):
        raw = self.run(["--git-dir=" + str(repo), "ls-tree", "-rz", "--full-tree", oid])
        files = {}
        for row in raw.split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            mode, kind, blob = meta.decode().split()
            path = name.decode()
            if mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError("Delivery candidates require regular Git files.")
            from homebrew_harness.security.file_visibility import (
                model_parts,
                host_managed_parts,
            )

            _, parts = model_parts(self.root, path)
            if host_managed_parts(parts):
                raise ValueError("Candidate includes private runtime paths.")
            files[path] = {"mode": mode, "oid": blob}
        return files

    def proposed_delta(self, candidate):
        """The complete proposed change, including changes before a CI repair."""
        self.verify(candidate)
        base = self.tree(candidate["repo"], candidate["target_base"])
        head = self.tree(candidate["repo"], candidate["commit"])
        return {
            path: (
                {"deleted": True} if path not in head else
                {key: candidate["files"][path][key] for key in ("sha256", "executable")}
            )
            for path in sorted(set(base) | set(head))
            if base.get(path) != head.get(path)
        }

    def content(self, repo, oid):
        return self.run(["--git-dir=" + str(repo), "cat-file", "blob", oid])

    def prepare(self, work, workspace, baseline, directory, *, previous=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        repo = directory / "repo.git"
        target = directory / "workspace"
        if repo.exists():
            raise ValueError("Inspect an existing candidate before preparing it again.")
        current = snapshot(workspace, **work["run_spec"]["snapshot_limits"])
        before = baseline["files"]
        input_changes = {
            name
            for name in set(before) | set(current)
            if (before.get(name) or {}).get("sha256")
            != (current.get(name) or {}).get("sha256")
            or (before.get(name) or {}).get("executable")
            != (current.get(name) or {}).get("executable")
        }
        if any(
            not within(name, work["run_spec"]["write_paths"]) for name in input_changes
        ):
            raise ValueError("Work changes exceed its approved paths.")
        if previous:
            base = previous["commit"]
        elif self.spec.get("base_update", "manual") == "manual" and baseline.get("head"):
            base = baseline["head"]
        else:
            base = self.ref(self.spec["target_branch"])
        if not base:
            raise ValueError("Delivery target branch is missing.")
        self.run(["init", "--bare", "--template=", str(repo)])
        source = previous["repo"] if previous else self.values["remote"]
        self.run(
            [
                "--git-dir=" + str(repo),
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                source,
                base,
            ],
            credentials=not previous,
        )
        origin = previous["commit"] if previous else baseline.get("head") or base
        if origin != base:
            self.run(
                [
                    "--git-dir=" + str(repo),
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    self.values["remote"],
                    origin,
                ],
                credentials=True,
            )
        files = self.tree(repo, origin)
        # Original working input may include explicitly proposed source drafts.
        # Only the approved paths contribute; unmodified outside-scope input is
        # evidence, not an implicit publication grant.
        changes = {}
        initial_dirty = []
        for name in set(files) | set(current):
            if name not in input_changes and name not in work.get(
                "included_inputs", []
            ):
                continue
            old = files.get(name)
            old_data = self.content(repo, old["oid"]) if old else None
            old_hash = (
                hashlib.sha256(old_data).hexdigest() if old_data is not None else None
            )
            proposed = current.get(name)
            if (
                not baseline.get("head")
                and name in input_changes
                and old_hash != (before.get(name) or {}).get("sha256")
                and old_hash != (proposed or {}).get("sha256")
            ):
                raise StaleDelivery(
                    "Candidate baseline differs from the work input without an original Git base: "
                    + name
                )
            if old_hash == (proposed or {}).get("sha256") and (
                old is None or (old["mode"] == "100755") == bool(proposed["executable"])
            ):
                continue
            if not within(name, work["run_spec"]["write_paths"]):
                continue
            changes[name] = proposed
            if old_hash != (before.get(name) or {}).get("sha256"):
                initial_dirty.append(name)
        if not changes:
            raise ValueError("Work produced no candidate changes.")
        index = directory / "index"
        extra = {"GIT_INDEX_FILE": str(index)}
        self.run(["--git-dir=" + str(repo), "read-tree", origin], env_extra=extra)
        delta = {}
        for name, metadata in sorted(changes.items()):
            actual = files.get(name)
            expected = before.get(name)
            if metadata is None:
                self.run(
                    [
                        "--git-dir=" + str(repo),
                        "update-index",
                        "--force-remove",
                        "--",
                        name,
                    ],
                    env_extra=extra,
                )
                delta[name] = {"deleted": True}
                continue
            with (
                SafeFile(Path(workspace), name) as file,
                target_file_read(file) as data,
            ):
                if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                    raise StaleDelivery("Work changed during candidate capture.")
                blob = (
                    self.run(
                        ["--git-dir=" + str(repo), "hash-object", "-w", "--stdin"],
                        data=data,
                    )
                    .decode()
                    .strip()
                )
            mode = "100755" if metadata["executable"] else "100644"
            self.run(
                [
                    "--git-dir=" + str(repo),
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    mode,
                    blob,
                    name,
                ],
                env_extra=extra,
            )
            delta[name] = {
                "sha256": metadata["sha256"],
                "executable": metadata["executable"],
            }
        if snapshot(workspace, **work["run_spec"]["snapshot_limits"]) != current:
            raise StaleDelivery("Workspace changed during candidate preparation.")
        tree = (
            self.run(["--git-dir=" + str(repo), "write-tree"], env_extra=extra)
            .decode()
            .strip()
        )
        identity = {
            "GIT_AUTHOR_NAME": self.values["author_name"],
            "GIT_AUTHOR_EMAIL": self.values["author_email"],
            "GIT_COMMITTER_NAME": self.values["author_name"],
            "GIT_COMMITTER_EMAIL": self.values["author_email"],
        }
        if origin != base:
            proposal = (
                self.run(
                    [
                        "--git-dir=" + str(repo),
                        "commit-tree",
                        tree,
                        "-p",
                        origin,
                        "-m",
                        "Proposed work " + work["id"],
                    ],
                    env_extra=identity,
                )
                .decode()
                .strip()
            )
            merged = self.run(
                [
                    "--git-dir=" + str(repo),
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "-z",
                    base,
                    proposal,
                ],
                accepted_codes=(0, 1),
            ).split(b"\0")
            tree = merged[0].decode().strip()
            conflicts = []
            for path in merged[1:]:
                if not path:
                    break
                conflicts.append(path.decode())
        else:
            proposal, conflicts = None, []
        # Delta is relative to the observed target, so already published host
        # repairs are not re-described as new feature work.
        final_files = self.tree(repo, tree)
        target_files = self.tree(repo, base)
        delta = {}
        for name in set(final_files) | set(target_files):
            if final_files.get(name) == target_files.get(name):
                continue
            meta = final_files.get(name)
            delta[name] = (
                {"deleted": True}
                if meta is None
                else {
                    "sha256": hashlib.sha256(
                        self.content(repo, meta["oid"])
                    ).hexdigest(),
                    "executable": meta["mode"] == "100755",
                }
            )
        if any(not within(name, work["run_spec"]["write_paths"]) for name in delta):
            raise ValueError(
                "Aligned candidate changes exceed the Work approved paths."
            )
        commit = (
            self.run(
                [
                    "--git-dir=" + str(repo),
                    "commit-tree",
                    tree,
                    "-p",
                    base,
                    "-m",
                    "Maintain work " + work["id"],
                ],
                env_extra=identity,
            )
            .decode()
            .strip()
        )
        self.run(
            ["--git-dir=" + str(repo), "update-ref", "refs/heads/candidate", commit]
        )
        self.run(
            ["--git-dir=" + str(repo), "symbolic-ref", "HEAD", "refs/heads/candidate"]
        )
        target.mkdir()
        for name, meta in self.tree(repo, commit).items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.content(repo, meta["oid"]))
            destination.chmod(0o755 if meta["mode"] == "100755" else 0o644)
        (target / ".git").write_text("gitdir: " + str(repo) + "\n")
        self.run(["--git-dir=" + str(repo), "config", "core.bare", "false"])
        self.run(["--git-dir=" + str(repo), "config", "core.worktree", str(target)])
        self.run(["--git-dir=" + str(repo), "read-tree", commit])
        frozen = snapshot(target, **work["run_spec"]["snapshot_limits"])
        return {
            "repo": str(repo),
            "workspace": str(target),
            "commit": commit,
            "tree": tree,
            "parent": base,
            "target_base": previous["target_base"] if previous else base,
            "files": frozen,
            "content_hash": content_digest(frozen),
            "delta": delta,
            "original_git_base": origin,
            "proposal_commit": proposal,
            "conflicts": conflicts,
            "initial_input_changes": sorted(initial_dirty),
            "snapshot_limits": work["run_spec"]["snapshot_limits"],
            **({"review_rewrite": True, "origin_candidate": previous["origin_candidate"]}
               if previous and previous.get("review_rewrite") else {}),
        }

    def verify(self, candidate):
        if (
            self.head(candidate["workspace"]) != candidate["commit"]
            or snapshot(Path(candidate["workspace"]), **candidate["snapshot_limits"])
            != candidate["files"]
        ):
            raise StaleDelivery("Frozen candidate changed.")

    def publish(self, candidate, branch, expected):
        if branch == self.spec["target_branch"] or not branch.startswith(
            self.spec["review_prefix"]
        ):
            raise ValueError(
                "Delivery Git publication is restricted to review branches."
            )
        self.verify(candidate)
        actual = self.ref(branch)
        if actual == candidate["commit"]:
            return {"confirmed": True, "ref": branch, "commit": actual}
        if actual != expected:
            raise StaleDelivery("Remote target changed before delivery.")
        if expected and not candidate.get("review_rewrite"):
            self.run(
                [
                    "--git-dir=" + candidate["repo"],
                    "merge-base",
                    "--is-ancestor",
                    expected,
                    candidate["commit"],
                ]
            )
        if (
            candidate.get("review_rewrite")
            and candidate.get("origin_candidate") != expected
        ):
            raise StaleDelivery(
                "Review rewrite is not bound to its accepted previous head."
            )
        lease = "--force-with-lease=refs/heads/" + branch + ":" + (expected or "")
        self.run(
            [
                "--git-dir=" + candidate["repo"],
                "push",
                "--porcelain",
                lease,
                self.values["remote"],
                candidate["commit"] + ":refs/heads/" + branch,
            ],
            credentials=True,
        )
        if self.ref(branch) != candidate["commit"]:
            raise RuntimeError("Git acknowledgment does not confirm the candidate.")
        return {"confirmed": True, "ref": branch, "commit": candidate["commit"]}

    def close(self):
        from homebrew_harness.execution.background import ShellRunner

        self.stop.set()
        for process in list(self.processes):
            ShellRunner._stop(process, time.monotonic() + 1)
            if process.poll() is None:
                raise RuntimeError("Delivery Git process still owns its lease.")
            self.processes.discard(process)
        self.leases.close()


from contextlib import contextmanager


@contextmanager
def target_file_read(file):
    with file.open_read() as stream:
        data = stream.read(current_limits().candidate_file_bytes + 1)
    file.verify_binding()
    if len(data) > current_limits().candidate_file_bytes:
        raise ValueError("Candidate file exceeds its byte limit.")
    yield data
