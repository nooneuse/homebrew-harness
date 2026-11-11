"""Host-declared reviewer authority applies to effective decisions at one head."""


def check(feedback, policy):
    trusted = set(policy["reviewer_ids"])
    latest = {}
    for row in feedback.get("reviews", []):
        if row.get("actor_id") not in trusted or row.get("head") != feedback["head"]:
            continue
        state = row.get("status", "").upper()
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        key = (
            row.get("version") or "",
            int(row["id"]) if str(row.get("id", "")).isdecimal() else 0,
        )
        actor = row["actor_id"]
        if actor not in latest or key > latest[actor][0]:
            latest[actor] = (key, state)
    blocking = sorted(
        actor for actor, (_, state) in latest.items() if state == "CHANGES_REQUESTED"
    )
    approvals = sorted(
        actor for actor, (_, state) in latest.items() if state == "APPROVED"
    )
    return {
        "ready": not blocking and len(approvals) >= policy["required_approvals"],
        "blocking_reviewers": blocking,
        "approved_reviewers": approvals,
        "required_approvals": policy["required_approvals"],
    }
