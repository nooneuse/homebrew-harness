import tempfile
import unittest
from pathlib import Path

from homebrew_harness.services import ServiceSpec, ScheduleSpec
from homebrew_harness.services.store import ServiceStore


class ServiceStorage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "control"
        self.root.mkdir(mode=0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.store = ServiceStore(self.root)

    def create(self, **options):
        spec = ServiceSpec(str(self.workspace), **options)
        value = self.store.create(spec, {}, {}, "one")
        return spec, self.store.claim(value["id"], {})

    def test_service_and_work_acceptance_replay_and_payload_conflicts(self):
        spec, header = self.create()
        self.assertEqual(self.store.create(spec, {}, {}, "one")["id"], header["id"])
        with self.assertRaises(ValueError):
            self.store.create(spec, {"different": True}, {}, "one")

        def submit(conn):
            return self.store.enqueue(conn, header["id"], "Work", "input")

        first = self.store.command(header, "submit", {"text": "Work"}, "input", submit)
        again = self.store.command(
            header,
            "submit",
            {"text": "Work"},
            "input",
            lambda _conn: self.fail("Duplicate action executed"),
        )
        self.assertEqual(first, again)
        with self.assertRaises(ValueError):
            self.store.command(header, "submit", {"text": "Different"}, "input", submit)
        self.assertEqual(len(self.store.page(header["id"])["items"]), 1)

    def test_old_owner_is_fenced_after_restart(self):
        _, old = self.create()
        new = self.store.claim(old["id"], {})
        with self.assertRaises(ValueError):
            self.store.update(old, error="old owner")
        self.assertEqual(new["generation"], old["generation"] + 1)

    def test_missed_schedule_windows_coalesce_and_pending_work_blocks_duplicates(self):
        _, header = self.create(
            schedules=(
                ScheduleSpec("task", 10, "submit_work", "Task"),
                ScheduleSpec("health", 10),
            )
        )
        now = self.store.get(header["id"])["created_at"] + 101
        self.assertEqual(self.store.poll_schedules(header, now), 2)
        self.assertEqual(self.store.poll_schedules(header, now), 0)
        self.assertEqual(self.store.poll_schedules(header, now + 20), 1)
        work = self.store.page(header["id"])["items"][0]
        self.store.update_work(header, work["id"], state="review")
        self.assertEqual(self.store.poll_schedules(header, now + 20), 1)
        self.assertEqual(len(self.store.page(header["id"])["items"]), 2)
        jobs = self.store.page(header["id"], section="service_jobs")["items"]
        self.assertTrue(all(j["next_due"] > now + 20 for j in jobs))

    def test_queue_full_rolls_back_trigger_and_cursor(self):
        _, header = self.create(
            queue_limit=1, schedules=(ScheduleSpec("task", 10, "submit_work", "Task"),)
        )
        with self.store.db.write() as conn:
            self.store.enqueue(conn, header["id"], "First", "first")
        before = self.store.page(header["id"], section="service_jobs")
        with self.assertRaises(ValueError):
            self.store.poll_schedules(
                header, self.store.get(header["id"])["created_at"] + 20
            )
        self.assertEqual(before, self.store.page(header["id"], section="service_jobs"))

    def test_paginated_queries_do_not_copy_full_run_spec(self):
        _, header = self.create()
        for i in range(3):
            with self.store.db.write() as conn:
                self.store.enqueue(conn, header["id"], "Task " + str(i), str(i))
        first = self.store.page(header["id"], limit=2)
        second = self.store.page(header["id"], cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotIn("run_spec", first["items"][0])
        self.assertNotIn("prompt", first["items"][0])

    def test_spec_scope_and_installed_runtime_are_validated(self):
        spec = ServiceSpec(str(self.workspace))
        spec.validate_location(self.root)
        with self.assertRaises(ValueError):
            ServiceSpec("relative")
        with self.assertRaises(ValueError):
            ScheduleSpec("job", float("nan"))
        with self.assertRaises(ValueError):
            ScheduleSpec("job", 10, "shell")
        with self.assertRaises(ValueError):
            ServiceSpec(str(self.root)).validate_location(self.root)
        from tests.support.paths import PROJECT_ROOT

        with self.assertRaises(ValueError):
            ServiceSpec(str(PROJECT_ROOT)).validate_location(self.root)

    def test_unknown_launch_token_can_be_fenced_without_restarting_work(self):
        from homebrew_harness.services.launcher import recover_launch

        spec, header = self.create()
        with self.store.db.write() as conn:
            value = self.store.db._get(conn, "service_meta", header["id"])
            self.store.db._put(
                conn,
                "service_meta",
                header["id"],
                {**value, "launch": {"token": "old", "phase": "pending"}},
            )
        self.assertEqual(
            recover_launch(self.store, header["id"], "old")["state"], "recovered"
        )
        self.assertIsNone(self.store.get(header["id"])["launch"]["token"])
        with self.assertRaises(ValueError):
            recover_launch(self.store, header["id"], "old")
