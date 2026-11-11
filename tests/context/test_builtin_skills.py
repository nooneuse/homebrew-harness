"""Installed hosting guidance is discoverable without widening tool authority."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness.context.skill_loader import SkillLoader
from homebrew_harness.runtime.session import HarnessSession
from tests.runtime.test_interaction import InteractionHost, LocalAssembler, eventually
from tests.support.clients import call, fake_client, reply


class BuiltinSkillsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def local(self, name, folder=None):
        target = self.root / "skills" / (folder or name) / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\nname: " + name + "\ndescription: Local fixture\n---\nLOCAL_BODY")

    def test_builtin_catalog_combines_with_local_and_loads_only_on_demand(self):
        self.local("local-guide")
        loader = SkillLoader(self.root / "skills", include_builtin=True)
        self.assertEqual(set(loader.skills), {"host-project", "local-guide"})
        self.assertIn("帮我托管这个项目", loader.catalog())
        self.assertNotIn("PROJECT_PUBLISH_TOKEN", loader.catalog())
        self.assertIn("PROJECT_PUBLISH_TOKEN", loader.load("host-project"))

    def test_explicit_python_loader_keeps_its_selected_catalog(self):
        self.assertEqual(dict(SkillLoader(self.root / "missing").skills), {})
        client, requests = fake_client([reply("No implicit skill")])
        with HarnessSession(client, "fixture", self.root, skill_loader=SkillLoader(self.root / "missing")) as session:
            session.run_user("Show skills")
        self.assertNotIn("host-project", requests[0][0]["content"])

    def test_local_collision_does_not_replace_builtin_guidance(self):
        self.local("host-project")
        with redirect_stderr(io.StringIO()):
            loader = SkillLoader(self.root / "skills", include_builtin=True)
        self.assertNotIn("host-project", loader.skills)

    def test_automatic_entry_loads_hosting_skill_and_can_collect_choices(self):
        client, requests = fake_client([
            reply(calls=[call("hosting", name="load_skill", arguments=json.dumps({"name": "host-project"}))]),
            reply("Which project objectives and deployment directory should I use?"),
        ])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            host = InteractionHost(LocalAssembler(self.root, client))
            try:
                accepted = host.submit("帮我托管这个项目，处理 issue 并通过 PR 维护代码")
                value = eventually(lambda: host.state.get(accepted["task_id"]) if host.state.get(accepted["task_id"])["state"] == "finished" else None)
                self.assertEqual(value["target"]["kind"], "session")
                self.assertIn("project objectives", value["result"])
                self.assertNotIn("PROJECT_PUBLISH_TOKEN", json.dumps(requests[0]))
                self.assertIn("PROJECT_PUBLISH_TOKEN", json.dumps(requests[1]))
                self.assertFalse((self.root / ".harness/control").exists())
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
