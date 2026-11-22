import tempfile
import unittest
from pathlib import Path
from homebrew_harness.orchestration.task_store import TaskStore


class EditablePlanDependencies(unittest.TestCase):
    def test_optional_dependency_can_be_removed_without_completing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp))
            self.addCleanup(store.close)
            optional = store.create("Optional research")
            task = store.create("Implement")
            store.update_dependencies(task.id, [optional.id])
            current = store.update_dependencies(
                task.id, remove_blocked_by=[optional.id]
            )
            self.assertEqual(current.blockedBy, ())
            self.assertEqual(store.load(optional.id).status, "pending")

    def test_replacement_rejects_cycle_without_mutating_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TaskStore(Path(temp))
            self.addCleanup(store.close)
            first = store.create("First")
            second = store.create("Second")
            store.update_dependencies(second.id, [first.id])
            with self.assertRaises(ValueError):
                store.update_dependencies(first.id, blocked_by=[second.id])
            self.assertEqual(store.load(first.id).blockedBy, ())
