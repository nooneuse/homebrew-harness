"""A bounded operator view over service decisions and native Run requests."""

import json
import re
import time
from collections import Counter

from homebrew_harness.runtime.limits import current_limits, expired
from homebrew_harness.security.data_boundary import redact

from . import request_views
from .maintenance_spec import text
from .protocol import fingerprint, request_key

MAX_ITEMS = 100
QUESTIONS = {"attention", "work_information", "discussion_information", "delivery_information"}
TITLES = {
    "attention": "Run needs an answer",
    "work_information": "Clarify Work requirements",
    "discussion_information": "Answer a discussion question",
    "delivery_information": "Clarify delivery",
    "work_authority": "Authorize Work",
    "discussion_requirements": "Accept discussion requirements",
    "communication_authority": "Authorize a public response",
    "delivery_authority": "Authorize delivery",
    "ci_repair": "Authorize CI repair",
    "tool": "Approve a Run tool call",
    "test_grant": "Renew a Run test grant",
}


def _display(value, limit):
    # Redact before clipping, and strip terminal controls and Unicode format controls.
    value = " ".join("".join(c if c.isprintable() else " " for c in redact(value)).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _preview(*parts):
    return "\n".join(part for part in parts if isinstance(part, str) and part.strip())


def _item(runtime, request, active_work):
    ref, kind = request["ref"], request["kind"]
    args = request.get("arguments") or {}
    title = TITLES.get(kind, kind.replace("_", " ").capitalize())
    question = kind in QUESTIONS
    created_at = None
    subject = None
    preview = request.get("question") if question else None
    if ref["target"]["kind"] == "service":
        ms = runtime.maintenance.store
        raw = ms.db.get("service_decisions", ref["id"])
        if not raw or raw["revision"] != ref["revision"] or raw["state"] != "pending":
            return None
        created_at = raw.get("created_at")
        work_id = raw.get("work_id")
        candidate = raw.get("candidate") or {}
        subject = candidate.get("subject")
        if raw.get("discussion_id"):
            discussion = ms.db.get("service_discussions", raw["discussion_id"]) or {}
            subject = discussion.get("subject") or subject
        if raw.get("changeset_id") and not subject:
            row = ms.db.get("service_changesets", raw["changeset_id"]) or {}
            subject = row.get("change_request")
        if kind == "delivery_authority":
            action = ms.db.get("service_actions", raw["action_id"]) or {}
            plan = action.get("plan") or {}
            subject = plan.get("subject") or plan.get("existing") or subject
            if action.get("kind"):
                title = "Authorize " + action["kind"].replace("_", " ")
            preview = _preview(plan.get("title"), plan.get("body"))
            if not preview:
                preview = _preview(raw.get("reason"), plan.get("branch"), plan.get("commit"))
        if not preview:
            preview = _preview(candidate.get("title"), candidate.get("body"), candidate.get("plan"))
        if not subject and work_id:
            work = ms.db.get("service_work", work_id) or {}
            subject = work.get("subject")
    else:
        work_id = active_work["id"] if active_work else None
        subject = active_work.get("subject") if active_work else None
        if active_work and ref["target"]["id"] == active_work.get("run_id"):
            child = runtime.child_for(active_work)
            raw = child.run_store.runtime.get("run_requests", ref["id"]) or {}
            created_at = raw.get("created_at")
        if request.get("tool"):
            title = "Approve " + request["tool"]
    group = "work:" + work_id if work_id else "service"
    if isinstance(subject, dict) and type(subject.get("number")) is int:
        prefix = {"issue": "issue", "change_request": "pr"}.get(subject.get("kind"))
        if prefix:
            group = f"{prefix}:#{subject['number']}"
    return {
        "ref": ref,
        "kind": kind,
        "action_type": "question" if question else "approval",
        "state": request["state"],
        "group_key": group,
        "title": _display(title, 100),
        "preview": _display(preview or request.get("reason") or request.get("question") or "", 500),
        "expires_at": request.get("expires_at"),
        "created_at": created_at,
        **({"extra_required": bool(args["repair_attempts"]["extra_required"])} if kind == "ci_repair" else {}),
    }


def _result(items):
    for index, item in enumerate(items, 1):
        item["index"] = index
    actionable = [item for item in items if item["state"] == "pending"]
    counts = {
        "total": len(items),
        "actionable": len(actionable),
        "expired": len(items) - len(actionable),
        "approvals": sum(item["action_type"] == "approval" for item in items),
        "questions": sum(item["action_type"] == "question" for item in items),
        "by_kind": dict(sorted(Counter(item["kind"] for item in items).items())),
    }
    digest = fingerprint([(item["ref"]["id"], item["ref"]["revision"], item["state"]) for item in actionable])
    lines = [f"Inbox: {counts['actionable']} pending, {counts['expired']} expired (shown).", f"Snapshot: {digest}"]
    for item in items:
        label = item["action_type"] if item["state"] == "pending" else "expired " + item["action_type"]
        line = f"[#{item['index']}] {item['group_key']} | {label} | {item['title']}"
        if item["preview"]:
            line += " — " + _display(item["preview"], 160)
        if item.get("extra_required"):
            line += " (approval needs --allow-extra-attempt)"
        lines.append(line)
    if actionable:
        lines.append('Use inbox decide --snapshot DIGEST with --approve N, --deny N, or --answer \'N=TEXT\'.')
    if any(item["kind"] == "test_grant" for item in actionable):
        lines.append("Use renew-tests for Run test grants.")
    if counts["expired"]:
        lines.append("Use inbox refresh-expired to renew eligible service requests, then list again.")
    return {
        "items": items,
        "counts": counts,
        "oldest_created_at": min((item["created_at"] for item in items if item["created_at"] is not None), default=None),
        "digest": digest,
        "summary": "\n".join(lines),
    }


def build_inbox(runtime, *, work_id=None, include_expired=False, limit=50):
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"Inbox limit must be an integer from 1 to {MAX_ITEMS}.")
    if type(include_expired) is not bool:
        raise ValueError("include_expired must be Boolean.")
    if work_id is not None:
        text(work_id, "work_id")
    requests = runtime.requests(work_id)
    items = {}
    now = time.time()
    active_work = None
    if any(r["ref"]["target"]["kind"] == "run" for r in requests):
        active_work = runtime.store.work(work_id, runtime.id) if work_id else runtime.store.next_work(runtime.id)

    def add(request):
        state = request["state"]
        if state not in {"pending", "expired"}:
            return
        if expired(request.get("expires_at"), now):
            state = "expired"
        if state == "expired" and not include_expired:
            return
        identity = request["ref"]["id"]
        if identity not in items:
            item = _item(runtime, {**request, "state": state}, active_work)
            if item:
                items[identity] = item

    # The legacy list includes the active Run but may only include the first
    # service page (or fall back to expired service requests). Fill from pages
    # up to the same bound regardless of the requested display limit.
    for request in requests:
        add(request)
    if runtime.maintenance is not None:
        for is_expired in (False, True) if include_expired else (False,):
            cursor = 0
            state = "expired" if is_expired else "pending"
            while sum(item["state"] == state for item in items.values()) < MAX_ITEMS:
                page = request_views.page(
                    runtime.maintenance, work_id=work_id, cursor=cursor,
                    limit=MAX_ITEMS, expired=is_expired,
                )
                for request in page["items"]:
                    add(request)
                cursor = page["next_cursor"]
                if cursor is None:
                    break
    selected = sorted(
        items.values(),
        key=lambda item: (item["state"] != "pending", item["created_at"] is None, item["created_at"] or 0),
    )[:limit]
    result = _result(selected)
    # The preview and its summary can contain multibyte characters.
    budget = current_limits().control_frame_bytes - 1024
    while selected and len(json.dumps(result, ensure_ascii=False).encode()) > budget:
        selected.pop()
        result = _result(selected)
    return result


