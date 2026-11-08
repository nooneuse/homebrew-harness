"""Bounded authenticated HTTP input for one existing GitHub pull request."""

import json
import asyncio
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from homebrew_harness.execution.execution_control import check
from homebrew_harness.storage.storage_gate import storage_gate
from homebrew_harness.security.data_boundary import redact, register_private_values
from .requests import digest
from .spec import TERMINAL


class RemoteFailure(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__("Remote service returned status " + str(status))


def validated_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Invalid external service address.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(
            "External service URL must identify one host without embedded credentials."
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(
            "Authenticated external services require HTTPS; HTTP is for loopback fixtures."
        )
    return value


class JsonTransport:
    def __init__(self, endpoint, token):
        self.endpoint = validated_url(endpoint)
        if not isinstance(token, str) or any(
            ord(c) < 32 or ord(c) == 127 for c in token
        ):
            raise ValueError("Invalid external service credential.")
        register_private_values(endpoint, token)
        self.loop = asyncio.new_event_loop()
        headers = {"Accept": "application/json", "User-Agent": "homebrew-harness"}
        if token:
            headers["Authorization"] = "Bearer " + token
        self.client = httpx.AsyncClient(
            base_url=self.endpoint.rstrip("/") + "/",
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        )
        self.identity = digest({"endpoint": self.endpoint})

    def request(
        self,
        method,
        path,
        deadline,
        *,
        params=None,
        body=None,
        headers=None,
        parse_json=True,
    ):
        check(deadline)
        remaining = deadline.remaining()
        return self.loop.run_until_complete(
            asyncio.wait_for(
                self._request(
                    method,
                    path,
                    deadline,
                    params=params,
                    body=body,
                    headers=headers,
                    parse_json=parse_json,
                ),
                timeout=remaining,
            )
        )

    async def _request(
        self, method, path, deadline, *, params, body, headers, parse_json
    ):
        remaining = deadline.remaining()
        target = self.endpoint if method == "POST" and not path else path
        async with self.client.stream(
            method,
            target,
            params=params,
            json=body,
            headers=headers,
            timeout=min(5, remaining) if remaining is not None else 5,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise RemoteFailure(response.status_code)
            if not parse_json:
                return None, dict(response.headers)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                check(deadline)
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("External JSON response exceeds its size limit.")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return (json.loads(raw) if raw else None), dict(response.headers)

    def close(self):
        if self.loop.is_closed():
            return
        try:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), 1
                    )
                )
            self.loop.run_until_complete(asyncio.wait_for(self.client.aclose(), 1))
        finally:
            self.loop.close()


def iso_time(value):
    return (
        datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    )


