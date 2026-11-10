"""Goal binding preserves task text and commits before any model work."""

from tests.support.clients import cli_model_factory

import io
import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from homebrew_harness import HarnessSession
from homebrew_harness.cli import main
from homebrew_harness.execution.test_runner import TestRunner
from homebrew_harness.orchestration.goal_input import GoalInputError, load_goal_input
from homebrew_harness.runtime.runtime_state import RecoveryRequired
from tests.support.clients import fake_client, reply


class GoalBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "REPORT.md").write_text("ready")
        for redirect in (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def spec(self, condition="REPORT ready"):
        return {
            "condition": condition,
            "predicates": [
                {
                    "id": "report",
                    "type": "file_exists",
                    "path": "REPORT.md",
                    "min_bytes": 1,
                }
            ],
            "judge": False,
        }

    def session(self, responses=None, **kwargs):
        client, snapshots = fake_client(responses or [reply("Done")])
        client._harness_model_identity = "goal-binding-fixture"
        session = HarnessSession(
            client, "fixture", self.root, shutdown_timeout=0.05, **kwargs
        )
        self.addCleanup(lambda: session.wait_closed(5))
        self.addCleanup(session.close)
        return session, client, snapshots

    def test_prompt_and_goal_exist_in_first_request_and_saved_checkpoint(self):
        session, client, snapshots = self.session()
        original = "Create REPORT.md. Never edit protected.txt. 原始约束必须保留。"

        def verify_start(**_kwargs):
            self.assertIn(original, json.dumps(_kwargs["messages"], ensure_ascii=False))
            self.assertIn(self.spec()["condition"], json.dumps(_kwargs["messages"]))
            checkpoint = session.runtime_store.get(
                "checkpoints", session.checkpoint_key
            )
            self.assertEqual(checkpoint["intent"]["original"], original)
            goal = session.runtime_store.get("goals", checkpoint["goal_id"])
            self.assertEqual(goal["task_intent"], checkpoint["intent"])
            self.assertEqual(goal["spec"], session.goal.state["spec"])
            return reply("Done")

        client.chat.completions.create.side_effect = verify_start
        self.assertIn(
            "goal achieved", session.run_user(original, goal_spec=self.spec())
        )
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertIn(
            original,
            json.dumps(snapshots, ensure_ascii=False)
            if snapshots
            else session.intent.original,
        )
        self.assertEqual(session.intent.original, original)
        self.assertEqual(
            sum(
                m.get("role") == "user" and m.get("content") == original
                for m in session.messages
            ),
            1,
        )

    def test_goal_condition_slash_is_data_not_a_host_command(self):
        session, client, _ = self.session()
        condition = "/task Do not reset the original task"
        session.run_user(condition, goal_spec=self.spec(condition))
        self.assertEqual(session.intent.original, condition)
        self.assertEqual(session.goal.state["condition"], condition)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(session.goal.state["status"], "achieved")

    def test_resume_retains_original_and_adds_new_prompt(self):
        first, _, _ = self.session()
        first.run_user("Keep original constraints", goal_spec=self.spec())
        identity = first.session_id
        first.close()
        self.assertTrue(first.wait_closed(5))
        second, _, _ = self.session(resume_session=identity)
        second.run_user("Add a new requirement", goal_spec=self.spec("Updated report"))
        self.assertEqual(second.intent.original, "Keep original constraints")
        self.assertIn("Add a new requirement", second.intent.amendments)
        self.assertEqual(second.goal.state["task_intent"], second.intent.to_dict())

    def test_failed_binding_retains_old_goal_and_accepts_no_model_work(self):
        session, client, _ = self.session()
        session.run_user("/usage")
        session.intent.update("Original")
        session.goal.set_goal("Old goal")
        previous = session.goal.state["goal_id"]
        with patch.object(
            session._checkpoint_writer,
            "save",
            side_effect=OSError("failed transaction"),
        ):
            with self.assertRaises(OSError):
                session.run_user("New input", goal_spec=self.spec())
        self.assertEqual(session.goal.state["goal_id"], previous)
        self.assertEqual(session.intent.original, "Original")
        self.assertEqual(set(session.runtime_store.items("goals")), {previous})
        client.chat.completions.create.assert_not_called()

    def test_goal_conflict_aborts_without_replacing_original(self):
        session, client, _ = self.session()
        session.run_user("/usage")
        session.intent.update("Original")
        session.goal.set_goal("Old goal")
        original_save = session._checkpoint_writer.save

        def conflict(*args, **kwargs):
            session.goal._persist()
            return original_save(*args, **kwargs)

        with (
            patch.object(session._checkpoint_writer, "save", side_effect=conflict),
            self.assertRaises(RecoveryRequired),
        ):
            session.run_user("New input", goal_spec=self.spec())
        self.assertEqual(
            session.intent.to_dict(), {"original": "Original", "amendments": []}
        )
        self.assertEqual(session.goal.state["condition"], "Old goal")
        client.chat.completions.create.assert_not_called()

    def test_committed_binding_with_lost_return_is_not_repeated(self):
        session, client, _ = self.session()
        session.run_user("/usage")
        original = session.runtime_store.checkpoint
        lost = False

        def checkpoint(*args, **kwargs):
            nonlocal lost
            result = original(*args, **kwargs)
            if not lost and (kwargs.get("boundary") or {}).get("goal_binding"):
                lost = True
                raise OSError("return lost after commit")
            return result

        with patch.object(session.runtime_store, "checkpoint", side_effect=checkpoint):
            session.run_user("Never edit protected.txt", goal_spec=self.spec())
        self.assertTrue(lost)
        self.assertEqual(len(session.runtime_store.items("goals")), 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(session.intent.original, "Never edit protected.txt")

    def test_cli_preserves_prompt_with_goal_spec_and_only_spec(self):
        path = self.root / "goal.json"
        path.write_text(json.dumps(self.spec()))
        for text in ("Never edit protected.txt", None):
            client, snapshots = fake_client([reply("Done")])
            args = ["session", "--cwd", str(self.root), "--goal-spec", str(path)] + (
                [text] if text else []
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "fixture-key",
                        "OPENAI_BASE_URL": "fixture",
                        "OPENAI_MODEL": "fixture",
                        "HARNESS_PROFILE": "core",
                    },
                ),
                patch("homebrew_harness.session_cli.load_dotenv"),
                cli_model_factory() as factory,
                patch(
                    "homebrew_harness.session_cli.load_test_runner",
                    return_value=TestRunner(self.root, []),
                ),
            ):
                factory.return_value.__enter__.return_value = client
                self.assertEqual(main(args), 0)
            expected = text or self.spec()["condition"]
            self.assertTrue(
                any(
                    m.get("role") == "user" and m.get("content") == expected
                    for m in snapshots[0]
                )
            )

    def test_invalid_goal_types_preserve_existing_goal_and_checkpoint(self):
        session, client, _ = self.session()
        session.run_user("/usage")
        session.intent.update("Original task")
        session.goal.set_goal("Existing goal")
        previous = copy.deepcopy(session.goal.state)
        checkpoint = session.runtime_store.get("checkpoints", session.checkpoint_key)
        lease = session.goal.lease
        for kind in ([], {}, None, 1, 1.0, True, "", "unknown"):
            spec = self.spec()
            spec["predicates"][0]["type"] = kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                session.run_user("Replace task", goal_spec=spec)
            self.assertEqual(session.goal.state, previous)
            self.assertIs(session.goal.lease, lease)
            self.assertEqual(
                session.runtime_store.get("checkpoints", session.checkpoint_key),
                checkpoint,
            )
        self.assertEqual(session.intent.original, "Original task")
        client.chat.completions.create.assert_not_called()


class GoalInputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_invalid_discriminator_types_have_normal_cli_errors(self):
        path = self.root / "goal.json"
        for kind in ([], {}, None, 1, False):
            path.write_text(
                json.dumps(
                    {
                        "condition": "finish",
                        "predicates": [
                            {"id": "target", "type": kind, "path": "out.txt"}
                        ],
                    }
                )
            )
            captured = io.StringIO()
            with (
                self.subTest(kind=kind),
                patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "fixture-key",
                        "OPENAI_BASE_URL": "fixture",
                        "OPENAI_MODEL": "fixture",
                    },
                ),
                patch("homebrew_harness.session_cli.load_dotenv"),
                cli_model_factory() as factory,
                redirect_stderr(captured),
            ):
                self.assertEqual(
                    main(
                        [
                            "session",
                            "--cwd",
                            str(self.root),
                            "--goal-spec",
                            str(path),
                            "task",
                        ]
                    ),
                    1,
                )
                factory.assert_not_called()
            self.assertIn("--goal-spec", captured.getvalue())
            self.assertNotIn("Traceback", captured.getvalue())
            self.assertFalse((self.root / ".harness").exists())

    def test_field_type_matrix_uses_shared_validation(self):
        from homebrew_harness.orchestration.hard_conditions import (
            ConditionError,
            validate_spec,
        )

        valid = {
            "condition": "done",
            "predicates": [{"id": "result", "type": "file_exists", "path": "out.txt"}],
        }
        for field, values in {
            "id": [[], {}, None, True],
            "workspace": [[], {}, None, 1],
            "path": [[], {}, None, 1],
            "min_bytes": [[], {}, None, True],
        }.items():
            for value in values:
                spec = copy.deepcopy(valid)
                spec["predicates"][0][field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ConditionError),
                ):
                    validate_spec(spec)
        original = copy.deepcopy(valid)
        normalized = validate_spec(valid)
        self.assertEqual(valid, original)
        self.assertEqual(normalized["predicates"][0]["workspace"], "base")

    def test_regular_unicode_definition_is_frozen(self):
        path = self.root / "goal.json"
        path.write_text(
            json.dumps(
                {
                    "condition": "保持中文约束",
                    "predicates": [
                        {"id": "file", "type": "file_exists", "path": "file"}
                    ],
                    "judge": False,
                },
                ensure_ascii=False,
            )
        )
        result = load_goal_input(path)
        path.write_text("{}")
        self.assertEqual(result["condition"], "保持中文约束")

    def test_invalid_input_fails_before_clients_and_test_resources(self):
        path = self.root / "goal.json"
        cases = [
            None,
            b"\xff",
            b"{bad-json}",
            b'{"condition":"a","condition":"b","predicates":[]}',
            b'{"condition":"a","predicates":[],"judge":NaN}',
            b"[" * 2000 + b"0" + b"]" * 2000,
            b"x" * (1024 * 1024 + 1),
        ]
        for raw in cases:
            with self.subTest(case=None if raw is None else len(raw)):
                if path.exists():
                    path.unlink()
                if raw is not None:
                    path.write_bytes(raw)
                with (
                    patch.dict(
                        os.environ,
                        {
                            "OPENAI_API_KEY": "fixture-key",
                            "OPENAI_BASE_URL": "fixture",
                            "OPENAI_MODEL": "fixture",
                        },
                    ),
                    patch("homebrew_harness.session_cli.load_dotenv"),
                    cli_model_factory() as client,
                    patch("homebrew_harness.session_cli.load_test_runner") as tests,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()) as error,
                ):
                    self.assertEqual(
                        main(
                            [
                                "session",
                                "--cwd",
                                str(self.root),
                                "--goal-spec",
                                str(path),
                            ]
                        ),
                        1,
                    )
                self.assertNotIn("Traceback", error.getvalue())
                client.assert_not_called()
                tests.assert_not_called()
                self.assertFalse((self.root / ".harness").exists())

    def test_special_files_and_symlinks_fail_without_blocking(self):
        path = self.root / "input"
        for kind in ("directory", "fifo", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    path.mkdir()
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    path.symlink_to(self.root / "missing")
                with self.assertRaises(GoalInputError):
                    load_goal_input(path)
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()

    def test_file_replacement_during_read_is_rejected(self):
        path = self.root / "goal.json"
        path.write_text("{}")
        original = os.stat

        def changed(*args, **kwargs):
            replacement = self.root / "new.json"
            replacement.write_text('{"different":true}')
            replacement.replace(path)
            return original(*args, **kwargs)

        with (
            patch(
                "homebrew_harness.orchestration.goal_input.os.stat", side_effect=changed
            ),
            self.assertRaises(GoalInputError),
        ):
            load_goal_input(path)
