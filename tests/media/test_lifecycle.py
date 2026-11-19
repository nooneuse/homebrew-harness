"""Media lifecycle: compaction, save/resume, GC roots, backup/restore, cancellation.

Accepted frozen media must stay retrievable through every supported lifecycle
transition, and interrupted operations must never publish partial media.
"""

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from homebrew_harness import Harness, HostConfig
from homebrew_harness.execution.execution_control import control_scope
from homebrew_harness.execution.model_gateway import (
    Deadline,
    ModelCancelled,
    ModelDeadlineExceeded,
)
from homebrew_harness.execution.model_protocol import ModelCapabilities
from homebrew_harness.execution.model_registry import ModelRegistry, ModelSpec
from homebrew_harness.media import MediaStore
from homebrew_harness.runtime import session_commands
from homebrew_harness.runtime.host_commands import HostCommand
from homebrew_harness.runs import RunSpec, RunStore
from homebrew_harness.runs.maintenance import (
    backup_run,
    restore_backup,
    verify_backup,
)
from homebrew_harness.storage.artifact_inventory import inspect_artifacts
from homebrew_harness.tools.media import render_bar_chart, run_render_chart
from tests.support.clients import fake_client, reply


def png(seed):
    return render_bar_chart(
        ["alpha", "beta", "gamma"], [seed, seed + 1, seed + 2], width=160, height=120
    )


def image_part(path):
    return {"type": "image", "path": str(path)}


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workdir = self.root / "workspace"
        self.workdir.mkdir()

    def host(self, responses):
        client, requests = fake_client(responses)
        client.close = lambda: None
        self.spec = ModelSpec(
            name="fixture",
            model="fixture-model",
            api_key="fixture",
            base_url="http://127.0.0.1:9/v1",
            capabilities=ModelCapabilities(input_modalities=("text", "image")),
        )
        registry = ModelRegistry(
            [self.spec],
            lead_default="fixture",
            client_factory=lambda **_kw: client,
        )
        host = Harness(
            workdir=self.workdir,
            config=HostConfig.from_components(
                model_registry=registry, shutdown_seconds=3
            ),
        )
        self.addCleanup(host.close)
        return host, client, requests

    def write_png(self, name, seed):
        path = self.workdir / name
        data = png(seed)
        path.write_bytes(data)
        return path, data

    def submit_image(self, session, path, text="Describe the picture"):
        turn = session.submit(
            text, parts=[{"type": "text", "text": text}, image_part(path)]
        )
        self.assertEqual(turn.wait(10).snapshot.state, "completed")
        return turn


class CompactionTests(LifecycleTestCase):
    def test_compaction_keeps_archived_media_retrievable(self):
        path, data = self.write_png("压缩 compact.png", 1)
        responses = (
            [reply("ack")]
            + [reply(f"ok {index}") for index in range(4)]
            + [reply("Working state: the image turn was archived; media retained.")]
        )
        host, _, _ = self.host(responses)
        session = host.sessions.open()
        self.submit_image(session, path)
        for index in range(4):
            turn = session.submit(f"follow up {index}")
            self.assertEqual(turn.wait(10).snapshot.state, "completed")
        # The accepted turn is frozen: editing the source must not change it.
        path.write_bytes(png(50))

        # The CLI /compact host command archives old turns into a marker.
        outcome = session_commands.run(session.runtime, HostCommand.parse("/compact"))
        self.assertTrue(json.loads(outcome)["compacted"])

        media = session.media()
        self.assertEqual(len(media), 1)
        entry = media[0]
        self.assertEqual(entry["origin"], "user_input")
        self.assertEqual(entry["status"], "available")
        meta = session.media_metadata(entry["media_ref"])
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(meta["bytes"], len(data))
        self.assertEqual(MediaStore(self.workdir).load(entry["media_ref"]), data)
        exported = session.export_media(entry["media_ref"], "exports/compacted.png")
        self.assertEqual(
            (self.workdir / exported["path"]).read_bytes(), data
        )


