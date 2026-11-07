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
    goal = boundary.get("goal")
    if goal is not None:
        previous = db._get(conn, "goals", goal["goal_id"])
        if previous is None or previous.get("revision", 0) != boundary["goal_expected"]:
            raise ProposalSuperseded("Goal changed before completion commit.")
        db._put(conn, "goals", goal["goal_id"], goal)
    for receipt in boundary.get("receipts", ()):
        db._put(conn, "input_receipts", receipt["input_id"], receipt)
    for recipient, record in boundary.get("deliveries", ()):
        db._send(conn, recipient, record)
    end = boundary.get("end_turn")
    if end:
        recipient, turn = end
        current = db._get(conn, "input_turns", recipient)
        if current != turn:
            raise RecoveryRequired("Completion does not own the active user turn.")
        db._put(conn, "input_turns", recipient, None)
