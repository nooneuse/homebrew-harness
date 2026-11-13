"""Reviewer evidence pages keep Unicode offsets, coverage and file boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.security.review_evidence import ReviewEvidence, EvidenceLimitReached, FILE_BYTES, MAX_FILES, MAX_PAGES


class EvidencePages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.evidence = ReviewEvidence(self.root)

    def test_unicode_pages_reconstruct_the_file_and_keep_one_version(self):
        content = '汉字🌱\r\n' * 2500
        (self.root / 'guide.md').write_bytes(content.encode())
        first = self.evidence.read({'path': 'guide.md'})
        self.assertTrue(first['truncated'])
        self.assertFalse(first['read_complete'])
        self.assertEqual(first['next_offset'], 10000)
        second = self.evidence.read({'path': 'guide.md', 'offset': first['next_offset']})
        self.assertFalse(second['truncated'])
        self.assertTrue(second['read_complete'])
        self.assertEqual(first['content'] + second['content'], content)
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(len(self.evidence.verify()["evidence"]), 1)

    def test_holes_and_overlapping_pages_do_not_claim_complete_coverage(self):
        (self.root / 'guide').write_text('0123456789')
        tail = self.evidence.read({'path': 'guide', 'offset': 6, 'limit': 4})
        self.assertIsNone(tail['next_offset'])
        self.assertFalse(tail['read_complete'])
        self.evidence.read({'path': 'guide', 'offset': 0, 'limit': 4})
        self.assertFalse(self.evidence.verify()["evidence"][0]['read_complete'])
        full = self.evidence.read({'path': 'guide', 'offset': 3, 'limit': 4})
        self.assertTrue(full['read_complete'])

    def test_missing_leaf_and_parent_are_negative_observations(self):
        for path in ('missing.txt', 'missing-parent/file.txt'):
            result = self.evidence.read({'path': path})
            self.assertEqual(result['error']['code'], 'not_found')
        (self.root / 'missing.txt').write_text('Now present')
        check = self.evidence.verify()
        self.assertEqual(check['changes'][0]['current_status'], 'present')

    def test_empty_file_is_complete(self):
        (self.root / 'empty').write_text('')
        page = self.evidence.read({'path': 'empty'})
        self.assertEqual(page['content'], '')
        self.assertTrue(page['read_complete'])

    def test_changed_and_replaced_files_cannot_mix_pages(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                path = self.root / 'guide'
                path.write_text('original text')
                evidence = ReviewEvidence(self.root)
                evidence.read({'path': 'guide', 'limit': 4})
                if replacement:
                    alternate = self.root / 'new-guide'
                    alternate.write_text('original text')
                    alternate.replace(path)
                else:
                    path.write_text('modified text')
                check = evidence.verify()
                self.assertEqual(len(check['changes']), 1)
                page = evidence.read({'path': 'guide', 'offset': 4})
                self.assertTrue(page['ok'])
                self.assertEqual(evidence.verify()['changes'], [])

    def test_invalid_bounds_and_fields_are_rejected(self):
        (self.root / 'guide').write_text('text')
        invalid = [{'path': 'guide', 'offset': v} for v in (-1, True, 1.5, '0', FILE_BYTES + 1)]
        invalid += [{'path': 'guide', 'limit': v} for v in (0, True, 1.5, '4', 10001)]
        invalid += [{}, [], {'path': ''}, {'path': 12}, {'path': 'guide', 'extra': 1}, {'path': 'guide', 'offset': 5}]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertFalse(self.evidence.read(arguments)['ok'])

    def test_sensitive_paths_traversal_and_symlinks_remain_unreadable(self):
        (self.root / '.env').write_text('private')
        (self.root / '.harness').mkdir()
        (self.root / '.harness/state.json').write_text('private')
        (self.root / 'guide').write_text('text')
        (self.root / 'linked').symlink_to(self.root / 'guide')
        for name in ('.env', '.harness/state.json', '../outside', 'linked'):
            with self.subTest(path=name):
                result = self.evidence.read({'path': name})
                self.assertFalse(result['ok'])
                self.assertNotIn('content', result)

    def test_byte_file_and_page_limits_are_enforced(self):
        (self.root / 'too-large').write_bytes(b'x' * (FILE_BYTES + 1))
        with self.assertRaisesRegex(EvidenceLimitReached, 'size limit'):
            self.evidence.read({'path': 'too-large'})
        self.evidence = ReviewEvidence(self.root)
        (self.root / 'guide').write_text('text')
        for _ in range(MAX_PAGES):
            self.evidence.read({'path': 'guide'})
        with self.assertRaisesRegex(EvidenceLimitReached, 'page limit'):
            self.evidence.read({'path': 'guide'})
        evidence = ReviewEvidence(self.root)
        for index in range(MAX_FILES):
            (self.root / str(index)).write_text('text')
            evidence.read({'path': str(index)})
        with self.assertRaisesRegex(EvidenceLimitReached, 'file limit'):
            evidence.read({'path': 'guide'})
        with patch('homebrew_harness.security.review_evidence.TOTAL_BYTES', 7):
            evidence = ReviewEvidence(self.root)
            evidence.read({'path': 'guide'})
            with self.assertRaisesRegex(EvidenceLimitReached, 'file limit'):
                evidence.read({'path': '0'})
