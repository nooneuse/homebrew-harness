"""Small control-plane additions to the existing checkpoint transaction."""

from homebrew_harness.runtime.runtime_state import RecoveryRequired


class ProposalSuperseded(RuntimeError):
    """No action was accepted; a newer user revision owns the next decision."""


def check_guard(db, conn, guard):
    for recipient, expected in (guard or {}).items():
        actual = db._get(conn, "input_epochs", recipient, 0)
        if actual != expected:
            raise ProposalSuperseded(
                "User input superseded this proposal before acceptance."
            )


def apply_boundary(db, conn, boundary):
    if not boundary:
        return
    check_guard(db, conn, boundary.get("guard"))
    resume = boundary.get("resume")
    if resume:
        run = db._get(conn, "runs", resume["run_id"])
        control = db._get(conn, "run_controls", resume["run_id"])
        if (
            not run
            or (run.get("resume_request") or {}).get("id") != resume["id"]
            or control["action"] != "run"
            or control["revision"] != resume["control_revision"]
        ):
            raise ProposalSuperseded(
                "Resume confirmation changed before checkpoint commit."
            )
    if boundary.get("managed"):
        from homebrew_harness.runs.store import commit_run

        commit_run(db, conn, boundary["managed"])
    if boundary.get("member_outcome"):
        from homebrew_harness.runs.team import commit_member_outcome

        commit_member_outcome(db, conn, boundary["member_outcome"])
    goal = boundary.get("goal")
    if goal is not None:
        previous = db._get(conn, "goals", goal["goal_id"])
        if previous is None or previous.get("revision", 0) != boundary["goal_expected"]:
            raise ProposalSuperseded("Goal changed before completion commit.")
        db._put(conn, "goals", goal["goal_id"], goal)
    for receipt in boundary.get("receipts", ()):
        db._put(conn, "input_receipts", receipt["input_id"], receipt)
        if receipt["state"] == "referenced":
            event = db._get(conn, "run_events", receipt["input_id"])
            if event:
                db._put(
                    conn,
                    "run_events",
                    receipt["input_id"],
                    {**event, "state": "applied", "revision": event["revision"] + 1},
                )
    for recipient, record in boundary.get("deliveries", ()):
        db._send(conn, recipient, record)
    end = boundary.get("end_turn")
    if end:
        recipient, turn = end
        current = db._get(conn, "input_turns", recipient)
        if current != turn:
            raise RecoveryRequired("Completion does not own the active user turn.")
        db._put(conn, "input_turns", recipient, None)
