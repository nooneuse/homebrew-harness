"""Platform contract isolation and concrete GitHub translation."""

import time
import unittest
from tests.services import test_maintenance as fixture
from tests.services.p2_fixture import issue
from homebrew_harness.platforms.contracts import ObjectRef, SourceEvent, SourcePage
from homebrew_harness.platforms.registry import RepositorySource, catalog
from homebrew_harness.execution.model_gateway import Deadline


class PlatformTests(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    start = fixture.MaintenanceTests.start

    def source(self, **kw):
        declaration = {
            "name": "project",
            "provider": "github",
            "instance": "primary",
            "project": "example/project",
            "api_url_env": "E",
            "token_env": "T",
            "poll_seconds": 10,
            **kw,
        }
        source = RepositorySource(declaration, {"E": self.peer.url, "T": "synthetic"})
        self.addCleanup(source.close)
        return source

    def test_reference_identity_includes_platform_instance_project_and_kind(self):
        base = ObjectRef("github", "primary", "example/project", "issue", "1", 1)
        others = [
            ObjectRef("fixture", "primary", "example/project", "issue", "1", 1),
            ObjectRef("github", "other", "example/project", "issue", "1", 1),
            ObjectRef("github", "primary", "other/project", "issue", "1", 1),
            ObjectRef("github", "primary", "example/project", "change_request", "1", 1),
        ]
        self.assertEqual(len({r.key() for r in [base, *others]}), 5)
        self.assertEqual(
            base.key(),
            ObjectRef("github", "primary", "example/project", "issue", "1", 2).key(),
        )

    def test_unsupported_provider_fails_before_network_calls(self):
        with self.assertRaises(ValueError):
            self.source(provider="gitlab")
        self.assertEqual(self.peer.source_requests, [])
        self.assertEqual(
            catalog(),
            [
                {
                    "provider": "github",
                    "capabilities": [
                        "change_request_feedback",
                        "communication",
                        "delivery",
                        "repository_events",
                    ],
                }
            ],
        )

    def test_cursor_cannot_cross_instances_or_projects(self):
        source = self.source()
        page = source.poll({}, Deadline.after(5))
        for other in (
            self.source(instance="other"),
            self.source(project="other/project"),
        ):
            before = len(self.peer.source_requests)
            with self.assertRaises(ValueError):
                other.poll(page.cursor, Deadline.after(5))
            self.assertEqual(len(self.peer.source_requests), before)

    def test_issue_and_change_request_with_same_number_have_distinct_work(self):
        service = self.start()
        service.pause("all")
        store = service.runtime.maintenance.store
        refs = [
            ObjectRef("fixture", "internal", "group/subgroup/project", kind, "1", 1)
            for kind in ("issue", "change_request")
        ]
        for n, ref in enumerate(refs):
            event = SourceEvent(
                ref,
                ref,
                "version-1",
                {
                    "title": "Requirement",
                    "body": "Investigate",
                    "state": "open",
                    "actor_id": 7,
                },
            )
            page = SourcePage((event,), {}, time.time() + 10, "ready")
            store.source_page("source-" + str(n), page, ref.scope())
        works = service.work.list()["items"]
        self.assertEqual(len(works), 2)
        self.assertEqual(
            {w["subject"]["kind"] for w in works}, {"issue", "change_request"}
        )

    def test_duplicate_feed_does_not_retriage_the_same_object_version(self):
        service = self.start()
        service.pause("all")
        store = service.runtime.maintenance.store
        ref = ObjectRef("fixture", "internal", "group/project", "issue", "42", 1)
        event = SourceEvent(
            ref, ref, "version-1", {"title": "Requirement", "body": "Investigate"}
        )
        page = SourcePage((event,), {}, time.time() + 10, "ready")
        store.source_page("first", page, ref.scope())
        store.source_page("second", page, ref.scope())
        works = service.work.list()["items"]
        events = service.records("events")["items"]
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]["requirements_revision"], 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["sources"], ["first", "second"])

    def test_foreign_page_is_rejected_before_persistence(self):
        service = self.start()
        service.pause("all")
        store = service.runtime.maintenance.store
        ref = ObjectRef("fixture", "internal", "group/project", "issue", "42", 1)
        page = SourcePage(
            (SourceEvent(ref, ref, "v1", {"body": "Request"}),),
            {},
            time.time(),
            "ready",
        )
        with self.assertRaises(ValueError):
            store.source_page("project", page, ("github", "primary", "example/project"))
        self.assertEqual(service.work.list()["items"], [])

    def test_github_pr_comments_keep_the_change_request_subject_on_resume(self):
        row = issue(1)
        row["pull_request"] = {}
        self.peer.issues = [row]
        self.peer.comments = [
            {
                "id": 91,
                "issue_url": self.peer.url + "/repos/example/project/issues/1",
                "body": "Review note",
                "updated_at": "2026-01-02T00:00:00Z",
                "user": {"id": 11},
            }
        ]
        first = self.source()
        page = first.poll({}, Deadline.after(5))
        self.assertEqual(page.events[0].subject.kind, "change_request")
        restored = self.source()
        comments = restored.poll(page.cursor, Deadline.after(5))
        self.assertEqual(comments.events[0].subject, page.events[0].subject)
        self.assertEqual(comments.events[0].object.kind, "comment")
        self.assertTrue(any("/issues/1" in path for path in self.peer.source_requests))

    def test_private_ca_is_explicit_and_tls_verification_remains_enabled(self):
        import subprocess
        import shutil
        import ssl

        if not shutil.which("openssl"):
            self.skipTest("TLS fixture requires openssl")
        directory = self.base / "tls"
        directory.mkdir()
        cert = directory / "server.pem"
        key = directory / "server.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.peer.server.socket = context.wrap_socket(
            self.peer.server.socket, server_side=True
        )
        endpoint = self.peer.url.replace("http:", "https:")
        declaration = {
            "name": "project",
            "provider": "github",
            "instance": "private",
            "project": "example/project",
            "api_url_env": "E",
            "token_env": "T",
            "ca_file_env": "CA",
        }
        source = RepositorySource(
            declaration,
            {"E": endpoint, "T": "synthetic", "CA": str(cert)},
            roots=(self.control, self.workspace),
        )
        self.addCleanup(source.close)
        self.assertEqual(source.poll({}, Deadline.after(5)).events, ())
        untrusted = RepositorySource(
            {k: v for k, v in declaration.items() if k != "ca_file_env"},
            {"E": endpoint, "T": "synthetic"},
        )
        self.addCleanup(untrusted.close)
        with self.assertRaises(Exception):
            untrusted.poll({}, Deadline.after(5))
        local = self.workspace / "ca.pem"
        local.write_bytes(cert.read_bytes())
        with self.assertRaises(ValueError):
            RepositorySource(
                declaration,
                {"E": endpoint, "T": "synthetic", "CA": str(local)},
                roots=(self.workspace,),
            )

    def test_registered_adapter_can_use_a_different_project_identifier(self):
        from unittest.mock import patch
        from homebrew_harness.platforms.registry import PROVIDERS

        class FixtureAdapter:
            def __init__(self, spec, transport):
                self.spec, self.transport = spec, transport

            def poll(self, position, deadline):
                ref = ObjectRef(
                    "fixture",
                    self.spec["instance"],
                    self.spec["project"],
                    "change_request",
                    "opaque-id",
                    1,
                )
                return SourcePage(
                    (SourceEvent(ref, ref, "v1", {"body": "Normalized requirement"}),),
                    {"bookmark": "opaque"},
                    time.time() + 10,
                    "ready",
                )

            def close(self):
                self.transport.close()

        with patch.dict(
            PROVIDERS,
            {
                "fixture": {
                    "project": lambda value: str(value),
                    "repository_events": FixtureAdapter,
                }
            },
        ):
            source = self.source(provider="fixture", project=42)
            page = source.poll({}, Deadline.after(5))
            self.assertEqual(page.events[0].subject.project, "42")
            self.assertEqual(page.cursor["position"], {"bookmark": "opaque"})
            self.assertEqual(self.peer.source_requests, [])