def _resolve_decisions(runtime, actions, work_id, snapshot, limit):
    from .delivery.batches import validate_decisions

    if not isinstance(actions, list) or not actions:
        raise ValueError("Supply at least one --approve, --deny, or --answer.")
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"Inbox limit must be an integer from 1 to {MAX_ITEMS}.")
    if work_id is not None:
        text(work_id, "work_id")
    exact = {}
    for action in actions:
        if (
            not isinstance(action, dict)
            or not {"token", "decision"} <= action.keys()
            or action.keys() - {"token", "decision", "text"}
            or not isinstance(action["decision"], str)
        ):
            raise ValueError("Invalid inbox decision action.")
        token = action["token"]
        if not isinstance(token, str) or not token.strip() or len(token) > 256:
            raise ValueError("Invalid inbox decision token.")
        if ":" in token:
            match = re.fullmatch(r"([^:]+):([0-9]+)", token)
            if not match:
                raise ValueError("Exact inbox references must use REQUEST_ID:REVISION.")
            exact[token] = (match[1], int(match[2]))
        elif snapshot is None:
            raise ValueError("Inbox indices and bare IDs require --snapshot DIGEST; otherwise use REQUEST_ID:REVISION.")

    indices = {}
    if snapshot is not None:
        current = build_inbox(runtime, work_id=work_id, limit=limit)
        if current["digest"] != snapshot:
            raise ValueError("Inbox changed since snapshot; list the inbox again.")
        pending = {item["ref"]["id"]: item for item in current["items"] if item["state"] == "pending"}
        indices = {str(item["index"]): identity for identity, item in pending.items()}
    else:
        # Exact refs don't depend on the rendered list's page or byte budget.
        pending = {request["ref"]["id"]: request for request in runtime.requests(work_id)}
        for identity, _ in exact.values():
            if identity not in pending:
                raw = runtime.store.db.get("service_decisions", identity)
                if raw and raw["service_id"] == runtime.id and raw["state"] == "pending":
                    request = request_views.project(runtime.maintenance, raw, work_id)
                    if request:
                        pending[identity] = request

    refs, decisions, extra = [], [], set()
    for action in actions:
        token = action["token"]
        identity, revision = exact[token] if token in exact else (indices.get(token, token), None)
        item = pending.get(identity)
        if (
            not item or item["state"] != "pending" or expired(item.get("expires_at"), time.time())
            or (revision is not None and item["ref"]["revision"] != revision)
        ):
            raise ValueError("No pending inbox request matches " + token + "; list the inbox again.")
        decision = action["decision"]
        if decision == "allow_extra_attempt":
            if item["kind"] != "ci_repair" or "text" in action:
                raise ValueError("Extra attempts apply only to CI repair requests.")
            extra.add(identity)
        else:
            refs.append(item["ref"])
            decisions.append({"id": identity, **{k: action[k] for k in ("decision", "text") if k in action}})
    if extra - {d["id"] for d in decisions if d["decision"] == "approve"}:
        raise ValueError("Extra attempts must name approved CI repair requests in this batch.")
    for decision in decisions:
        if decision["decision"] == "approve" and decision["id"] in extra:
            decision["allow_extra_attempt"] = True
    validate_decisions(decisions)
    return refs, decisions


