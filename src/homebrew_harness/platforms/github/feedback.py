"""GitHub pull-request feedback normalized without Run or Service storage."""

from datetime import datetime, timezone
import re
import time

from homebrew_harness.security.data_boundary import redact
from homebrew_harness.platforms.contracts import (
    ChangeFeedback,
    FeedbackEvent,
    ObjectRef,
)


def stamp(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class GithubFeedback:
    def __init__(self, spec, transport):
        self.spec, self.transport = spec, transport

    def close(self):
        self.transport.close()

    def pages(self, path, deadline, params=None, field=None):
        result = []
        for page in range(1, self.spec["max_pages"] + 1):
            raw, headers = self.transport.request(
                "GET",
                path,
                deadline,
                params={**(params or {}), "per_page": 100, "page": page},
            )
            rows = raw.get(field) if field and isinstance(raw, dict) else raw
            if (
                not isinstance(rows, list)
                or len(rows) > 100
                or any(not isinstance(r, dict) for r in rows)
            ):
                raise ValueError("Invalid bounded GitHub feedback page.")
            result.extend(rows)
            if not ('rel="next"' in headers.get("link", "") or len(rows) == 100):
                return result
        raise ValueError(
            "Feedback pagination exceeds its bound; cursor was not advanced."
        )

    def fetch(self, subject, position, deadline):
        started = time.time()
        prefix = "repos/" + self.spec["project"]
        number = str(subject.number)
        raw, _ = self.transport.request("GET", prefix + "/pulls/" + number, deadline)
        head = raw.get("head", {}).get("sha") if isinstance(raw, dict) else None
        if not isinstance(head, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", head
        ):
            raise ValueError("Invalid GitHub change-request head.")
        since = position.get("comments_after")
        query = {}
        if since:
            query["since"] = stamp(
                datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() - 30
            )
        comments = self.pages(
            prefix + "/issues/" + number + "/comments", deadline, params=query
        )
        reviews = self.pages(prefix + "/pulls/" + number + "/reviews", deadline)
        checks = self.pages(
            prefix + "/commits/" + head + "/check-runs", deadline, field="check_runs"
        )
        events = []
        for kind, rows in (
            ("comment", comments),
            ("review", reviews),
            ("check", checks),
        ):
            for row in rows:
                identity = row.get("id")
                if type(identity) is not int or identity < 1:
                    raise ValueError("Feedback object lacks a stable ID.")
                actor = (row.get("app") if kind == "check" else row.get("user")) or {}
                if not isinstance(actor, dict):
                    raise ValueError("Invalid feedback actor.")
                event_head = (
                    row.get("head_sha")
                    if kind == "check"
                    else row.get("commit_id")
                    if kind == "review"
                    else head
                )
                body = redact(
                    row.get("output", {})
                    if kind == "check"
                    else (row.get("body") or "")
                )
                version = row.get(
                    "updated_at", row.get("submitted_at", row.get("completed_at", ""))
                )
                version = version or row.get("started_at") or ""
                if not isinstance(version, str):
                    raise ValueError("Invalid feedback object version.")
                ref = ObjectRef(
                    subject.provider,
                    subject.instance,
                    subject.project,
                    kind,
                    str(identity),
                )
                events.append(
                    FeedbackEvent(
                        ref,
                        actor.get("id")
                        if type(actor.get("id")) is int and actor["id"] > 0
                        else None,
                        "app" if kind == "check" else "user",
                        event_head,
                        version,
                        body,
                        row.get("status", row.get("state")),
                        row.get("conclusion"),
                        kind == "review"
                        and (row.get("state") == "PENDING" or not version),
                    )
                )
        latest = max(
            started,
            datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
            if since
            else 0,
        )
        return ChangeFeedback(
            subject, head, tuple(events), {"comments_after": stamp(latest)}
        ).validate(subject)
