"""Owned source views over existing Work, candidate and evidence records."""

import difflib
import hashlib
import json
from pathlib import Path

from homebrew_harness.execution.source_snapshot import snapshot, content_digest
from homebrew_harness.security.safe_files import SafeFile
from homebrew_harness.security.file_visibility import assert_visible, host_managed_parts
from homebrew_harness.runtime.limits import current_limits
from .protocol import fingerprint, request_key


class WorkSources:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.maintenance.store
        self.git = runtime.maintenance.delivery.git

    def work(self, identity):
        return self.runtime.store.work(identity, self.runtime.id)

    def rows(self, work):
        rows = []
        limits = (work.get("run_spec") or {}).get(
            "snapshot_limits"
        ) or self.runtime.spec.run_spec("Inspect source").snapshot_limits
        root = Path(work.get("run_workspace") or self.runtime.spec.workspace)
        target_base = (work.get("continuation") or {}).get("target_commit")
        if not target_base and work.get("changeset_id"):
            candidate = (
                self.store.get("changesets", work["changeset_id"]).get("candidate")
                or {}
            )
            target_base = candidate.get("target_base")
        target_base = target_base or (work.get("baseline") or {}).get("head")
        rows.append(
            {
                "target_base": target_base,
                "source_ref": "draft:" + work["id"],
                "role": "current_draft",
                "workspace": str(root),
                "commit": self.git.head(root),
                "mutable": True,
                "snapshot_limits": limits,
            }
        )
        with self.store.db.connection() as conn:
            candidates = conn.execute(
                "SELECT value FROM records WHERE section='service_changesets' AND owner=? "
                "AND json_extract(value,'$.work_id')=? ORDER BY rowid",
                ("service:" + self.runtime.id, work["id"]),
            ).fetchall()
            evidence = conn.execute(
                "SELECT value FROM records WHERE section='service_evidence' AND owner=? "
                "AND json_extract(value,'$.work_id')=? AND json_extract(value,'$.kind')='work_source' ORDER BY rowid",
                ("service:" + self.runtime.id, work["id"]),
            ).fetchall()
        for (raw,) in candidates:
            row = json.loads(raw)
            versions = [(row.get("candidate"), "candidate")]
            versions += [
                (v, "historical_candidate") for v in row.get("candidate_history", [])
            ]
            for candidate, role in versions:
                if (
                    not candidate
                    or not candidate.get("workspace")
                    or not candidate.get("commit")
                ):
                    continue
                ref = "candidate:" + row["id"] + ":" + candidate["commit"]
                if any(v["source_ref"] == ref for v in rows):
                    continue
                rows.append(
                    {
                        **candidate,
                        "source_ref": ref,
                        "role": role,
                        "mutable": False,
                        "snapshot_limits": candidate.get("snapshot_limits") or limits,
                    }
                )
        rows += [
            {
                **json.loads(raw)["source"],
                "source_ref": json.loads(raw)["id"],
                "role": json.loads(raw).get("source_role", "preserved_draft"),
                "mutable": False,
            }
            for raw, in evidence
        ]
        return rows

    def describe(self, identity):
        work = self.work(identity)
        rows = self.rows(work)
        values = [
            {
                k: row.get(k)
                for k in (
                    "source_ref",
                    "role",
                    "commit",
                    "content_hash",
                    "target_base",
                    "mutable",
                )
            }
            for row in rows
        ]
        return {
            "work_id": identity,
            "work_revision": work["revision"],
            "requirements_revision": work["requirements_revision"],
            "sources": values,
            "current_run": work.get("run_id"),
            "pause_requested": work.get("pause_requested"),
            "published_head": self._published(work),
            "target_branch": self.runtime.spec.delivery["target_branch"],
            "instruction": "Source roles are facts. Choose the draft or a recorded candidate explicitly; target movement does not replace your source.",
        }

    def _published(self, work):
        if not work.get("changeset_id"):
            return None
        row = self.store.get("changesets", work["changeset_id"])
        return (row.get("observed") or {}).get("head") or row.get("review_commit")

    def resolve(self, identity, reference):
        work = self.work(identity)
        row = next((v for v in self.rows(work) if v["source_ref"] == reference), None)
        if row is None:
            raise ValueError(
                "Source reference is outside this Work; query its source directory."
            )
        root = Path(row["workspace"])
        if root.is_symlink() or not root.is_dir():
            raise ValueError(
                "Recorded source is unavailable; the original record is retained."
            )
        if not row["mutable"]:
            actual = snapshot(root, **row["snapshot_limits"])
            if (
                row.get("content_hash")
                and content_digest(actual) != row["content_hash"]
            ):
                raise ValueError("Recorded source content changed.")
        return row

    def read(
        self, identity, reference, path, offset=0, limit=10000, expected_version=None
    ):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= current_limits().operation_read_chars
        ):
            raise ValueError(
                "Source read requires a nonnegative character offset and configured page limit."
            )
        row = self.resolve(identity, reference)
        root = Path(row["workspace"])
        assert_visible(root, path)
        if host_managed_parts(Path(path).parts):
            raise ValueError(
                "Source reads cannot access runtime or credential directories."
            )
        with SafeFile(root, path) as source:
            raw = source.snapshot(max_bytes=current_limits().candidate_file_bytes)[0]
        if raw is None:
            raise FileNotFoundError(path)
        version = hashlib.sha256(raw).hexdigest()
        if expected_version is not None and expected_version != version:
            raise ValueError(
                "Source file changed between pages; re-read its current version."
            )
        text = raw.decode("utf-8")
        return {
            "source_ref": reference,
            "path": path,
            "version": version,
            "bytes": len(raw),
            "offset": offset,
            "length": len(text),
            "text": text[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }

    def compare(
        self,
        identity,
        before,
        after,
        offset=0,
        limit=100,
        expected_version=None,
        path=None,
    ):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= current_limits().service_page_rows
        ):
            raise ValueError("Invalid source comparison page.")
        left, right = self.resolve(identity, before), self.resolve(identity, after)
        lfiles = snapshot(Path(left["workspace"]), **left["snapshot_limits"])
        rfiles = snapshot(Path(right["workspace"]), **right["snapshot_limits"])
        version = fingerprint(
            [before, after, content_digest(lfiles), content_digest(rfiles)]
        )
        if expected_version is not None and expected_version != version:
            raise ValueError("Source comparison changed between pages.")
        changed = sorted(
            p
            for p in set(lfiles) | set(rfiles)
            if (lfiles.get(p) or {}).get("sha256")
            != (rfiles.get(p) or {}).get("sha256")
            or (lfiles.get(p) or {}).get("executable")
            != (rfiles.get(p) or {}).get("executable")
        )
        result = {
            "before": before,
            "after": after,
            "version": version,
            "total_files": len(changed),
            "files": [
                {"path": p, "before": lfiles.get(p), "after": rfiles.get(p)}
                for p in changed[offset : offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(changed) else None,
        }
        if path:

            if path not in lfiles and path not in rfiles:
                raise FileNotFoundError(path)

            def full(row, expected):
                if expected is None:
                    return ""
                root = Path(row["workspace"])
                assert_visible(root, path)
                with SafeFile(root, path) as source:
                    raw = source.snapshot(
                        max_bytes=current_limits().candidate_file_bytes
                    )[0]
                if raw is None:
                    raise FileNotFoundError(path)
                if hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                    raise ValueError(
                        "Source changed during comparison; inspect its current versions."
                    )
                return raw.decode("utf-8")

            a = full(left, lfiles.get(path))
            b = full(right, rfiles.get(path))
            text = "".join(
                difflib.unified_diff(
                    a.splitlines(True),
                    b.splitlines(True),
                    fromfile=before + "/" + path,
                    tofile=after + "/" + path,
                )
            )
            result.update(
                diff=text[: current_limits().git_diff_chars],
                diff_complete=len(text) <= current_limits().git_diff_chars,
                diff_read_hint=(
                    "Read both named source files with read_source and file versions for complete content."
                    if len(text) > current_limits().git_diff_chars
                    else None
                ),
            )
        return result

    def preserve(self, identity, reference, key):
        from homebrew_harness.storage.work_snapshot import freeze

        row = self.resolve(identity, reference)
        eid = "source_" + request_key("work-source", identity, key)
        prior = self.store.store.runtime.get("service_evidence", eid)
        if prior:
            if prior.get("from_ref") != reference:
                raise ValueError("Source snapshot request has another source.")
            return {
                **prior["source"],
                "source_ref": eid,
                "role": "preserved_draft",
                "mutable": False,
            }
        captured = freeze(Path(row["workspace"]), eid, row["snapshot_limits"])
        captured.update(
            commit=row.get("commit"),
            target_base=row.get("target_base"),
            snapshot_limits=row["snapshot_limits"],
        )
        try:
            captured["repo"] = (
                self.git.run(["rev-parse", "--absolute-git-dir"], cwd=row["workspace"])
                .decode()
                .strip()
            )
        except ValueError:
            captured["repo"] = row.get("repo")
        with self.store.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            self.store.put(
                conn,
                "evidence",
                {
                    "id": eid,
                    "kind": "work_source",
                    "work_id": identity,
                    "from_ref": reference,
                    "source_role": "preserved_draft",
                    "source": captured,
                },
            )
        return {
            **captured,
            "source_ref": eid,
            "role": "preserved_draft",
            "mutable": False,
        }

    def target(self, identity, key):
        """Observe the registered target once and retain its exact object."""
        from .delivery.integration import materialize

        work = self.work(identity)
        limits = (work.get("run_spec") or {}).get(
            "snapshot_limits"
        ) or self.runtime.spec.run_spec("Inspect target").snapshot_limits
        eid = "source_" + request_key("work-target", identity, key)
        old = self.store.store.runtime.get("service_evidence", eid)
        if old:
            return {
                **old["source"],
                "source_ref": eid,
                "role": "target",
                "mutable": False,
            }
        directory = (
            self.runtime.store.root / ".harness/delivery/sources" / identity / eid
        )
        directory.mkdir(parents=True, exist_ok=True)
        repo = directory / "repo.git"
        if not repo.exists():
            self.git.run(["init", "--bare", "--template=", str(repo)])
        manifest = directory / "manifest.json"
        if manifest.is_file():
            source = json.loads(manifest.read_text())
            self.git.verify(source)
        else:
            self.git.run(
                [
                    "--git-dir=" + str(repo),
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    self.git.values["remote"],
                    "refs/heads/" + self.runtime.spec.delivery["target_branch"],
                ],
                credentials=True,
            )
            head = (
                self.git.run(["--git-dir=" + str(repo), "rev-parse", "FETCH_HEAD"])
                .decode()
                .strip()
            )
            source = materialize(
                self.git,
                repo,
                head,
                directory,
                limits,
                fingerprint([identity, eid, head]),
                {},
            )
        with self.store.db.write() as conn:
            self.runtime.store.guard(conn, self.runtime.header)
            self.store.put(
                conn,
                "evidence",
                {
                    "id": eid,
                    "kind": "work_source",
                    "source_role": "target",
                    "work_id": identity,
                    "source": source,
                },
            )
        return {**source, "source_ref": eid, "role": "target", "mutable": False}