def _complete(result, decisions):
    return all(result["results"].get(d["id"], {}).get("state") not in {None, "unknown"} for d in decisions)


def decide_inbox(runtime, *, actions, work_id=None, snapshot=None, limit=50, request_id):
    from .delivery.batches import DecisionBatches

    method = "inbox_decide"
    signature = fingerprint([method, {"actions": actions, "work_id": work_id, "snapshot": snapshot, "limit": limit}])
    key = request_key("command", runtime.id, request_id)
    batches = DecisionBatches(runtime, runtime.maintenance.store)
    db = runtime.store.db
    with db.write() as conn:
        meta = runtime.store.guard(conn, runtime.header)
        command = db._get(conn, "service_commands", key)
        if command:
            if command["payload"] != signature:
                raise ValueError("Control request_id has another command payload.")
            if _complete(command["result"], command["resolved_decisions"]):
                return command["result"]
        else:
            if meta["state"] in {"stopping", "stopped"}:
                raise ValueError("Service is stopping.")
            refs, decisions = _resolve_decisions(runtime, actions, work_id, snapshot, limit)
            # Commit the batch and its command together before applying any decision.
            batch = batches.create(refs, request_id, conn=conn, work_id=work_id)
            command = {
                "service_id": runtime.id,
                "method": method,
                "payload": signature,
                "batch_id": batch["id"],
                "revision": batch["revision"],
                "resolved_decisions": decisions,
                "result": {"batch_id": batch["id"], "results": {}, "complete": False, "request_id": request_id},
                "checkpoint": "service:" + runtime.id,
            }
            db._put(conn, "service_commands", key, command)
            # Execute exactly what replay will load, including storage redaction.
            command = db._get(conn, "service_commands", key)

    decisions = command["resolved_decisions"]
    result = batches.apply(command["batch_id"], command["revision"], decisions, request_id)
    result["complete"] = _complete(result, decisions)
    with db.write() as conn:
        runtime.store.guard(conn, runtime.header)
        db._put(conn, "service_commands", key, {**command, "result": result})
        return db._get(conn, "service_commands", key)["result"]


