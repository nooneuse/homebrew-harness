"""Task completion, effect certainty, ownership and resource accounting."""

import json
import threading
import unittest
import uuid
from unittest.mock import patch

from tests.orchestration import test_subagent as fixture
from tests.support.clients import reply, call
from homebrew_harness.runtime.turns import run_subagent
from homebrew_harness.orchestration.subagent_results import read_result
from homebrew_harness.storage.durable_runtime import DurableStore, CheckpointWriter, checkpoint_scope, track_activity, settle_activity
from homebrew_harness.tools.tool_outcome import needs_inspection, ToolExecutionError


class SubagentResultTests(unittest.TestCase):
    setUp = fixture.SubagentTests.setUp
    run_child = fixture.SubagentTests.run_child
    run_parent = fixture.SubagentTests.run_parent

    def test_incomplete_queries_have_equal_certainty_and_are_not_unexecuted(self):
        (self.workdir / "sample.txt").write_text("needle\n")
        cases = [("read_file", {"path":"sample.txt"}), ("search_text", {"text":"needle","pattern":"*.txt"}), ("glob", {"pattern":"*.txt"})]
        for name,args in cases:
            with self.subTest(tool=name):
                output,_ = self.run_child([reply(calls=[call("query",name=name,arguments=json.dumps(args))])],max_turns=1)
                value=json.loads(output)
                self.assertEqual(value["status"],"incomplete")
                self.assertEqual(value["effect_state"],"none")
                self.assertTrue(value["executed"])
                self.assertTrue(value["cleanup_complete"])
                self.assertFalse(needs_inspection(output))

    def test_parent_can_continue_after_incomplete_readonly_task(self):
        original = run_subagent
        def short(**kwargs):
            kwargs["max_turns"]=1
            return original(**kwargs)
        with patch("homebrew_harness.runtime.turns.run_subagent",side_effect=short):
            result,requests,history=self.run_parent([reply(calls=[fixture.task_call("outer")]),reply(calls=[call("q",name="glob",arguments='{"pattern":"*"}')]),reply("Continue with preserved query results")])
        self.assertIn("Continue",result)
        self.assertEqual(len(requests),3)
        self.assertEqual(json.loads(history[-2]["content"])["effect_state"],"none")

    def test_confirmed_write_is_settled_even_without_final_child_report(self):
        output,_=self.run_child([reply(calls=[call("write",name="write_file",arguments='{"path":"result.txt","content":"saved"}')])],max_turns=1)
        value=json.loads(output)
        self.assertEqual(value["status"],"incomplete")
        self.assertEqual(value["effect_state"],"settled")
        self.assertFalse(needs_inspection(output))
        self.assertEqual((self.workdir/"result.txt").read_text(),"saved")

    def test_unverified_mutation_still_requires_inspection(self):
        failure=ToolExecutionError("Publication acknowledgement was lost.",outcome="partial",details={"operation":"write"})
        with patch.dict("homebrew_harness.tools.registry.TOOL_HANDLERS",{"write_file":lambda **kw: (_ for _ in ()).throw(failure)}):
            output,_=self.run_child([reply(calls=[call("write",name="write_file",arguments='{"path":"result.txt","content":"saved"}')])],max_turns=1)
        self.assertEqual(json.loads(output)["effect_state"],"uncertain")
        self.assertTrue(needs_inspection(output))

    def child_under_parent(self):
        sid=uuid.uuid4().hex;store=DurableStore(self.workdir)
        messages=[{"role":"user","content":"Parent requirement"}]
        parent=CheckpointWriter(store,"lead:"+sid,lambda:{"session_id":sid,"messages":messages})
        parent.messages=messages;parent.save("idle")
        client,_=fixture.scripted_client([reply(calls=[call("read",name="read_file",arguments='{"path":"sample.txt"}')])])
        (self.workdir/"sample.txt").write_text("CHILD_CONTENT_CAN_BE_READ_ON_DEMAND")
        with checkpoint_scope(parent):
            output=run_subagent("Read the sample",self.workdir,client=client,model="fixture",max_turns=1)
        return store,parent,json.loads(output)

    def test_parent_reads_child_evidence_on_demand_with_character_paging(self):
        store,parent,result=self.child_under_parent()
        self.assertNotIn("CHILD_CONTENT",json.dumps(result))
        with checkpoint_scope(parent):
            first=json.loads(read_result(workdir=self.workdir,run_id=result["run_id"],limit=25))
            self.assertFalse(first["complete"])
            summary=json.loads(json.loads(read_result(workdir=self.workdir,run_id=result["run_id"],limit=16000))["content"])
            record_id=summary["tool_results"][0]["operation_id"]
            evidence=json.loads(read_result(workdir=self.workdir,run_id=result["run_id"],operation_id=record_id,limit=16000))
        self.assertIn("CHILD_CONTENT_CAN_BE_READ_ON_DEMAND",evidence["content"])
        self.assertEqual(first["next_offset"],25)

    def test_archived_child_output_can_be_read_after_message_compaction(self):
        from homebrew_harness.context.compaction import ArchiveStore
        store,parent,result=self.child_under_parent()
        key=result["evidence_ref"];checkpoint=store.get("checkpoints",key)
        archive=ArchiveStore(self.workdir)
        reference=archive.save("transcripts", "\n".join(json.dumps(m) for m in checkpoint["messages"]))
        checkpoint["archives"]["saved"].append(reference)
        checkpoint["messages"]=[]
        store.checkpoint(key,checkpoint)
        op=checkpoint["effects"][0]["operation_id"]
        with checkpoint_scope(parent):
            output=json.loads(read_result(workdir=self.workdir,run_id=result["run_id"],operation_id=op,limit=16000))
        self.assertIn("CHILD_CONTENT_CAN_BE_READ_ON_DEMAND",output["content"])

    def test_known_pending_child_stops_later_calls_without_requiring_inspection(self):
        from homebrew_harness.tools.tool_outcome import subagent_result, ExecutionPending
        value=subagent_result("sub-"+"a"*32,"incomplete",effect_state="pending",cleanup_complete=False)
        later=call("later",name="write_file",arguments='{"path":"later.txt","content":"bad"}')
        with patch("homebrew_harness.runtime.turns.run_subagent",return_value=json.dumps(value)),self.assertRaises(ExecutionPending):
            self.run_parent([reply(calls=[fixture.task_call("outer"),later])])
        self.assertFalse((self.workdir/"later.txt").exists())

    def test_other_parent_and_unregistered_operation_cannot_read_child(self):
        store,parent,result=self.child_under_parent()
        other=CheckpointWriter(store,"lead:"+uuid.uuid4().hex,lambda:{"session_id":uuid.uuid4().hex,"messages":[]})
        with checkpoint_scope(other),self.assertRaisesRegex(ValueError,"parent"):
            read_result(workdir=self.workdir,run_id=result["run_id"])
        with checkpoint_scope(parent),self.assertRaisesRegex(ValueError,"registered"):
            read_result(workdir=self.workdir,run_id=result["run_id"],operation_id="forged")

    def test_live_owned_resource_is_pending_and_not_a_mutation_inspection(self):
        store,parent,result=self.child_under_parent()
        checkpoint=store.get("checkpoints",result["evidence_ref"])
        operation=checkpoint["effects"][0]["operation_id"]
        token=object();key=track_activity(token,(str(store.workdir),operation))
        try:
            from homebrew_harness.orchestration.subagent_results import result_for
            value=result_for(type("Child",(),{"effects":checkpoint["effects"],"store":store,"key":result["evidence_ref"]})(),result["run_id"],"incomplete")
            self.assertEqual(value["effect_state"],"pending")
            self.assertFalse(value["cleanup_complete"])
            self.assertFalse(needs_inspection(value))
        finally:settle_activity(key,token)

    def test_budget_notice_uses_existing_calls_and_does_not_add_a_request(self):
        output,requests=self.run_child([reply("Bounded finding")],max_turns=2)
        self.assertEqual(len(requests),1)
        self.assertTrue(any(m.get("name")=="subagent_remaining_calls" for m in requests[0]["messages"]))
        self.assertEqual(json.loads(output)["status"],"completed")


