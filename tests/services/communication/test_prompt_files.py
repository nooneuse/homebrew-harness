"""External guidance is checked offline and remains outside agent workspaces."""

import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.services.communication.prompts import defaults, load, identity
from tests.services import test_setup_validation as fixture


class PromptFiles(unittest.TestCase):
    setUp = fixture.SetupValidationTests.setUp
    save = fixture.SetupValidationTests.save
    save_tests = fixture.SetupValidationTests.save_tests
    validate = fixture.SetupValidationTests.validate

    def test_valid_external_override_needs_no_model_or_platform_call(self):
        prompt = self.private / 'assessment.md'
        prompt.write_text('Ask requesters only about their needs. Use operator for internal decisions.')
        self.spec['communication']['prompt_files'] = {'assessment': str(prompt)}
        with patch('homebrew_harness.execution.model_connections.HostModels.open', side_effect=AssertionError('model opened')):
            self.assertEqual(self.validate()['state'], 'validated')
        selected = {'assessment': str(prompt)}
        fixed = identity(selected, defaults())
        before = load(selected)['signature']
        prompt.write_text('Updated assessment instructions.')
        self.assertNotEqual(load(selected)['signature'], before)
        self.assertEqual(identity(selected, defaults()), fixed)

    def test_empty_oversized_non_utf8_missing_and_internal_files_are_rejected(self):
        prompt = self.private / 'assessment.md'
        self.spec['communication']['prompt_files'] = {'assessment': str(prompt)}
        with self.assertRaises((OSError, ValueError)):
            self.validate()
        for value in (b'', b'x' * 8001, b'\xff'):
            prompt.write_bytes(value)
            with self.subTest(value_length=len(value)), self.assertRaises((OSError, ValueError)):
                self.validate()
        for directory in (self.workspace, self.control):
            internal = directory / 'prompt.md'
            internal.write_text('instructions')
            self.spec['communication']['prompt_files'] = {'draft': str(internal)}
            with self.assertRaisesRegex(ValueError, 'external'):
                self.validate()

    def test_symlinked_prompt_and_unregistered_assessment_model_are_rejected(self):
        source = self.private / 'real.md'
        source.write_text('instructions')
        linked = self.private / 'linked.md'
        linked.symlink_to(source)
        self.spec['communication']['prompt_files'] = {'draft': str(linked)}
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.validate()
        self.spec['communication']['prompt_files'] = {}
        self.spec['communication']['assessment_model'] = 'missing-profile'
        with self.assertRaisesRegex(ValueError, 'Unknown model profile'):
            self.validate()

    def test_assessment_profile_can_configure_supported_max_reasoning(self):
        import json
        from homebrew_harness.execution.providers.common import frozen_options
        self.assertEqual(json.loads(frozen_options({"reasoning": "max"}))["reasoning"], "max")