def _renew(maintenance, conn, request):
    ms = maintenance.store
    kind = request["kind"]
    if kind in {"work_authority", "work_information"}:
        work = ms.get("work", request["work_id"], conn)
        if work.get("cancel_requested") or work["state"] in {"cancelled", "rejected", "merged", "delivered"}:
            raise ValueError("Work is no longer active.")
        if (work.get("authority") or {}).get("revoked") or work.get("error") == "authority_revoked":
            raise ValueError("Work authority has been revoked.")
        if (
            request["requirements_revision"] != work["requirements_revision"]
            or work.get("triage_revision") != work["requirements_revision"]
            or work.get("triage") != request["candidate"]
        ):
            raise ValueError("Work requirements or plan changed.")
        renewed = ms.work_request(conn, work, work["triage"], renew=True)
        ms.changed(conn, work["id"])
        return renewed
    if kind in {"delivery_authority", "ci_repair"}:
        if maintenance.delivery is None:
            raise ValueError("Delivery is not configured.")
        from .delivery.store import scoped_repair

        delivery = maintenance.delivery.store
        row = delivery.get("changesets", request["changeset_id"], conn)
        if row["work_id"] != request["work_id"] or row["requirements_revision"] != request["requirements_revision"]:
            raise ValueError("Delivery request no longer matches the current Work.")
        ms.get("work", row["work_id"], conn)
        delivery.current(row, conn)
        if kind == "ci_repair":
            if (
                row["state"] != "repair_waiting"
                or not scoped_repair(row)
                or request["candidate"]["plan"] != row["ci_diagnosis"].get("plan")
                or (delivery.repair_decision(row, conn) or {}).get("id") != request["id"]
            ):
                raise ValueError("CI repair plan changed or is no longer waiting for approval.")
            return delivery.repair_request(conn, row, renew=True)
        action = delivery.get("actions", request["action_id"], conn)
        if row["state"] == "delivered" or delivery.merged_pr(conn, row):
            raise ValueError("The PR is already merged.")
        if (
            action["changeset_id"] != row["id"]
            or not row.get("candidate")
            or action["state"] not in {"awaiting_approval", "queued"}
            or action["plan_hash"] != request["plan_hash"]
            or delivery.action_basis(row, action["plan"]) != action["plan"]["hash"]
            or (action["kind"] == "merge_change_request" and row["state"] != "merge_ready")
        ):
            raise ValueError("Delivery action changed or is no longer waiting for approval.")
        return delivery.request(conn, row, action, renew=True)
    if kind == "communication_authority":
        if maintenance.communication is None:
            raise ValueError("Communication is not configured.")
        communication = maintenance.communication.store
        discussion = communication.get("discussions", request["discussion_id"], conn)
        action = communication.get("communication_actions", request["action_id"], conn)
        if (
            request["basis"] != communication.basis(discussion)
            or action["basis"] != request["basis"]
            or action["discussion_id"] != discussion["id"]
            or action["state"] != "awaiting_approval"
            or action["plan_hash"] != request["plan_hash"]
        ):
            raise ValueError("Discussion basis changed or its action is no longer awaiting approval.")
        communication.validate_anchors(discussion, action["plan"], conn)
        return communication.request_authority(conn, discussion, action)
    raise ValueError("This request kind has no inbox renewal path.")


