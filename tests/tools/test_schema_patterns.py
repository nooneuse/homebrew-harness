"""Tool string schemas support both JSON validation and whole-string decoding."""

import re
import unittest

from homebrew_harness.context.todo import TODO_INPUT_SCHEMA
from homebrew_harness.tools.catalog import NONEMPTY_STRING, TOOLS


class StringSchemaTests(unittest.TestCase):
    def tool_schema(self, tool, field):
        definition = next(t for t in TOOLS if t['function']['name'] == tool)
        return definition['function']['parameters']['properties'][field]

    def test_full_tool_arguments_fit_the_declared_pattern(self):
        cases = (
            ('read_file', 'path', 'src/core.py'),
            ('read_file', 'path', 'docs/path with spaces/中文.md'),
            ('glob', 'pattern', 'src/**/*.py'),
            ('search_text', 'text', 'UserInputController'),
            ('task', 'prompt', 'Inspect the input path.\nThen implement the agreed change.'),
            ('task', 'model_profile', 'coder'),
            ('request_attention', 'question', 'Which supported format should be documented first?'),
            ('request_attention', 'reason', 'The required model capability is not configured.'),
        )
        for tool, field, value in cases:
            with self.subTest(tool=tool, field=field):
                pattern = self.tool_schema(tool, field)['pattern']
                self.assertIsNotNone(re.fullmatch(pattern, value))

    def test_todo_content_supports_complete_multiline_text(self):
        pattern = TODO_INPUT_SCHEMA['items']['properties']['content']['pattern']
        for value in ('Implement input and output', '保存原始媒体\n验证恢复行为'):
            with self.subTest(value=value):
                self.assertIsNotNone(re.fullmatch(pattern, value))

    def test_nonempty_contract_rejects_whitespace_without_restricting_length(self):
        pattern = NONEMPTY_STRING['pattern']
        for value in ('', ' ', '\t\r\n', '\u2003'):
            with self.subTest(value=value):
                self.assertIsNone(re.search(pattern, value))
                self.assertIsNone(re.fullmatch(pattern, value))
        for value in ('x', 'many characters', ' \nX\t', '中文'):
            with self.subTest(value=value):
                self.assertIsNotNone(re.search(pattern, value))
                self.assertIsNotNone(re.fullmatch(pattern, value))


if __name__ == '__main__':
    unittest.main()
