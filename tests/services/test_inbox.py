"""Inbox ordering, exact mixed decisions and renewal over the service controls."""

import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from homebrew_harness import Harness, HostConfig, RemoteService, ServiceSpec
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

    def cli_error(self, service, *args):
        with redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["service", "--cwd", str(self.control), "inbox", service.id, *args]), 1)
        return output.getvalue()

    def set_frame_limit(self, size):
        self.host.close()
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(
                env_file=self.env, shutdown_seconds=5, runtime_limits={"control_frame_bytes": size},
            ),
        )
        self.addCleanup(self.host.close)

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
        self.assertIn(f"Snapshot: {listed['digest']}", listed["summary"])
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
        self.seed(service, "Expired plan", elapsed=60)
        lines = iter([
            "inbox list --include-expired", f"inbox decide --approve 1 --deny {denied['id']} --answer '3=variant = A' --request-id mixed",
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

    def test_text_ui_reuses_snapshot_scope_and_limit_after_relisting(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "Use the originally listed decision")
        second = self.seed(service, "Do not decide the later listing")
        decide = "inbox decide --approve 1 --request-id attach-replay"
        lines = iter([
            f"inbox list --work-id {first['work_id']} --limit 1",
            decide,
            f"inbox list --work-id {second['work_id']} --limit 2",
            decide,
            "exit",
        ])
        with RemoteService.from_root(self.control, control=True) as remote, redirect_stdout(io.StringIO()) as output:
            attach(remote, input_fn=lambda _: next(lines))
        receipt = service.receipt("attach-replay")["result"]
        self.assertEqual(output.getvalue().count(json.dumps(receipt, ensure_ascii=False, indent=2)), 2)
        self.assertEqual(list(receipt["results"]), [first["id"]])
        self.assertEqual([r.ref.id for r in service.requests()], [second["id"]])

    def test_text_ui_exact_answer_ignores_stale_scoped_snapshot(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "Resolve the listed decision separately")
        second = self.seed(service, "Which variant?", disposition="needs_info")

        def commands():
            yield f"inbox list --work-id {first['work_id']} --limit 1"
            request = service.requests(first["work_id"])[0]
            service.decide(request, "approve")
            yield f"inbox decide --answer '{second['id']}:{second['revision']}=Use A' --request-id exact-attach"
            yield "exit"

        lines = commands()
        with RemoteService.from_root(self.control, control=True) as remote, redirect_stdout(io.StringIO()) as output:
            attach(remote, input_fn=lambda _: next(lines))
        self.assertIn('"complete": true', output.getvalue())
        receipt = service.receipt("exact-attach")
        self.assertEqual(receipt["result"]["results"][second["id"]]["state"], "confirmed")
        self.assertEqual(service.runtime.maintenance.store.get("decisions", second["id"])["answer"], "Use A")

    def test_short_indices_cannot_approve_a_request_that_shifted_into_view(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "First decision")
        second = self.seed(service, "Second decision")
        listing = json.loads(self.cli(service, "list", "--json"))
        self.assertEqual([item["ref"]["id"] for item in listing["items"]], [first["id"], second["id"]])
        first_ref = next(r.ref for r in service.requests() if r.ref.id == first["id"])
        stale = "Inbox changed since snapshot; list the inbox again."

        def commands():
            yield "inbox"
            service.decide(first_ref, "approve")
            yield "inbox decide --approve 1"
            yield "exit"

        lines = commands()
        with RemoteService.from_root(self.control, control=True) as remote, redirect_stdout(io.StringIO()) as output:
            attach(remote, input_fn=lambda _: next(lines))
        self.assertIn(f"Snapshot: {listing['digest']}", output.getvalue())
        self.assertIn(stale, output.getvalue())
        self.assertIn("require --snapshot", self.cli_error(service, "decide", "--approve", "1"))
        self.assertIn(stale, self.cli_error(service, "decide", "--approve", "1", "--snapshot", listing["digest"]))
        remaining = service.inbox()["items"]
        self.assertEqual([(r["index"], r["ref"]["id"], r["state"]) for r in remaining], [(1, second["id"], "pending")])

    def test_inbox_exact_ref_does_not_require_a_snapshot(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "First decision")
        second = self.seed(service, "Second decision")
        self.assertIn("require --snapshot", self.cli_error(service, "decide", "--approve", second["id"]))
        self.assertIn("No pending inbox request matches", self.cli_error(
            service, "decide", "--approve", f"{second['id']}:{second['revision'] + 1}",
        ))
        result = json.loads(self.cli(service, "decide", "--limit", "1", "--approve", f"{second['id']}:{second['revision']}"))
        self.assertEqual(result["results"][second["id"]]["state"], "confirmed")
        self.assertEqual([r["ref"]["id"] for r in service.inbox()["items"]], [first["id"]])

    def test_inbox_decisions_replay_after_exact_and_snapshot_requests_leave_inbox(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "Exact decision")
        second = self.seed(service, "Snapshot decision")
        outside_limit = self.seed(service, "Leave outside the bounded snapshot")
        for request, request_id in ((first, "replay-exact"), (second, "replay-snap")):
            with self.subTest(request_id=request_id):
                if request is first:
                    token, args = f"{first['id']}:{first['revision']}", ()
                else:
                    token, args = "1", ("--snapshot", service.inbox(limit=1)["digest"], "--limit", "1")
                args = ("decide", "--request-id", request_id, *args)
                reply = self.cli(service, *args, "--approve", token)
                result = json.loads(reply)
                self.assertEqual(result["results"][request["id"]]["state"], "confirmed")
                self.assertNotIn(request["id"], [r["ref"]["id"] for r in service.inbox()["items"]])
                self.assertEqual(json.loads(self.cli(service, *args, "--approve", token)), result)
                self.assertEqual(service.receipt(request_id)["result"], result)
                self.assertIn("Control request_id has another command payload.",
                              self.cli_error(service, *args, "--deny", token))
        self.assertEqual([r["ref"]["id"] for r in service.inbox()["items"]], [outside_limit["id"]])

    def test_inbox_replays_frozen_batch_when_a_decision_outcome_is_unknown(self):
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "First decision")
        second = self.seed(service, "Second decision")
        args = ("decide", "--snapshot", service.inbox()["digest"], "--approve", "1", "2", "--request-id", "partial")
        dispatch = service.runtime.dispatch

        def lose_decision_response(identity, method, payload, request_id=None):
            result = dispatch(identity, method, payload, request_id)
            if method == "service_decide" and payload["id"] == second["id"]:
                raise RuntimeError("Lost decision response after commit")
            return result

        with patch.object(service.runtime, "dispatch", side_effect=lose_decision_response):
            partial = json.loads(self.cli(service, *args))
        self.assertEqual(service.inbox()["items"], [])
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["results"][first["id"]]["state"], "confirmed")
        self.assertEqual(partial["results"][second["id"]]["state"], "unknown")
        result = json.loads(self.cli(service, *args))
        self.assertTrue(result["complete"])
        self.assertEqual(result["batch_id"], partial["batch_id"])
        self.assertEqual(result["results"][first["id"]], partial["results"][first["id"]])
        self.assertEqual(result["results"][second["id"]]["state"], "confirmed")
        self.assertEqual(service.receipt("partial")["result"], result)

    def test_batch_accepts_a_full_length_request_id(self):
        service = self.start(auto=False)
        service.pause("all")
        request = self.seed(service, "Approve under a long idempotency key")
        batch = service.batch(service.requests())
        decisions = [{"id": request["id"], "decision": "approve"}]
        request_id = "a" * 256
        result = service.decide_batch(batch["id"], decisions, request_id=request_id)
        self.assertEqual(result["results"][request["id"]]["state"], "confirmed")
        self.assertEqual(service.decide_batch(batch["id"], decisions, request_id=request_id), result)

    def test_http_receipt_does_not_expose_frozen_answer_text(self):
        self.set_frame_limit(2048)
        service = self.start(auto=False)
        service.pause("all")
        request = self.seed(service, "Choose a detailed approach", disposition="needs_info")
        answer = "a" * 1550
        actions = [{"token": f"{request['id']}:{request['revision']}", "decision": "answer", "text": answer}]
        with RemoteService.from_root(self.control, control=True) as remote:
            result = remote.decide_inbox(actions, request_id="long-answer")
            self.assertEqual(result["results"][request["id"]]["state"], "confirmed")
            receipt = remote.receipt("long-answer")
            self.assertEqual(set(receipt), {"service_id", "method", "payload", "result", "checkpoint"})
            self.assertEqual(receipt["result"], result)
            self.assertEqual(remote.decide_inbox(actions, request_id="long-answer"), result)
        self.assertEqual(service.runtime.maintenance.store.get("decisions", request["id"])["answer"], answer)

    def test_small_local_and_remote_frames_use_the_same_inbox_snapshot(self):
        self.set_frame_limit(1024)
        service = self.start(auto=False)
        service.pause("all")
        first = self.seed(service, "First decision", subject={"kind": "issue", "number": 1})
        self.seed(service, "Second decision")
        listed = service.inbox()
        self.assertEqual([item["ref"]["id"] for item in listed["items"]], [first["id"]])
        with RemoteService.from_root(self.control, control=True) as remote:
            self.assertEqual(remote.inbox(), listed)
        result = service.decide_inbox([{"token": "1", "decision": "approve"}], snapshot=listed["digest"])
        self.assertEqual(result["results"][first["id"]]["state"], "confirmed")

    def test_manual_inbox_advertises_exact_controls_instead_of_inbox_decide(self):
        self.peer.native_attention = "Which behavior?"
        service = self.host.services.start(ServiceSpec(str(self.workspace), profile="manual"))
        service.work.submit("Ask before making changes")
        inbox = eventually(lambda: (view if (view := service.inbox())["items"] else None), 15)
        self.assertEqual(inbox["items"][0]["kind"], "attention")
        self.assertIn("Use approve, deny, or answer with the exact request ID and revision", inbox["summary"])
        self.assertNotIn("inbox decide", inbox["summary"])

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
        self.assertFalse(reply["has_more"])
        self.assertIsNone(reply["next_cursor"])
        result = service.refresh_expired(request_id="refresh-all")
        self.assertEqual([r["id"] for r in result["refreshed"]], [unselected["id"]])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def start_with_refresh_frame(self, frame_bytes):
        from homebrew_harness import Harness, HostConfig

        self.host.close()
        self.host = Harness(
            workdir=self.control,
            config=HostConfig.from_files(
                env_file=self.env, shutdown_seconds=5, runtime_limits={"control_frame_bytes": frame_bytes},
            ),
        )
        self.addCleanup(self.host.close)
        service = self.start(auto=False, authority_seconds=3600)
        service.pause("all")
        return service

    def test_refresh_fits_remote_control_frame_and_rolls_back_overflow(self):
        service = self.start_with_refresh_frame(2048)
        requests = [self.seed(service, f"Plan {i}", elapsed=60) for i in range(20)]
        store = service.runtime.maintenance.store
        activity = service.store.get(service.id).get("activity", 0)
        with RemoteService.from_root(self.control, control=True) as remote:
            first = remote.refresh_expired(request_id="batch-1")
            self.assertTrue(first["has_more"])
            self.assertTrue(first["next_cursor"])
            self.assertEqual(first["skipped"], [])
            count = len(first["refreshed"])
            self.assertTrue(0 < count < len(requests))
            self.assertEqual(first, remote.receipt("batch-1")["result"])
            self.assertEqual(first, remote.refresh_expired(request_id="batch-1"))
            with self.assertRaisesRegex(SDKError, "another command payload"):
                remote.refresh_expired(request_id="batch-1", cursor=first["next_cursor"])
            self.assertEqual([r["id"] for r in first["refreshed"]], [r["id"] for r in requests[:count]])
            self.assertEqual(service.store.get(service.id)["activity"], activity + count)
            for original, renewed in zip(requests, first["refreshed"]):
                saved = store.get("decisions", original["id"])
                self.assertEqual(saved["revision"], original["revision"] + 1)
                self.assertEqual(saved["expires_at"], renewed["expires_at"])
            for original in requests[count:]:
                self.assertEqual(store.get("decisions", original["id"]), original)

            renewed_ids = [r["id"] for r in first["refreshed"]]
            page = first
            for index in range(2, len(requests) + 1):
                if not page["has_more"]:
                    break
                options = {"request_id": f"batch-{index}", "cursor": page["next_cursor"]}
                page = remote.refresh_expired(**options)
                self.assertTrue(page["refreshed"])
                self.assertEqual(page, remote.receipt(options["request_id"])["result"])
                self.assertEqual(page, remote.refresh_expired(**options))
                renewed_ids.extend(r["id"] for r in page["refreshed"])
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_cursor"])
            self.assertEqual(renewed_ids, [r["id"] for r in requests])

            # These IDs fit in the request, but the skipped entries' UTF-8 bytes
            # plus receipt metadata cannot all fit in one response. Continue with
            # the full selection; duplicate and no-longer-expired IDs count once.
            missing = ["界" * 29 + str(i) for i in range(17)]
            last = self.seed(service, "Selected expired request after missing IDs", elapsed=60)
            expected_skipped = [requests[0]["id"], *missing]
            selection = [*expected_skipped, missing[0], last["id"]]
            skipped = remote.refresh_expired(selection, request_id="skip-bounded")
            self.assertTrue(skipped["has_more"])
            self.assertEqual(skipped["refreshed"], [])
            count = len(skipped["skipped"])
            self.assertTrue(0 < count < len(expected_skipped))
            self.assertEqual([r["id"] for r in skipped["skipped"]], expected_skipped[:count])
            self.assertEqual(skipped, remote.receipt("skip-bounded")["result"])
            cursor = skipped["next_cursor"]
            for invalid in (None, True, "1", -1, 2**63, len(selection) + 1):
                with self.assertRaisesRegex(SDKError, "Invalid refresh cursor"):
                    remote.refresh_expired(selection, cursor=invalid, request_id="bad-cursor")
            rest = json.loads(self.cli(
                service, "refresh-expired", "--ids", *selection, "--cursor", str(cursor), "--request-id", "skip-rest",
            ))
            self.assertFalse(rest["has_more"])
            self.assertIsNone(rest["next_cursor"])
            self.assertEqual([r["id"] for r in rest["skipped"]], expected_skipped[count:])
            self.assertEqual([r["id"] for r in rest["refreshed"]], [last["id"]])
            self.assertEqual(rest, remote.receipt("skip-rest")["result"])

    def test_refresh_cursor_advances_past_a_page_of_unrenewable_requests(self):
        service = self.start_with_refresh_frame(1024)
        revoked = [self.seed(service, f"Revoked plan {i}", elapsed=60) for i in range(8)]
        for request in revoked:
            service.store.update_work(service.runtime.header, request["work_id"], authority={"revoked": True})
        eligible = self.seed(service, "Still the current plan", elapsed=60)
        with RemoteService.from_root(self.control, control=True) as remote:
            page = remote.refresh_expired(request_id="scan-0")
            self.assertTrue(page["has_more"])
            self.assertTrue(page["skipped"])
            self.assertEqual(page["refreshed"], [])
            skipped = list(page["skipped"])
            refreshed = []
            for i in range(1, len(revoked) + 2):
                if not page["has_more"]:
                    break
                page = remote.refresh_expired(request_id=f"scan-{i}", cursor=page["next_cursor"])
                skipped.extend(page["skipped"])
                refreshed.extend(page["refreshed"])
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_cursor"])
        self.assertEqual([item["id"] for item in skipped], [r["id"] for r in revoked])
        self.assertEqual([item["id"] for item in refreshed], [eligible["id"]])

    def test_refresh_budgets_redacted_bytes_for_response_and_receipt(self):
        from homebrew_harness.security import data_boundary

        self.start_with_refresh_frame(2048)
        private = "redactme"
        missing = [private * 12 + str(i) for i in range(12)]
        expected_ids = ["[redacted]" * 12 + str(i) for i in range(len(missing))]
        returned_ids = []
        cursor = 0
        with RemoteService.from_root(self.control, control=True) as remote:
            with patch.object(data_boundary, "_VALUES", data_boundary._VALUES | {private}):
                for i in range(len(missing)):
                    request_id = private * 31 + f"-{i}"
                    options = {"request_id": request_id, "cursor": cursor}
                    page = remote.refresh_expired(missing, **options)
                    self.assertEqual(page["request_id"], "[redacted]" * 31 + f"-{i}")
                    self.assertEqual(page["refreshed"], [])
                    self.assertTrue(page["skipped"])
                    self.assertEqual(page, remote.receipt(request_id)["result"])
                    self.assertEqual(page, remote.refresh_expired(missing, **options))
                    if i == 0:
                        self.assertTrue(page["has_more"])
                    returned_ids.extend(item["id"] for item in page["skipped"])
                    cursor = page["next_cursor"]
                    if not page["has_more"]:
                        break
        self.assertIsNone(cursor)
        self.assertEqual(returned_ids, expected_ids)
