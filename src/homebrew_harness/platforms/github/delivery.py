"""Fixed GitHub delivery operations with correlation-based read reconciliation."""

from functools import wraps
from homebrew_harness.platforms.contracts import ObjectRef
from homebrew_harness.platforms.http import JsonTransport
from homebrew_harness.platforms.effects import publication, tracks_write
from homebrew_harness.execution.execution_control import (
    ControlledRLock,
    operation_deadline,
)

from homebrew_harness.platforms.merges import CLOSING


def serialized(function):
    @wraps(function)
    def invoke(self, *args, **kw):
        with self.lock:
            return function(self, *args, **kw)

    return invoke


class GithubDelivery:
    closing = CLOSING

    def capabilities(self):
        return {
            "merge_observation": True,
            "merge_methods": ["merge", "squash", "rebase"],
            "async_merge": False,
        }

    def __init__(self, spec, values):
        self.spec, self.values = spec, values
        self.lock = ControlledRLock()
        verify = True
        if values.get("ca_file"):
            import ssl

            verify = ssl.create_default_context(cafile=values["ca_file"])
        self.transport = JsonTransport(
            values["api_url"], values["api_token"], verify=verify
        )
        self.prefix = "repos/" + spec["project"]

    @serialized
    def close(self):
        self.transport.close()

    @tracks_write
    def request(self, method, path, body=None, params=None):
        return self.transport.request(
            method,
            path,
            operation_deadline(self.spec["request_seconds"]),
            body=body,
            params=params,
        )[0]

    def pages(self, path, params=None):
        rows = []
        for page in range(1, 11):
            data, headers = self.transport.request(
                "GET",
                path,
                operation_deadline(self.spec["request_seconds"]),
                params={**(params or {}), "per_page": 100, "page": page},
            )
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError("Invalid GitHub delivery query response.")
            rows.extend(data)
            if len(data) < 100 and 'rel="next"' not in headers.get("link", ""):
                return rows
        raise ValueError("Delivery query reached its page bound.")

    def marker(self, key):
        return "<!-- harness-delivery:" + key + " -->"

    @serialized
    @publication
    def ensure_change_request(self, plan, *, reconcile=False):
        marker = self.marker(plan["correlation"])
        title, body = plan["title"], plan["body"]
        if CLOSING.search(body):
            raise ValueError(
                "Change-request descriptions cannot implicitly close issues."
            )
        if plan.get("existing"):
            ref = ObjectRef(**plan["existing"])
            self.target(ref.data())
            path = self.prefix + "/pulls/" + str(ref.number)
            row = self.request("GET", path)
            if (
                row.get("head", {}).get("ref") != plan["branch"]
                or row.get("base", {}).get("ref") != self.spec["target_branch"]
                or row.get("head", {}).get("sha") != plan["commit"]
                or row.get("state") != "open"
            ):
                raise ValueError(
                    "Existing change request no longer matches the repair."
                )
            if marker not in (row.get("body") or ""):
                if reconcile:
                    return None
                if self.marker(plan["previous_correlation"]) not in (
                    row.get("body") or ""
                ):
                    raise ValueError(
                        "Existing change request lost its ownership marker."
                    )
                row = self.request(
                    "PATCH", path, {"title": title, "body": body + "\n\n" + marker}
                )
            matches = [row]
        else:
            matches = [
                r
                for r in self.pages(
                    self.prefix + "/pulls",
                    {
                        "state": "all",
                        "head": self.spec["project"].split("/")[0]
                        + ":"
                        + plan["branch"],
                        "base": self.spec["target_branch"],
                    },
                )
                if marker in (r.get("body") or "")
                and r.get("head", {}).get("ref") == plan["branch"]
                and r.get("base", {}).get("ref") == self.spec["target_branch"]
            ]
        if len(matches) > 1:
            raise RuntimeError("Change-request correlation is ambiguous.")
        if not matches:
            if reconcile:
                return None
            row = self.request(
                "POST",
                self.prefix + "/pulls",
                {
                    "title": title,
                    "body": body + "\n\n" + marker,
                    "head": plan["branch"],
                    "base": self.spec["target_branch"],
                },
            )
        else:
            row = matches[0]
        number = row.get("number")
        if (
            type(number) is not int
            or number < 1
            or row.get("head", {}).get("sha") != plan["commit"]
        ):
            raise RuntimeError(
                "Change-request response does not confirm the candidate."
            )
        return {
            "confirmed": True,
            "subject": ObjectRef(
                self.spec["provider"],
                self.spec["instance"],
                self.spec["project"],
                "change_request",
                str(number),
                number,
            ).data(),
            "object": ObjectRef(
                self.spec["provider"],
                self.spec["instance"],
                self.spec["project"],
                "change_request",
                str(number),
                number,
            ).data(),
            "actor_id": (row.get("user") or {}).get("id"),
            "head": row["head"]["sha"],
            "state": row.get("state"),
            "merged": row.get("merged", False),
        }

    def target(self, ref):
        subject = ObjectRef(**ref)
        if (
            subject.scope()
            != (self.spec["provider"], self.spec["instance"], self.spec["project"])
            or subject.kind not in {"issue", "change_request"}
            or subject.number is None
        ):
            raise ValueError("Comment target is outside delivery scope.")
        return self.prefix + "/issues/" + str(subject.number) + "/comments"

    @serialized
    @publication
    def post_comment(self, plan, *, reconcile=False):
        path = self.target(plan["subject"])
        marker = self.marker(plan["correlation"])
        body = plan["body"] + "\n\n" + marker
        matches = [r for r in self.pages(path) if (r.get("body") or "") == body]
        if len(matches) > 1:
            raise RuntimeError("Comment correlation is ambiguous.")
        if not matches:
            if reconcile:
                return None
            row = self.request("POST", path, {"body": body})
        else:
            row = matches[0]
        if type(row.get("id")) is not int:
            raise RuntimeError("Comment response is not confirmed.")
        return {
            "confirmed": True,
            "id": str(row["id"]),
            "subject": plan["subject"],
            "object": ObjectRef(
                self.spec["provider"],
                self.spec["instance"],
                self.spec["project"],
                "comment",
                str(row["id"]),
            ).data(),
            "actor_id": (row.get("user") or {}).get("id"),
        }

    @serialized
    def metadata(self, subject):
        from .merge import metadata

        return metadata(self, subject)

    @serialized
    def merge_gate(self, method):
        from .merge import rule_evidence

        return rule_evidence(self, method)

    @serialized
    @publication
    def merge_change_request(self, plan):
        from .merge import merge

        return merge(self, plan)

    @serialized
    def inspect_merge(self, plan):
        from homebrew_harness.platforms.merges import receipt, validate_plan

        ref = validate_plan(plan, self.spec)
        observed = self.metadata(ref.data())
        return (
            receipt(ref.data(), observed, plan["commit"], method=plan["method"])
            if observed["merged"]
            else None
        )

    @serialized
    def feedback(self, subject):
        from .feedback import GithubFeedback
        from homebrew_harness.platforms.contracts import ObjectRef

        ref = ObjectRef(**subject)
        reader = GithubFeedback({**self.spec, "max_pages": 10}, self.transport)
        batch = reader.fetch(ref, {}, operation_deadline(self.spec["request_seconds"]))
        details = self.metadata(ref.data())
        if batch.head != details["head"]:
            raise ValueError("PR head changed during observation.")
        return {
            **details,
            "checks": [
                {
                    "id": event.object.id,
                    "name": event.name,
                    "app_id": event.actor_id,
                    "head": event.head,
                    "status": event.status,
                    "conclusion": event.conclusion,
                    "body": event.body,
                }
                for event in batch.events
                if event.object.kind == "check"
            ],
            "reviews": [
                {
                    "id": event.object.id,
                    "actor_id": event.actor_id,
                    "head": event.head,
                    "status": event.status,
                    "body": event.body,
                    "version": event.version,
                }
                for event in batch.events
                if event.object.kind == "review"
            ],
        }
