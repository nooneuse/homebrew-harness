"""Terminal Run races must not prevent actual interaction resource shutdown."""

import unittest
from unittest.mock import patch

from tests.runtime import test_interaction as fixture


class CloseRaceTests(unittest.TestCase):
    setUp = fixture.InteractionTests.setUp
    host = fixture.InteractionTests.host

    def test_completion_between_read_and_pause_still_releases_owned_resources(self):
        host, _, _ = self.host([])
        host.supervisor = object()
        task = {
            "target": {"kind": "run", "id": "run_" + "a" * 32},
            "lifetime": "attached",
        }
        with (
            patch.object(host.state, "active", return_value=task),
            patch.object(
                host.runs,
                "get",
                side_effect=[{"status": "running"}, {"status": "completed"}],
            ),
            patch.object(
                host.runs,
                "control",
                side_effect=ValueError("Terminal run controls are closed."),
            ) as control,
        ):
            host.close()
            control.assert_called_once_with(task["target"]["id"], "pause")
            self.assertTrue(host.shutdown.finished.wait(5))
        self.assertTrue(host.closed)

    def test_already_terminal_run_needs_no_pause_but_closes_normally(self):
        host, _, _ = self.host([])
        host.supervisor = object()
        task = {
            "target": {"kind": "run", "id": "run_" + "b" * 32},
            "lifetime": "attached",
        }
        with (
            patch.object(host.state, "active", return_value=task),
            patch.object(host.runs, "get", return_value={"status": "failed"}),
            patch.object(host.runs, "control") as control,
        ):
            host.close()
            control.assert_not_called()
            self.assertTrue(host.shutdown.finished.wait(5))