class SaveResumeTests(LifecycleTestCase):
    def test_save_resume_and_follow_up_keep_media_available(self):
        path, data = self.write_png("保存 resume.png", 2)
        host, client, _ = self.host([reply("ack"), reply("follow up answer")])
        session = host.sessions.open()
        self.submit_image(session, path)
        identity = session.id
        ref = session.media()[0]["media_ref"]
        host.close()
        # The original attachment is gone; the frozen turn must survive.
        path.unlink()

        # A fresh process builds its own registry over the same persisted state.
        registry = ModelRegistry(
            [self.spec], lead_default="fixture", client_factory=lambda **_kw: client
        )
        other = Harness(
            workdir=self.workdir,
            config=HostConfig.from_components(model_registry=registry, shutdown_seconds=3),
        )
        self.addCleanup(other.close)
        self.assertEqual(other.sessions.get(identity).snapshot().state, "closed")
        resumed = other.sessions.resume(identity)
        self.assertEqual(resumed.id, identity)

        media = resumed.media()
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["media_ref"], ref)
        self.assertEqual(media[0]["status"], "available")
        self.assertEqual(media[0]["origin"], "user_input")
        meta = resumed.media_metadata(ref)
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())

        turn = resumed.submit("What was in the image?")
        self.assertEqual(turn.wait(10).snapshot.state, "completed")
        self.assertEqual(resumed.media()[0]["media_ref"], ref)
        exported = resumed.export_media(ref, "exports/resumed.png")
        self.assertEqual((self.workdir / exported["path"]).read_bytes(), data)
        calls = client.chat.completions.create.call_count
        resumed.close()
        self.assertEqual(client.chat.completions.create.call_count, calls)


class RetentionTests(LifecycleTestCase):
    def test_gc_roots_protect_live_session_media(self):
        path, data = self.write_png("gc.png", 3)
        host, _, _ = self.host([reply("ack")])
        session = host.sessions.open()
        self.submit_image(session, path)
        ref = session.media()[0]["media_ref"]
        # A frozen but never-referenced image is reclaimable once expired.
        orphan = MediaStore(self.workdir).freeze(png(77))["media_ref"]
        host.close()

        report = inspect_artifacts(
            self.workdir, retention_days=1, now=time.time() + 3 * 86400
        )
        candidate_paths = {item["path"] for item in report["candidates"]}
        self.assertIn(ref, report["references"])
        self.assertNotIn(ref, candidate_paths)
        self.assertIn(orphan, candidate_paths)
        self.assertEqual(MediaStore(self.workdir).load(ref), data)


class BackupRestoreTests(LifecycleTestCase):
    def test_run_backup_restore_round_trips_frozen_media(self):
        runws = self.root / "runws"
        runws.mkdir()
        store = RunStore(runws)
        self.addCleanup(store.close)
        value = store.create(RunSpec("Media backup rehearsal"))
        value.update(status="completed", cleanup_complete=True)
        store.runtime.put("runs", value["id"], value)
        data = png(4)
        meta = MediaStore(runws).freeze(data)

        backup = backup_run(store, value["id"], self.root / "backup")
        self.assertTrue(backup["backed_up"])
        self.assertTrue(verify_backup(self.root / "backup")["valid"])
        member = self.root / "backup" / "attachments" / meta["media_ref"]
        self.assertEqual(member.read_bytes(), data)

        # Tampering with the frozen member breaks manifest verification.
        member.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_backup(self.root / "backup")
        member.write_bytes(data)

        restored = restore_backup(self.root / "backup", self.root / "restored")
        self.assertTrue(restored["restored"])
        target = self.root / "restored"
        self.assertEqual(MediaStore(target).load(meta["media_ref"]), data)
        described = MediaStore(target).describe(meta["media_ref"])
        self.assertEqual(described["sha256"], meta["sha256"])
        self.assertEqual(described["bytes"], len(data))


class CancellationTests(LifecycleTestCase):
    def test_cancellation_and_deadline_interrupt_media_without_partial_output(self):
        store = MediaStore(self.workdir)
        data = png(5)
        meta = store.freeze(data)
        media_dir = self.workdir / ".harness" / "media"
        before = sorted(item.name for item in media_dir.iterdir())

        cancel = threading.Event()
        cancel.set()
        with control_scope(Deadline.after(30), cancel_events=(cancel,)):
            with self.assertRaises(ModelCancelled):
                store.load(meta["media_ref"])
            with self.assertRaises(ModelCancelled):
                store.freeze(png(6))
            with self.assertRaises(ModelCancelled):
                run_render_chart(["alpha"], [1], self.workdir)
        # Interrupted preparation publishes nothing.
        self.assertEqual(sorted(item.name for item in media_dir.iterdir()), before)

        with control_scope(Deadline.after(0)):
            with self.assertRaises(ModelDeadlineExceeded):
                store.load(meta["media_ref"])

        # Outside the interrupted scope the accepted content stays usable.
        self.assertEqual(store.load(meta["media_ref"]), data)


if __name__ == "__main__":
    unittest.main()
