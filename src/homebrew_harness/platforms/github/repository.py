"""GitHub issue/PR/comment translation and its opaque reconciliation cursor."""

import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from homebrew_harness.platforms.contracts import (
    ObjectRef,
    SourceEvent,
    SourcePage,
    bounded,
)


def validate_project(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise ValueError("GitHub requires an owner/project identifier.")
    return value


def stamp(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class GithubRepository:
    def __init__(self, spec, transport):
        self.spec, self.transport = spec, transport
        self.parents = {}

    def close(self):
        self.transport.close()

    def reference(self, kind, identity, number=None):
        return ObjectRef(
            self.spec["provider"],
            self.spec["instance"],
            self.spec["project"],
            kind,
            str(identity),
            number,
        )

    def subject(self, row):
        number = row.get("number")
        if type(number) is not int or number < 1:
            raise ValueError("Invalid GitHub project number.")
        ref = self.reference(
            "change_request" if "pull_request" in row else "issue", number, number
        )
        self.parents[number] = ref
        return ref

    def event(self, row, stream, deadline):
        identity = row.get("id")
        if type(identity) is not int or identity < 1:
            raise ValueError("GitHub object has no stable ID.")
        if stream == "issues":
            subject = self.subject(row)
            ref = subject
        else:
            path = urlparse(row.get("issue_url", "")).path
            prefix = "/repos/" + self.spec["project"] + "/issues/"
            pos = path.find(prefix)
            suffix = path[pos + len(prefix) :] if pos >= 0 else ""
            if not suffix.isdecimal():
                raise ValueError(
                    "Comment parent is outside the configured GitHub project."
                )
            number = int(suffix)
            subject = self.parents.get(number)
            if subject is None:
                parent, _ = self.transport.request(
                    "GET",
                    "repos/" + self.spec["project"] + "/issues/" + str(number),
                    deadline,
                )
                if not isinstance(parent, dict) or parent.get("number") != number:
                    raise ValueError("Invalid GitHub comment parent response.")
                subject = self.subject(parent)
            ref = self.reference("comment", identity)
        actor = row.get("user") or {}
        if not isinstance(actor, dict):
            raise ValueError("Invalid GitHub actor.")
        content = {
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "state": row.get("state"),
            "actor_id": actor.get("id"),
            "metadata": {
                "api_object_id": identity,
                "association": row.get("author_association"),
            },
        }
        return SourceEvent(
            ref,
            subject,
            bounded(row.get("updated_at"), "GitHub object version", 100),
            content,
        )

    def poll(self, saved, deadline):
        now = time.time()
        cursor = dict(saved or {})
        if cursor.pop("rescan_required", False):
            cursor = {"watermark": cursor.get("watermark", 0)}
        if not cursor.get("sweep"):
            cursor.update(
                sweep=now,
                stream="issues",
                page=1,
                since=max(0, cursor.get("watermark", 0) - 60),
                pass_digest="0",
                pass_count=0,
                passes=0,
            )
        stream, page = cursor["stream"], cursor["page"]
        path = (
            "repos/"
            + self.spec["project"]
            + ("/issues" if stream == "issues" else "/issues/comments")
        )
        params = {
            "per_page": 50,
            "page": page,
            "sort": "updated",
            "direction": "asc",
        }
        if cursor["since"] > 0:
            params["since"] = stamp(cursor["since"])
        if stream == "issues":
            params["state"] = "all"
        rows, headers = self.transport.request("GET", path, deadline, params=params)
        if (
            not isinstance(rows, list)
            or len(rows) > 50
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("Invalid bounded GitHub source page.")
        events = [self.event(row, stream, deadline) for row in rows]
        digest = int(cursor["pass_digest"], 16)
        for event in events:
            digest ^= int(hashlib.sha256(event.data()["key"].encode()).hexdigest(), 16)
        cursor.update(
            pass_digest=format(digest, "x"),
            pass_count=cursor["pass_count"] + len(events),
        )
        more = 'rel="next"' in headers.get("link", "") or len(rows) == 50
        if more:
            cursor["page"] += 1
        elif stream == "issues":
            cursor.update(stream="comments", page=1)
        else:
            observed = [cursor["pass_count"], cursor["pass_digest"]]
            if cursor.get("previous_pass") == observed:
                cursor = {"watermark": cursor["sweep"]}
            elif cursor["passes"] >= 3:
                cursor = {
                    "watermark": cursor.get("watermark", 0),
                    "rescan_required": True,
                }
            else:
                cursor.update(
                    stream="issues",
                    page=1,
                    pass_digest="0",
                    pass_count=0,
                    previous_pass=observed,
                    passes=cursor["passes"] + 1,
                )
        state = (
            "rescan_required"
            if cursor.get("rescan_required")
            else "reconciling"
            if cursor.get("sweep")
            else "ready"
        )
        return SourcePage(
            tuple(events),
            cursor,
            now + (self.spec["poll_seconds"] if not cursor.get("sweep") else 0),
            state,
        )
