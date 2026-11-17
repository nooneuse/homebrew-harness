"""Read snapshots identify source changes without caching or unbounded hashing."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.tools.files import run_read
from homebrew_harness.runtime.errors import error_info


class FileVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / 'source.py'
        self.path.write_text('你好\r\none\r\ntwo\r\n', encoding='utf-8', newline='')

    def read(self, **kwargs):
        return json.loads(run_read('source.py', self.root, **kwargs))

    def test_unicode_paging_keeps_snapshot_and_uses_character_offsets(self):
        first = self.read(limit=1)
        self.assertEqual(first['offset'], 0)
        self.assertEqual(first['content'], '你好\r\n')
        self.assertEqual(first['next_offset'], 4)
        second = self.read(offset=first['next_offset'], expected_version=first['version'])
        self.assertEqual(second['content'], 'one\r\ntwo\r\n')
        self.assertEqual(second['version'], first['version'])
        self.assertFalse(second['truncated'])

    def test_change_between_pages_reports_versions_and_restart_navigation(self):
        first = self.read(limit=1)
        self.path.write_text('Changed source\n')
        with self.assertRaises(ValueError) as failure:
            self.read(offset=first['next_offset'], expected_version=first['version'])
        info = error_info(failure.exception)
        self.assertEqual(info['code'], 'file_read_version_changed')
        self.assertEqual(info['details']['expected_version'], first['version'])
        self.assertNotEqual(info['details']['observed_version'], first['version'])
        self.assertEqual(info['details']['restart_offset'], 0)
        self.assertEqual(info['details']['file_path'], 'source.py')

    def test_same_bytes_replacement_has_a_new_filesystem_version(self):
        first = self.read()
        replacement = self.root / 'replacement.py'
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        self.assertNotEqual(self.read()['version'], first['version'])

    def test_in_place_mutation_during_read_is_reported_before_returning_content(self):
        from homebrew_harness.tools import files
        checks = 0
        def mutate():
            nonlocal checks
            checks += 1
            if checks == 1:
                self.path.write_text('Concurrent edit\n')
        with patch.object(files, 'execution_check', side_effect=mutate):
            with self.assertRaises(ValueError) as failure:
                self.read()
        self.assertEqual(error_info(failure.exception)['code'], 'file_read_changed')

    def test_replacement_during_read_is_reported_before_returning_content(self):
        from homebrew_harness.tools import files
        def replace():
            replacement = self.root / 'replacement.py'
            replacement.write_text('Concurrent replacement\n')
            os.replace(replacement, self.path)
        with patch.object(files, 'execution_check', side_effect=replace):
            with self.assertRaises(ValueError) as failure:
                self.read()
        self.assertEqual(error_info(failure.exception)['code'], 'file_read_changed')

    def test_removal_during_read_returns_changed_file_navigation(self):
        from homebrew_harness.tools import files
        with patch.object(files, 'execution_check', side_effect=self.path.unlink):
            with self.assertRaises(ValueError) as failure:
                self.read()
        self.assertEqual(error_info(failure.exception)['code'], 'file_read_changed')
        self.assertEqual(error_info(failure.exception)['details']['restart_offset'], 0)

    def test_normal_and_empty_file_reads_are_not_cached(self):
        first = self.read()
        self.path.write_text('')
        second = self.read()
        self.assertEqual(second['content'], '')
        self.assertNotEqual(second['version'], first['version'])

    def test_schema_states_units_and_version_contract(self):
        from homebrew_harness.tools.catalog import TOOLS
        tool = next(t['function'] for t in TOOLS if t['function']['name'] == 'read_file')
        properties = tool['parameters']['properties']
        self.assertIn('Unicode characters', properties['offset']['description'])
        self.assertIn('lines', properties['limit']['description'])
        self.assertIn('expected_version', properties)
