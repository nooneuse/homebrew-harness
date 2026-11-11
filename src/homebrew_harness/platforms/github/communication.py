"""Version-bound GitHub discussion and review operations using public REST APIs."""

import base64
import hashlib
import re
from functools import wraps

from homebrew_harness.execution.execution_control import (
    ControlledRLock,
    operation_deadline,
    check,
)
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent, digest
from .repository import validate_project
from homebrew_harness.platforms.effects import publication, tracks_write


def serialized(function):
    @wraps(function)
    def invoke(self, *args, **kwargs):
        with self.lock:
            return function(self, *args, **kwargs)

    return invoke


def oid(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", value
    ):
        raise ValueError("Platform returned an invalid code object.")
    return value


class GithubCommunication:
    capabilities = frozenset(
        {
            "post_comment",
            "reply_thread",
            "submit_review",
            "inline_review",
            "version_anchored_review",
            "read_reconcile",
            "snapshot",
        }
    )

    def __init__(self, spec, transport):
        self.spec, self.transport = spec, transport
        self.prefix = "repos/" + spec["project"]
        self.lock = ControlledRLock()
        self.actor = None

    @serialized
    def close(self):
        self.transport.close()

    @tracks_write
    def request(self, method, path, body=None, params=None):
        return self.transport.request(
            method, path, operation_deadline(10), body=body, params=params
        )[0]

    def pages(self, path, params=None):
        result = []
        for page in range(1, self.spec["max_pages"] + 1):
            raw, headers = self.transport.request(
                "GET",
                path,
                operation_deadline(10),
                params={**(params or {}), "per_page": 100, "page": page},
            )
            if not isinstance(raw, list) or len(raw) > 100:
                raise ValueError("Invalid discussion page.")
            result.extend(raw)
            if len(raw) < 100 and 'rel="next"' not in headers.get("link", ""):
                return result
        raise ValueError("Discussion exceeded its page bound.")

    @serialized
    def identity(self):
        if self.actor is None:
            data = self.request("GET", "user")
            if (
                not isinstance(data, dict)
                or type(data.get("id")) is not int
                or data["id"] <= 0
            ):
                raise ValueError("Publisher lacks a stable authenticated identity.")
            self.actor = data["id"]
        return self.actor

    def target(self, subject):
        ref = ObjectRef(**subject)
        if (
            ref.scope()
            != (self.spec["provider"], self.spec["instance"], self.spec["project"])
            or ref.kind not in {"issue", "change_request"}
            or ref.number is None
        ):
            raise ValueError("Discussion target crosses platform scope.")
        return ref

    def reference(self, kind, identity):
        return ObjectRef(
            self.spec["provider"],
            self.spec["instance"],
            self.spec["project"],
            kind,
            str(identity),
        )

    def details(self, subject):
        ref = self.target(subject)
        row = self.request(
            "GET",
            self.prefix
            + ("/pulls/" if ref.kind == "change_request" else "/issues/")
            + str(ref.number),
        )
        if not isinstance(row, dict) or row.get("number") != ref.number:
            raise ValueError("Discussion target changed.")
        return row

    @serialized
    def observe(self, subject):
        ref = self.target(subject)
        before = self.details(subject)
        events = []
        parent = SourceEvent(
            ref,
            ref,
            str(before.get("updated_at") or digest(before)),
            {
                "title": before.get("title") or "",
                "body": before.get("body") or "",
                "state": before.get("state"),
                "actor_id": (before.get("user") or {}).get("id"),
                "metadata": {"api_object_id": before.get("id")},
            },
        )
        events.append(parent)
        resources = [("comment", "/issues/" + str(ref.number) + "/comments")]
        if ref.kind == "change_request":
            resources.extend(
                [
                    ("review", "/pulls/" + str(ref.number) + "/reviews"),
                    ("inline", "/pulls/" + str(ref.number) + "/comments"),
                ]
            )
        for kind, path in resources:
            for row in self.pages(self.prefix + path):
                if type(row.get("id")) is not int:
                    raise ValueError("Discussion object lacks a stable identity.")
                identity = (
                    "review_comment:" + str(row["id"])
                    if kind == "inline"
                    else str(row["id"])
                )
                metadata = {"channel": kind, "api_object_id": row["id"]}
                if kind == "inline":
                    metadata.update(
                        thread_id=str(row.get("in_reply_to_id") or row["id"]),
                        review_id=str(row.get("pull_request_review_id") or ""),
                        reply_to=row.get("in_reply_to_id"),
                        path=row.get("path"),
                        line=row.get("line"),
                        side=row.get("side", "RIGHT"),
                        commit=row.get("commit_id"),
                    )
                if kind == "review":
                    metadata.update(
                        commit=row.get("commit_id"), review_state=row.get("state")
                    )
                events.append(
                    SourceEvent(
                        self.reference(
                            "review" if kind == "review" else "comment", identity
                        ),
                        ref,
                        str(
                            row.get("updated_at")
                            or row.get("submitted_at")
                            or digest(row)
                        ),
                        {
                            "title": "",
                            "body": row.get("body") or "",
                            "state": row.get("state"),
                            "actor_id": (row.get("user") or {}).get("id"),
                            "metadata": metadata,
                        },
                    )
                )
        after = self.details(subject)
        version = self.basis(after, ref)
        if self.basis(before, ref) != version:
            raise ValueError("PR changed during discussion pagination.")
        return {
            "events": [event.data() for event in events],
            "basis": version,
            "complete": True,
        }

    def basis(self, row, ref):
        result = {
            "state": row.get("state"),
            "version": row.get("updated_at") or digest(row),
        }
        if ref.kind == "change_request":
            result.update(
                head=oid(row.get("head", {}).get("sha")),
                base=oid(row.get("base", {}).get("sha")),
                head_project=validate_project(
                    row.get("head", {}).get("repo", {}).get("full_name")
                ),
                base_project=validate_project(
                    row.get("base", {}).get("repo", {}).get("full_name")
                ),
            )
            result.pop("version", None)
        return result

    @serialized
    def current_basis(self, subject):
        ref = self.target(subject)
        return self.basis(self.details(subject), ref)

    @staticmethod
    def marker(key):
        return "<!-- harness-communication:" + key + " -->"

    @serialized
    @publication
    def publish(self, plan, *, reconcile=False):
        ref = self.target(plan["subject"])
        actor = self.identity()
        marker = self.marker(plan["correlation"])
        body = plan["body"] + "\n\n" + marker
        kind = plan["kind"]
        if kind == "post_comment":
            path = self.prefix + "/issues/" + str(ref.number) + "/comments"
            payload = {"body": body}
        elif kind == "reply_thread":
            if (
                ref.kind != "change_request"
                or not str(plan.get("thread_id", "")).isdecimal()
            ):
                raise ValueError("Thread replies require a PR thread.")
            thread = self.request(
                "GET", self.prefix + "/pulls/comments/" + plan["thread_id"]
            )
            from urllib.parse import urlsplit

            if urlsplit(
                thread.get("pull_request_url", "")
            ).path != "/" + self.prefix + "/pulls/" + str(ref.number):
                raise ValueError("Thread belongs to another PR.")
            path = self.prefix + "/pulls/" + str(ref.number) + "/comments"
            payload = {"body": body, "in_reply_to": int(plan["thread_id"])}
        elif kind == "submit_review":
            if ref.kind != "change_request":
                raise ValueError("Formal review requires a PR.")
            path = self.prefix + "/pulls/" + str(ref.number) + "/reviews"
            payload = {
                "body": body,
                "event": plan["event"],
                "commit_id": plan["review_basis"]["head"],
                "comments": plan.get("comments", []),
            }
        else:
            raise ValueError("Unsupported communication operation.")
        matches = []
        for row in self.pages(path):
            if row.get("body") == body and (row.get("user") or {}).get("id") == actor:
                if (
                    kind == "submit_review"
                    and row.get("commit_id") != plan["review_basis"]["head"]
                ):
                    continue
                if (
                    kind == "reply_thread"
                    and str(row.get("in_reply_to_id")) != plan["thread_id"]
                ):
                    continue
                matches.append(row)
        if len(matches) > 1:
            raise ValueError("Communication correlation is ambiguous.")
        if not matches:
            if reconcile:
                return None
            write_path = (
                path + "/" + plan["thread_id"] + "/replies"
                if kind == "reply_thread"
                else path
            )
            if kind == "reply_thread":
                payload = {"body": body}
            row = self.request("POST", write_path, payload)
        else:
            row = matches[0]
        if (
            type(row.get("id")) is not int
            or (row.get("user") or {}).get("id") != actor
            or row.get("body") != body
        ):
            raise ValueError(
                "Publication response does not confirm its author/content."
            )
        if kind == "submit_review" and (
            row.get("commit_id") != plan["review_basis"]["head"]
            or row.get("state", "").upper()
            != {
                "COMMENT": "COMMENTED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "APPROVE": "APPROVED",
            }[plan["event"]]
        ):
            raise ValueError("Review response does not confirm its version or event.")
        remote_id = (
            "review_comment:" + str(row["id"])
            if kind == "reply_thread"
            else str(row["id"])
        )
        return {
            "confirmed": True,
            "subject": ref.data(),
            "object": self.reference(
                "review" if kind == "submit_review" else "comment", remote_id
            ).data(),
            "actor_id": actor,
            "body_hash": digest(body),
            "head": row.get("commit_id"),
            "id": str(row["id"]),
        }

    @serialized
    def snapshot(self, subject, policy):
        ref = self.target(subject)
        if ref.kind != "change_request":
            raise ValueError("Snapshot requires a PR.")
        before = self.current_basis(subject)
        if before["state"] != "open":
            raise ValueError("PR is not open.")
        prefix = "repos/" + before["head_project"]
        commit = self.request("GET", prefix + "/git/commits/" + before["head"])
        if commit.get("sha") != before["head"]:
            raise ValueError("Commit response changed its identity.")
        tree_id = oid(commit.get("tree", {}).get("sha"))
        tree = self.request(
            "GET", prefix + "/git/trees/" + tree_id, params={"recursive": 1}
        )
        if (
            tree.get("sha") != tree_id
            or tree.get("truncated")
            or not isinstance(tree.get("tree"), list)
            or len(tree["tree"]) > policy["max_paths"]
        ):
            raise ValueError("PR tree is incomplete or exceeds its coverage bound.")
        compare = self.request(
            "GET",
            "repos/"
            + before["base_project"]
            + "/compare/"
            + before["base"]
            + "..."
            + before["head"],
        )
        merge_base = oid(compare.get("merge_base_commit", {}).get("sha"))
        changed = self.pages(self.prefix + "/pulls/" + str(ref.number) + "/files")
        from homebrew_harness.execution.source_snapshot import matches
        from homebrew_harness.security.file_visibility import host_managed_parts

        files = {}
        modes = {}
        skipped = []
        size = 0
        seen = set()
        for entry in tree["tree"]:
            check()
            path = entry.get("path")
            mode = entry.get("mode")
            if (
                not isinstance(path, str)
                or path.startswith("/")
                or "\\" in path
                or any(p in {"", "..", "."} for p in path.split("/"))
                or path in seen
            ):
                raise ValueError("Invalid PR tree path.")
            seen.add(path)
            if entry.get("type") == "tree":
                continue
            if host_managed_parts(tuple(path.split("/"))) or not matches(
                path, policy["review_globs"]
            ):
                skipped.append({"path": path, "reason": "outside_review_scope"})
                continue
            if mode not in {"100644", "100755"}:
                skipped.append({"path": path, "reason": "unsupported_mode"})
                continue
            if (
                type(entry.get("size")) is not int
                or entry["size"] > policy["max_file_bytes"]
            ):
                skipped.append({"path": path, "reason": "file_size"})
                continue
            blob_id = oid(entry.get("sha"))
            blob = self.request("GET", prefix + "/git/blobs/" + blob_id)
            if blob.get("sha") != blob_id or blob.get("encoding") != "base64":
                raise ValueError("Invalid Git blob response.")
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
            encoded = b"blob " + str(len(raw)).encode() + b"\0" + raw
            actual = (
                hashlib.sha1(encoded).hexdigest()
                if len(blob_id) == 40
                else hashlib.sha256(encoded).hexdigest()
            )
            if actual != blob_id or len(raw) != entry["size"]:
                raise ValueError("PR blob content does not match its identity.")
            size += len(raw)
            if size > policy["max_bytes"]:
                raise ValueError("PR content exceeds its snapshot bound.")
            try:
                content = raw.decode("utf-8")
            except UnicodeError:
                skipped.append({"path": path, "reason": "binary"})
                continue
            if "\0" in content:
                skipped.append({"path": path, "reason": "binary"})
                continue
            files[path] = content
            modes[path] = mode
        base_files = {}
        from urllib.parse import quote
        from homebrew_harness.platforms.http import RemoteFailure

        for entry in changed:
            path = entry.get("previous_filename") or entry.get("filename")
            if (
                not isinstance(path, str)
                or path.startswith("/")
                or any(p in {"..", ".", ""} for p in path.split("/"))
            ):
                raise ValueError("Invalid changed-file path.")
            if not matches(path, policy["review_globs"]) or host_managed_parts(
                tuple(path.split("/"))
            ):
                continue
            try:
                base_blob = self.request(
                    "GET",
                    "repos/"
                    + before["base_project"]
                    + "/contents/"
                    + quote(path, safe="/"),
                    params={"ref": merge_base},
                )
            except RemoteFailure as failure:
                if failure.status == 404:
                    continue
                raise
            if base_blob.get("encoding") != "base64" or not isinstance(
                base_blob.get("content"), str
            ):
                skipped.append({"path": path, "reason": "base_content"})
                continue
            raw = base64.b64decode("".join(base_blob["content"].split()), validate=True)
            blob_id = oid(base_blob.get("sha"))
            encoded = b"blob " + str(len(raw)).encode() + b"\0" + raw
            actual = (
                hashlib.sha1(encoded).hexdigest()
                if len(blob_id) == 40
                else hashlib.sha256(encoded).hexdigest()
            )
            if actual != blob_id:
                raise ValueError("Base file does not match its fixed object.")
            if len(raw) > policy["max_file_bytes"]:
                skipped.append({"path": path, "reason": "base_size"})
                continue
            size += len(raw)
            if size > policy["max_bytes"]:
                raise ValueError("PR snapshot exceeds its content bound.")
            try:
                content = raw.decode("utf-8")
            except UnicodeError:
                skipped.append({"path": path, "reason": "base_binary"})
                continue
            if "\0" in content:
                skipped.append({"path": path, "reason": "base_binary"})
                continue
            base_files[path] = content
        for entry in changed:
            if not isinstance(entry.get("patch"), str):
                skipped.append(
                    {"path": entry.get("filename"), "reason": "diff_unavailable"}
                )
        after = self.current_basis(subject)
        if before != after:
            raise ValueError("PR changed during snapshot capture.")
        from .review_coverage import coverage

        return {
            "basis": before,
            "tree": tree_id,
            "merge_base": merge_base,
            "files": files,
            "modes": modes,
            "base_files": base_files,
            "changed": changed,
            "coverage": coverage(changed, files, base_files, skipped, policy, size),
        }