class GithubPullSource:
    def __init__(
        self,
        transport,
        repository,
        pull_number,
        actor_ids,
        app_ids,
        *,
        poll_seconds=30,
        max_pages=10,
    ):
        if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise ValueError("GitHub source requires an explicit repository.")
        if any(part in {".", ".."} for part in repository.split("/")):
            raise ValueError("GitHub repository cannot contain dot path segments.")
        if type(pull_number) is not int or pull_number < 1:
            raise ValueError("GitHub source requires a positive pull number.")
        if (
            not isinstance(actor_ids, (list, tuple))
            or not isinstance(app_ids, (list, tuple))
            or any(type(v) is not int or v < 1 for v in [*actor_ids, *app_ids])
        ):
            raise ValueError(
                "Source actor and app identities must be positive stable IDs."
            )
        if (
            type(poll_seconds) not in {int, float}
            or not 0.1 <= poll_seconds <= 3600
            or type(max_pages) is not int
            or not 1 <= max_pages <= 100
        ):
            raise ValueError("Invalid source polling bounds.")
        self.transport = transport
        self.repository, self.pull_number = repository, pull_number
        self.actor_ids, self.app_ids = frozenset(actor_ids), frozenset(app_ids)
        self.poll_seconds, self.max_pages = poll_seconds, max_pages
        self.identity = digest(
            {
                "transport": transport.identity,
                "repository": repository,
                "pull": pull_number,
                "actors": sorted(actor_ids),
                "apps": sorted(app_ids),
                "poll_seconds": poll_seconds,
                "max_pages": max_pages,
            }
        )

    def _pages(self, path, deadline, *, params=None, field=None):
        result = []
        for page in range(1, self.max_pages + 1):
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
                raise ValueError("Source page is not a bounded object list.")
            result.extend(rows)
            more = 'rel="next"' in headers.get("link", "") or len(rows) == 100
            if not more:
                return result
        raise ValueError(
            "Source pagination exceeds the configured bound; no cursor was advanced."
        )

    def poll(self, store, run_id, deadline):
        from .controller import git_head

        run = store.get(run_id)
        if run["status"] in TERMINAL:
            return False
        identity = "src_" + digest({"run": run_id, "source": self.identity})[:32]
        prior = store.runtime.get(
            "run_sources", identity, {"revision": 0, "cursor": {}, "next_poll_at": 0}
        )
        if prior["next_poll_at"] > time.time():
            return False
        head = git_head(store.root)
        if not head:
            raise ValueError("GitHub source requires a local Git HEAD.")
        prefix = "repos/" + self.repository
        poll_started = time.time()
        try:
            pr, _ = self.transport.request(
                "GET", prefix + "/pulls/" + str(self.pull_number), deadline
            )
            observed_head = (
                pr.get("head", {}).get("sha") if isinstance(pr, dict) else None
            )
            if not isinstance(observed_head, str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", observed_head
            ):
                raise ValueError("Source pull head is invalid.")
            since = prior["cursor"].get("comments_after")
            query = {}
            if since:
                instant = datetime.fromisoformat(since.replace("Z", "+00:00"))
                query["since"] = iso_time(instant.timestamp() - 30)
            comments = self._pages(
                prefix + "/issues/" + str(self.pull_number) + "/comments",
                deadline,
                params=query,
            )
            reviews = self._pages(
                prefix + "/pulls/" + str(self.pull_number) + "/reviews", deadline
            )
            checks = self._pages(
                prefix + "/commits/" + observed_head + "/check-runs",
                deadline,
                field="check_runs",
            )
            selected = []
            for kind, rows in (
                ("comment", comments),
                ("review", reviews),
                ("check", checks),
            ):
                for raw in rows:
                    if type(raw.get("id")) is not int or raw["id"] < 1:
                        raise ValueError("Source object has no stable identity.")
                    actor = (
                        raw.get("app") if kind == "check" else raw.get("user")
                    ) or {}
                    actor_id = actor.get("id") if isinstance(actor, dict) else None
                    event_head = (
                        raw.get("head_sha")
                        if kind == "check"
                        else raw.get("commit_id")
                        if kind == "review"
                        else observed_head
                    )
                    body = redact(
                        raw.get("output", {})
                        if kind == "check"
                        else raw.get("body", "")
                    )
                    stamp = raw.get(
                        "updated_at",
                        raw.get("submitted_at", raw.get("completed_at", "")),
                    )
                    event = {
                        "run_id": run_id,
                        "checkpoint": "lead:" + run["session_id"],
                        "source": identity,
                        "repository": self.repository,
                        "pull_number": self.pull_number,
                        "kind": kind,
                        "object_id": raw["id"],
                        "actor_id": actor_id,
                        "head": event_head,
                        "observed_head": observed_head,
                        "object_version": stamp,
                        "body": body,
                        "status": raw.get("status", raw.get("state")),
                        "conclusion": raw.get("conclusion"),
                    }
                    if len(json.dumps(event, ensure_ascii=False)) > 64000:
                        raise ValueError("Source object exceeds its body limit.")
                    allowed = type(actor_id) is int and actor_id in (
                        self.app_ids if kind == "check" else self.actor_ids
                    )
                    current = observed_head == head and event_head == head
                    reason = (
                        None
                        if allowed and current
                        else "actor_not_allowed"
                        if not allowed
                        else "old_head"
                    )
                    if kind == "review" and (
                        raw.get("state") == "PENDING" or not stamp
                    ):
                        reason = "old_or_pending_review"
                    if kind == "comment" and not isinstance(stamp, str):
                        raise ValueError("Comment object version must be a timestamp.")
                    event.update(
                        id="evt_"
                        + digest(
                            {
                                key: value
                                for key, value in event.items()
                                if key != "checkpoint"
                            }
                        )[:32],
                        revision=0,
                        state="ignored" if reason else "queued",
                        ignore_reason=reason,
                    )
                    selected.append(event)
            if git_head(store.root) != head:
                raise ValueError("Local head changed while polling its source.")
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                current = db._get(conn, "run_sources", identity, {"revision": 0})
                latest_run = db._get(conn, "runs", run_id)
                if current["revision"] != prior["revision"] or latest_run.get(
                    "control_identity"
                ) != run.get("control_identity"):
                    raise ValueError(
                        "Source ownership or policy changed before ingest."
                    )
                recipient = "reference_" + run_id
                count = conn.execute(
                    "SELECT count(*) FROM inbox WHERE recipient=?", (recipient,)
                ).fetchone()[0]
                for event in selected:
                    if db._get(conn, "run_events", event["id"]) is not None:
                        continue
                    if latest_run["status"] in TERMINAL:
                        event.update(state="ignored", ignore_reason="run_closed")
                    if event["state"] == "queued":
                        count += 1
                        if count > 64:
                            raise ValueError(
                                "Run reference inbox is full; no source cursor was advanced."
                            )
                        summary = {
                            k: v
                            for k, v in event.items()
                            if k
                            in {
                                "id",
                                "kind",
                                "head",
                                "actor_id",
                                "status",
                                "conclusion",
                            }
                        }
                        summary["body"] = json.dumps(event["body"], ensure_ascii=False)[
                            :4000
                        ]
                        db._send(
                            conn,
                            recipient,
                            {
                                "id": event["id"],
                                "from": "github_source",
                                "to": recipient,
                                "type": "run_reference",
                                "content": json.dumps(summary, ensure_ascii=False),
                                "ts": time.time(),
                                "metadata": {
                                    "origin": "github_source",
                                    "source": identity,
                                },
                            },
                        )
                    db._put(conn, "run_events", event["id"], event)
                # Overlap the next poll and deduplicate stable object revisions.
                # A provider timestamp must not move the cursor beyond this completed poll.
                last = iso_time(
                    max(
                        poll_started,
                        datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
                        if since
                        else 0,
                    )
                )
                db._put(
                    conn,
                    "run_sources",
                    identity,
                    {
                        "id": identity,
                        "run_id": run_id,
                        "checkpoint": "lead:" + run["session_id"],
                        "identity": self.identity,
                        "revision": prior["revision"] + 1,
                        "cursor": {"comments_after": last},
                        "failures": 0,
                        "next_poll_at": time.time() + self.poll_seconds,
                    },
                )
            return True
        except Exception as error:
            db = store.runtime.backend()
            with storage_gate(store.root), db.write() as conn:
                current = db._get(conn, "run_sources", identity, prior)
                failures = current.get("failures", 0) + 1
                db._put(
                    conn,
                    "run_sources",
                    identity,
                    {
                        **current,
                        "id": identity,
                        "run_id": run_id,
                        "checkpoint": "lead:" + run["session_id"],
                        "identity": self.identity,
                        "revision": current["revision"] + 1,
                        "failures": failures,
                        "last_error": type(error).__name__,
                        "last_error_status": error.status
                        if isinstance(error, RemoteFailure)
                        else None,
                        "next_poll_at": time.time()
                        + min(300, self.poll_seconds * 2 ** min(failures, 5)),
                    },
                )
            return False