class ParentRunControlTests(unittest.TestCase):
    from tests.runs import test_controller as _fixture
    setUp = _fixture.RunTests.setUp
    controller = _fixture.RunTests.controller

    def test_parent_pause_is_not_hidden_by_child_failure(self):
        from homebrew_harness.runs import RunSpec
        from homebrew_harness.execution.model_gateway import ModelCancelled
        control, session, client, _ = self.controller([], RunSpec("Delegate local work", enabled=("subagents",), episode_model_calls=5))
        count=0
        def reply_or_stop(**kwargs):
            nonlocal count
            count+=1
            if count==1:return reply(calls=[fixture.task_call("child")])
            self.store.control(control.id,"pause")
            raise ModelCancelled("Parent paused")
        client.chat.completions.create.side_effect=reply_or_stop
        outcome=control.execute()
        self.assertEqual(outcome.status,"paused")
        self.assertEqual(outcome.reason_code,"pause")
        self.assertEqual(count,2)
        results=[m for m in session.messages if m.get("role")=="tool"]
        self.assertFalse(any(needs_inspection(m["content"]) for m in results))


class DelegationModelTests(unittest.TestCase):
    setUp = fixture.SubagentTests.setUp

    def test_parent_and_child_use_selected_models_and_parent_keeps_integration(self):
        from tests.execution.test_model_registry import spec
        from homebrew_harness.execution.model_registry import ModelRegistry
        from homebrew_harness.runtime.loop import agent_loop
        from homebrew_harness.runtime.loop_components import LoopComponents
        from homebrew_harness.runtime.capabilities import Capabilities
        from unittest.mock import Mock
        parent,lead_requests=fixture.scripted_client([reply(calls=[fixture.task_call("code", "Write implementation.txt", model_profile="coder")]),reply("Integrated the result")])
        child,child_requests=fixture.scripted_client([reply(calls=[call("write",name="write_file",arguments='{"path":"implementation.txt","content":"implemented"}')]),reply("Implementation ready")])
        child.close=Mock();registry=ModelRegistry([spec()],client_factory=lambda **kw:child)
        self.addCleanup(registry.close)
        from homebrew_harness.runtime.assembly import _prepare_loop
        with _prepare_loop(parent,"lead-fixture",self.workdir,LoopComponents(capabilities=Capabilities(("subagents",)),model_registry=registry)) as prepared:
            result=prepared.run([{"role":"user","content":"Delegate one bounded coding assignment"}],20,active_request="Delegate one bounded coding assignment")
        self.assertEqual(result,"Integrated the result")
        self.assertEqual(len(lead_requests),2)
        self.assertEqual(len(child_requests),2)
        self.assertTrue(all(r["model"]=="lead-fixture" for r in lead_requests))
        self.assertTrue(all(r["model"]==spec().model for r in child_requests))
        self.assertEqual((self.workdir/"implementation.txt").read_text(),"implemented")
        self.assertIn("model_profile",next(x for x in lead_requests[0]["tools"] if x["function"]["name"]=="task")["function"]["parameters"]["properties"])

    def test_parent_guidance_is_enabled_only_with_subagents(self):
        from homebrew_harness.runtime.prompts import build_system_prompt
        from homebrew_harness.runtime.capabilities import Capabilities
        from homebrew_harness.context.skill_loader import SkillLoader
        skills=SkillLoader(self.workdir/"skills")
        full=build_system_prompt(self.workdir,skill_loader=skills,capabilities=Capabilities(("subagents",)))
        core=build_system_prompt(self.workdir,skill_loader=skills,capabilities=Capabilities())
        self.assertIn("Use task for a bounded local subtask",full)
        self.assertNotIn("Use task for a bounded local subtask",core)
