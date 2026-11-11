"""PR-writing preferences reach review and a custom draft still needs approval."""

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
        self.assertEqual(action["plan"]["title"], publication["title"])
        self.assertEqual(action["plan"]["body"], publication["body"])
        self.assertEqual(len(self.peer.pulls), 0)
        payloads = [json.loads(m["content"]) for r in self.peer.model_requests for m in r["messages"] if m.get("role") == "user" and m.get("content", "").startswith('{"job":')]
        self.assertEqual(next(p for p in payloads if p["job"]["kind"] == "review")["policy"]["instructions"], {"review": guidance})
        service.decide(request, "approve", decision_id="publish-exact-draft")
        eventually(lambda: len(self.peer.pulls) == 1, 15)
        self.assertEqual(self.peer.pulls[0]["title"], publication["title"])
        self.assertEqual(self.peer.pulls[0]["body"].split("\n\n<!--", 1)[0], publication["body"])


if __name__ == "__main__":
    unittest.main()
