"""Search positions feed the file reader without guessing character locations."""

import json
import tempfile
import unittest
from pathlib import Path

from homebrew_harness.tools.queries import search_text
from homebrew_harness.tools.files import run_read


class SearchOffsets(unittest.TestCase):
    def test_unicode_and_crlf_match_offsets_read_the_matching_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "标题\r\nfirst needle\r\n空行\r\nsecond needle\n"
            (root/"source.py").write_bytes(text.encode())
            results = json.loads(search_text("needle", workdir=root))["matches"]
            self.assertEqual([m["line"] for m in results], [2, 4])
            for match in results:
                page = json.loads(run_read(match["path"], root, offset=match["offset"], limit=1))
                self.assertIn("needle", page["content"])
                self.assertTrue(page["content"].startswith(match["text"].rstrip()))