def refresh_expired(runtime, *, ids=None, request_id):
    from .protocol import VERSION

    if ids is not None and (
        not isinstance(ids, list)
        or any(not isinstance(identity, str) or not 1 <= len(identity) <= 100 for identity in ids)
    ):
        raise ValueError("ids must be a list of request IDs.")
    maintenance = runtime.maintenance
    method = "inbox_refresh_expired"
    checkpoint = "service:" + runtime.id

    def apply(conn):
        meta = runtime.store.guard(conn, runtime.header)
        if meta["state"] in {"stopping", "stopped"}:
            raise ValueError("Service is stopping.")
        reply = {"refreshed": [], "skipped": [], "request_id": request_id, "has_more": False}
        # The receipt's HTTP envelope is larger than the direct command reply.
        # Its payload field is a fixed-width SHA-256 hex digest. Reserve the
        # longer spelling of has_more (false) when sizing the empty envelope.
        receipt = {
            "service_id": runtime.id, "method": method, "payload": "0" * 64,
            "result": reply, "checkpoint": checkpoint,
        }
        envelope = {"version": VERSION, "service_id": runtime.id, "ok": True, "result": receipt}

        def encoded_size(value):
            return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

        remaining = current_limits().control_frame_bytes - encoded_size(envelope)
        if remaining < 0:
            raise ValueError("Refresh receipt cannot fit in the control frame.")

        def append_if_fits(key, item):
            nonlocal remaining
            entries = reply[key]
            # Account for the default JSON list separator after the first item.
            size = encoded_size(item) + (2 if entries else 0)
            if size > remaining:
                reply["has_more"] = True
                return False
            entries.append(item)
            remaining -= size
            return True

        sql = (
            "SELECT value FROM records WHERE section='service_decisions' AND owner=? "
            "AND json_extract(value,'$.state')='pending' AND json_extract(value,'$.expires_at')<=?"
        )
        parameters = (checkpoint, time.time())
        if ids is not None:
            sql += " AND key IN (SELECT value FROM json_each(?))"
            parameters += (json.dumps(ids),)
        rows = conn.execute(sql + " ORDER BY rowid", parameters).fetchall()
        seen = set()
        for (raw,) in rows:
            request = json.loads(raw)
            identity = request["id"]
            seen.add(identity)
            # A native helper may derive a different identity (e.g. a new Work
            # grant round). Roll back that item rather than reopen another request.
            conn.execute("SAVEPOINT inbox_refresh_item")
            try:
                result = _renew(maintenance, conn, request)
                if result["id"] != identity or result["revision"] <= request["revision"]:
                    raise ValueError("The original request is no longer current.")
            except ValueError as error:
                conn.execute("ROLLBACK TO inbox_refresh_item")
                append_if_fits("skipped", {"id": identity, "kind": request["kind"], "reason": _display(str(error), 200)})
            else:
                if not append_if_fits("refreshed", {
                    "id": identity, "kind": result["kind"], "revision": result["revision"],
                    "state": result["state"], "expires_at": result["expires_at"],
                }):
                    conn.execute("ROLLBACK TO inbox_refresh_item")
            conn.execute("RELEASE inbox_refresh_item")
            if reply["has_more"]:
                return reply
        for identity in dict.fromkeys(ids or []):
            if identity not in seen and not append_if_fits("skipped", {
                "id": identity, "reason": "Not an expired pending service request.",
            }):
                break
        return reply

    return runtime.store.command(runtime.header, method, {"ids": ids}, request_id, apply)
