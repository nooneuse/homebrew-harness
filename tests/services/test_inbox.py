"""Inbox ordering, exact mixed decisions and renewal over the service controls."""

import io
import json
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness import RemoteService
from homebrew_harness.cli import main
from homebrew_harness.contracts import SDKError
from homebrew_harness.runtime.limits import current_limits
from homebrew_harness.services.cli import attach
from homebrew_harness.services.protocol import fingerprint
from tests.runtime.test_interaction import eventually
from tests.services import test_maintenance as fixture


class InboxTests(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def seed(self, service, plan, *, disposition="accept", elapsed=None, subject=None):
        work_id = service.work.submit("Seed a fixed triage decision").id
        store = service.runtime.maintenance.store
        proposal = {**self.peer.triage, "disposition": disposition, "plan": plan}
        with store.db.write() as conn:
            work = store.get("work", work_id, conn)
            work.update(triage=proposal, triage_revision=work["requirements_revision"],
                        state="needs_info" if disposition == "needs_info" else "proposed")
            if subject:
                work["subject"] = subject
            store.put(conn, "work", work)
            request = store.work_request(conn, work, proposal)
            if elapsed is not None:
                request = store.put(conn, "decisions", {**request, "expires_at": time.time() - elapsed})
        return request

    def cli(self, service, *args):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["service", "--cwd", str(self.control), "inbox", service.id, *args]), 0)
        return output.getvalue()

    def test_summary_redaction_limits_and_cli_text_vs_json(self):
        service = self.start(auto=False)
        service.pause("all")
        old = self.seed(service, "Earlier expired plan", elapsed=60)
        pending = self.seed(service, "Review the change", subject={"kind": "issue", "number": 17})
        question = self.seed(service, "Clarify password=abcdefghijklmnop \x1b]52;c;bad \n" + "x" * 550,
                             disposition="needs_info")
        listed = service.inbox(include_expired=True)
        self.assertEqual([i["ref"]["id"] for i in listed["items"]], [pending["id"], question["id"], old["id"]])
        self.assertEqual([i["index"] for i in listed["items"]], [1, 2, 3])
        self.assertEqual(listed["counts"], {"total": 3, "actionable": 2, "expired": 1,
                                          "approvals": 2, "questions": 1,
                                          "by_kind": {"work_authority": 2, "work_information": 1}})
        self.assertEqual(listed["oldest_created_at"], old["created_at"])
        self.assertEqual(listed["digest"], fingerprint([(r["id"], r["revision"], "pending") for r in (pending, question)]))
        self.assertEqual(service.inbox()["digest"], listed["digest"])
        self.assertEqual(service.inbox(limit=1)["items"][0], listed["items"][0])
        self.assertEqual(service.inbox(question["work_id"])["items"][0]["ref"]["id"], question["id"])
        preview = listed["items"][1]["preview"]
        self.assertEqual(len(preview), 500)
        self.assertIn("[redacted]", preview)
        self.assertNotIn("abcdefghijklmnop", str(listed))
        self.assertNotIn("\x1b", listed["summary"])
        self.assertIn("[#1] issue:#17 | approval | Authorize Work", listed["summary"])
        self.assertIn("[#2] " + "work:" + question["work_id"] + " | question", listed["summary"])
        self.assertEqual(self.cli(service), service.inbox()["summary"] + "\n")
        self.assertEqual(json.loads(self.cli(service, "list", "--include-expired", "--json")), listed)

    def test_text_ui_decides_mixed_requests_by_index_with_maintenance_only(self):
        service = self.start(auto=False)
        service.pause("all")
        approved = self.seed(service, "Develop the first Work")
        denied = self.seed(service, "Develop the unneeded Work")
        answered = self.seed(service, "Which variant?", disposition="needs_info")
        lines = iter([
            "inbox", f"inbox decide --approve 1 --deny {denied['id']} --answer '3=variant = A' --request-id mixed",
            "exit",
        ])
        with RemoteService.from_root(self.control, control=True) as remote, redirect_stdout(io.StringIO()) as output:
            attach(remote, input_fn=lambda _: next(lines))
        self.assertIn("[#1]", output.getvalue())
        self.assertIn('"complete": true', output.getvalue())
        store = service.runtime.maintenance.store
        for request, state in ((approved, "approved"), (denied, "denied"), (answered, "answered")):
            self.assertEqual(store.get("decisions", request["id"])["state"], state)
        self.assertEqual(store.get("decisions", answered["id"])["answer"], "variant = A")
        self.assertEqual(service.inbox()["items"], [])

    def test_later_service_page_is_available_for_batch_creation(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "First decision")
        second = self.seed(service, "Second decision")
        limits = SimpleNamespace(control_frame_bytes=16390)
        with patch("homebrew_harness.services.request_views.current_limits", return_value=limits):
            self.assertEqual([r.ref.id for r in service.requests()], [first["id"]])
            items = service.inbox()["items"]
            self.assertEqual([item["ref"]["id"] for item in items], [first["id"], second["id"]])
            batch = service.batch([items[1]["ref"]], request_id="later-page")
        self.assertEqual(batch["items"], [items[1]["ref"]])

    def test_native_attention_is_answered_in_a_batch_over_http(self):
        self.peer.native_attention = "Which result should I use?"
        service = self.start()
        work = service.work.submit("Ask before choosing a result")
        with RemoteService.from_root(self.control, control=True) as remote:
            item = eventually(lambda: next((i for i in remote.inbox(work.id)["items"]
                                            if i["kind"] == "attention"), None), 20)
            self.assertEqual(item["action_type"], "question")
            self.assertIn("Which result", item["preview"])
            self.assertIsNotNone(item["created_at"])
            direct = remote.batch([item["ref"]], request_id="native-raw-group")
            self.assertEqual(direct["items"], [item["ref"]])
            request = next(r for r in remote.requests() if r.ref.id == item["ref"]["id"])
            self.assertEqual(request.ref.target.owner_id, service.id)
            batch = remote.batch([request], request_id="native-group")
            commands = ["service", "--cwd", str(self.control), "batch", service.id, "decide", batch["id"],
                        "--answer", item["ref"]["id"] + "=Use the original result", "--request-id", "native-answer"]
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(commands), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["results"][item["ref"]["id"]]["state"], "confirmed")
            self.assertEqual(result, remote.decide_batch(batch["id"], [
                {"id": item["ref"]["id"], "decision": "answer", "text": "Use the original result"},
            ], request_id="native-answer"))
            eventually(lambda: work.snapshot().state == "review", 20)

    def test_invalid_answer_payloads_do_not_partially_apply(self):
        service = self.start(auto=False)
        service.pause("all")
        pending = self.seed(service, "Can I proceed?")
        question = self.seed(service, "Which variant?", disposition="needs_info")
        batch = service.batch(service.requests())
        approve = {"id": pending["id"], "decision": "approve"}
        answer = {"id": question["id"], "decision": "answer"}
        for invalid in (answer, {**answer, "text": " "}, {**answer, "text": 42},
                        {**answer, "text": "x" * (current_limits().input_text_chars + 1)},
                        {**answer, "text": "yes", "allow_extra_attempt": False},
                        {**answer, "decision": "deny", "text": "no"},
                        {**answer, "decision": "approve", "text": "yes"}):
            with self.assertRaises(SDKError):
                service.decide_batch(batch["id"], [approve, invalid])
        self.assertEqual(service.inbox()["counts"]["actionable"], 2)

    def test_refresh_is_exact_replayable_and_skips_invalid_work(self):
        service = self.start(auto=False, authority_seconds=3600)
        service.pause("all")
        current = self.seed(service, "Still the current plan", elapsed=60)
        question = self.seed(service, "Still an open question", disposition="needs_info", elapsed=60)
        changed = self.seed(service, "Old plan", elapsed=60)
        revoked = self.seed(service, "Revoked plan", elapsed=60)
        cancelled = self.seed(service, "Cancelled plan", elapsed=60)
        superseded_round = self.seed(service, "Old grant round", elapsed=60)
        unselected = self.seed(service, "Not in this refresh", elapsed=60)
        service.work.get(changed["work_id"]).revise("The requirements have changed")
        store = service.runtime.maintenance.store
        for request, update in ((revoked, {"authority": {"revoked": True}}),
                                (cancelled, {"cancel_requested": True}),
                                (superseded_round, {"grant_round": 1})):
            service.store.update_work(service.runtime.header, request["work_id"], **update)
        selection = [r["id"] for r in (current, changed, question, revoked, cancelled, superseded_round)]
        reply = json.loads(self.cli(service, "refresh-expired", "--ids", *selection, "--request-id", "refresh-one"))
        self.assertEqual(reply, service.refresh_expired(selection, request_id="refresh-one"))
        self.assertEqual(reply, service.receipt("refresh-one")["result"])
        self.assertEqual([r["id"] for r in reply["refreshed"]], [current["id"], question["id"]])
        for request, renewed in zip((current, question), reply["refreshed"]):
            self.assertEqual(renewed["revision"], request["revision"] + 1)
            self.assertGreater(renewed["expires_at"], time.time())
            self.assertEqual(renewed["state"], "pending")
        self.assertEqual({r["id"] for r in reply["skipped"]}, {r["id"] for r in (changed, revoked, cancelled, superseded_round)})
        self.assertTrue(all(r["reason"] for r in reply["skipped"]))
        self.assertEqual(store.get("decisions", unselected["id"]), unselected)
        self.assertEqual(len(service.records("decisions")["items"]), len(selection) + 1)
        with self.assertRaises(SDKError):
            service.refresh_expired([], request_id="refresh-one")
        result = service.refresh_expired(request_id="refresh-all")
        self.assertEqual([r["id"] for r in result["refreshed"]], [unselected["id"]])
