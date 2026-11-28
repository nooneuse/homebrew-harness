"""PR-writing preferences and the opt-in boundary for custom publication."""

import json
import unittest

from tests.runtime.test_interaction import eventually
from tests.services.delivery import test_flow as fixture


class CustomPublication(unittest.TestCase):
    start = fixture.FlowTests.start
    run_git = fixture.FlowTests.run_git
    pending_target = fixture.FlowTests.pending_target

    def setUp(self):
        fixture.FlowTests.setUp(self)

    def test_guided_publication_uses_exact_text_and_retains_authority(self):
        publication = {"title": "[bug] Restore the value", "body": "## Summary\nRestore behavior.\n\n## Validation\nFixed unit-tests passed."}
        self.peer.review_extra = {"publication": publication}
        guidance = "Write publication with Summary and Validation sections."
        service = self.start(maintenance={"instructions": {"review": guidance}}, merge={"mode": "manual", "poll_seconds": 0.1})
        service.work.submit("Restore the documented value.")
        request = eventually(lambda: self.pending_target(service), 30)
        delivery = service.runtime.maintenance.delivery
        action = delivery.store.get("actions", request.arguments["action_id"])
        self.assertEqual(action["kind"], "ensure_change_request")
        self.assertEqual(action["state"], "awaiting_approval")
        self.assertIs(
            service.runtime.spec.delivery.get("auto_publish_pr_text", False), False
        )
        self.assertIsNone(action["authorization"])
        self.assertEqual(action["plan"]["title"], publication["title"])
        self.assertEqual(action["plan"]["body"], publication["body"])
        self.assertEqual(len(self.peer.pulls), 0)
        payloads = [json.loads(m["content"]) for r in self.peer.model_requests for m in r["messages"] if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
        self.assertEqual(next(p for p in payloads if p["job"]["kind"] == "review")["policy"]["instructions"], {"review": guidance})
        service.decide(request, "approve", decision_id="publish-exact-draft")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        self.assertEqual(self.peer.pulls[0]["title"], publication["title"])
        self.assertEqual(self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], publication["body"])

    def test_closing_keywords_opt_in_publishes_exact_body_after_approval(self):
        publication = {"title": "Restore the value", "body": "Fixes #1\n\nFixed unit tests passed."}
        self.peer.review_extra = {"publication": publication}
        service = self.start(allow_closing_keywords=True, merge={"mode": "manual"})
        service.work.submit("Restore the documented value.")
        request = eventually(lambda: self.pending_target(service), 30)
        inbox = service.inbox()
        item = next(item for item in inbox["items"] if item["ref"]["id"] == request.ref.id)
        self.assertIn("Restore the value Fixes #1", item["preview"])
        self.assertEqual(self.peer.pulls, [])
        import time

        store = service.runtime.maintenance.delivery.store
        old = store.update("decisions", request.ref.id, expires_at=time.time() - 1)
        refreshed = service.refresh_expired([request.ref.id], request_id="refresh-pr-text")
        self.assertEqual(refreshed["skipped"], [])
        self.assertEqual(refreshed["refreshed"][0]["revision"], old["revision"] + 1)
        self.assertEqual(self.peer.pulls, [])
        request = self.pending_target(service)
        service.decide(request, "approve")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        self.assertEqual(self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], publication["body"])

    def test_explicit_wording_without_stored_wording_requires_approval(self):
        service = self.start(merge={"mode": "manual"})
        work = service.work.submit("Restore the documented value.")
        store = service.runtime.maintenance.delivery.store
        fallback = eventually(
            lambda: next(
                (
                    action for action in service.records("actions")["items"]
                    if action["kind"] == "ensure_change_request"
                    and action["state"] == "confirmed"
                ),
                None,
            ),
            40,
        )
        service.pause("all")
        row = store.get("changesets", fallback["changeset_id"])
        self.assertFalse(row.get("publication"))
        self.assertNotIn("publication", row["review"])
        self.assertEqual(fallback["authorization"], "host_policy")
        self.assertEqual(fallback["plan"]["title"], "Maintain " + work.id)

        publication = {"title": "Custom title", "body": "Describe the reviewed change."}
        for draft in (False, True):
            with self.subTest(draft=draft):
                explicit = service.plan_delivery(
                    row["id"], "ensure_change_request", draft=draft, publication=publication
                )
                self.assertEqual(explicit["state"], "awaiting_approval")
                self.assertIsNone(explicit["authorization"])
                self.assertEqual(explicit["attempts"], 0)
                self.assertEqual({k: explicit["plan"][k] for k in publication}, publication)
                self.assertIs(explicit["explicit_publication"], True)
                self.assertNotIn("context_hash", explicit)
                decision = next(request for request in service.requests(work.id)
                                if request.arguments.get("action_id") == explicit["id"])
                self.assertIs(decision.arguments["explicit_publication"], True)
                self.assertIs(store.get("actions", explicit["id"])["explicit_publication"], True)
                self.assertIs(store.get("decisions", decision.ref.id)["explicit_publication"], True)
        self.assertEqual(self.peer.pulls[0]["title"], fallback["plan"]["title"])

    def test_validation_plan_draft_retains_host_policy_authorization(self):
        from unittest.mock import patch

        from homebrew_harness.services.delivery.runtime import Delivery

        self.peer.validation_extra = {
            "draft": {"title": "Draft the scoped change", "body": "Verification is pending."}
        }
        # This peer has no GitHub Draft API; hold execution at the queued boundary.
        with patch.object(Delivery, "execute"):
            service = self.start(merge={"mode": "manual"})
            service.work.submit("Restore the documented value.")
            draft = eventually(lambda: next((a for a in service.records("actions")["items"]
                                             if a["kind"] == "ensure_change_request" and a["plan"].get("draft")), None), 40)
            service.pause("all")
        self.assertEqual(draft["state"], "queued")
        self.assertEqual(draft["authorization"], "host_policy")
        self.assertIsNot(draft.get("explicit_publication"), True)
        self.assertNotIn("context_hash", draft)
        self.assertNotIn(draft["id"], {r.arguments.get("action_id") for r in service.requests()})

    def test_opt_in_publishes_review_text_but_does_not_authorize_explicit_plans(self):
        publication = {
            "title": "Restore the documented value",
            "body": "Restore behavior and preserve the unit-test contract.",
        }
        self.peer.review_extra = {"publication": publication}
        service = self.start(auto_publish_pr_text=True, merge={"mode": "manual"})
        service.work.submit("Restore the documented value.")
        confirmed = eventually(
            lambda: next(
                (
                    action
                    for action in service.records("actions")["items"]
                    if action["kind"] == "ensure_change_request"
                    and action["state"] in {"confirmed", "awaiting_approval"}
                ),
                None,
            ),
            40,
        )
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["authorization"], "host_policy")
        self.assertIs(confirmed["plan"].get("draft", False), False)
        self.assertEqual(len(self.peer.pulls), 1)
        self.assertEqual(self.peer.pulls[0]["title"], publication["title"])
        self.assertEqual(
            self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], publication["body"]
        )
        self.assertFalse(
            any(job["kind"] == "publication" for job in service.records("jobs")["items"])
        )

        service.pause("all")
        changeset = service.delivery(confirmed["changeset_id"])
        self.assertEqual(changeset["review"]["publication"], publication)
        draft = service.plan_delivery(
            changeset["id"], "ensure_change_request", draft=True, publication=publication
        )
        self.assertIs(draft["plan"]["draft"], True)
        self.assertEqual(draft["state"], "awaiting_approval")
        self.assertIsNone(draft["authorization"])
        request = self.pending_target(service)
        self.assertIsNotNone(request)
        self.assertEqual(request.arguments["action_id"], draft["id"])

        # An explicit plan still needs approval if its wording is identical to
        # that of an implicitly authorized plan for the same candidate.
        implicit = service.plan_delivery(changeset["id"], "ensure_change_request")
        self.assertEqual(implicit["authorization"], "host_policy")
        for wording in (publication, {"title": "Explicit override", "body": "Override body"}):
            with self.subTest(title=wording["title"]):
                explicit = service.plan_delivery(
                    changeset["id"], "ensure_change_request", publication=wording
                )
                self.assertEqual({k: explicit["plan"][k] for k in wording}, wording)
                self.assertNotEqual(explicit["id"], implicit["id"])
                self.assertEqual(explicit["state"], "awaiting_approval")
                self.assertIsNone(explicit["authorization"])
                self.assertEqual(explicit["attempts"], 0)
                self.assertIn(explicit["id"], {
                    r.arguments["action_id"] for r in service.requests()
                    if r.kind == "delivery_authority" and r.state == "pending"
                })


if __name__ == "__main__":
    unittest.main()
