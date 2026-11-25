"""Real temporary files and links only, no arbitrary-source discovery."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from agent.code_evidence import read_code_excerpt


class CodeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);(self.root / 'src').mkdir()
        self.path = self.root / 'src' / 'sample.py'
        self.path.write_text('first\nsecond\nthird\n')

    def read(self, path='src/sample.py', **kwargs):
        return read_code_excerpt(self.root, ['src/sample.py'], path, **kwargs)

    def test_exact_bytes_line_provenance_and_no_absolute_root(self):
        before = self.path.read_bytes();result = self.read(start_line=2, max_lines=1)
        self.assertEqual('second\n', result['text'])
        self.assertEqual((2, 2, 3), (result['start_line'], result['end_line'], result['total_lines']))
        self.assertEqual(hashlib.sha256(before).hexdigest(), result['file_sha256'])
        self.assertEqual(hashlib.sha256(b'second\n').hexdigest(), result['excerpt_sha256'])
        self.assertTrue(result['truncated']);self.assertNotIn(str(self.root), str(result))
        self.assertEqual(before, self.path.read_bytes())

    def test_unapproved_traversal_absolute_dotfile_and_unsupported_suffix(self):
        for path in ('src/other.py', '../x.py', '/tmp/x.py', 'src/../x.py', '.env', 'src/.hidden.py', 'src//sample.py', 'src/x.bin', 'src\\x.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.read(path)

    def test_symlink_file_and_parent_denied(self):
        target = self.root / 'external.py';target.write_text('external')
        self.path.unlink();self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, '^code_source_unavailable$'):
            self.read()
        self.path.unlink();(self.root / 'src').rmdir()
        other = self.root / 'other';other.mkdir();(other / 'sample.py').write_text('hidden')
        (self.root / 'src').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^code_source_unavailable$'):
            self.read()

    def test_oversize_binary_invalid_utf8_and_fifo_denied(self):
        for data in (b'x' * 65537, b'a\x00b', b'\xff'):
            self.path.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaisesRegex(ValueError, '^code_source_unavailable$'):
                self.read()
        self.path.unlink();os.mkfifo(self.path)
        with self.assertRaisesRegex(ValueError, '^code_source_unavailable$'):
            self.read()

    def test_strict_ranges_and_empty_file(self):
        for kwargs in ({'start_line': True}, {'max_lines': 201}, {'start_line': 4}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)
        self.path.write_text('');result = self.read()
        self.assertEqual('', result['text']);self.assertEqual(0, result['total_lines'])

    def test_controls_kept_as_data_only_when_safe_text_whitespace(self):
        self.path.write_text('a\tvalue\n')
        self.assertEqual('a\tvalue\n', self.read()['text'])
        self.path.write_text('bad\x1bvalue')
        with self.assertRaises(ValueError):
            self.read()

    def test_actual_repository_code_excerpt_known_definition(self):
        root = Path(__file__).resolve().parents[1]
        result = read_code_excerpt(root, ['agent/mcp_server.py'], 'agent/mcp_server.py', start_line=1, max_lines=100)
        self.assertIn('def text_stats(', result['text'])
        self.assertFalse(result['truncated'])
        self.assertEqual('untrusted_source_data', result['trust'])
        self.assertEqual(hashlib.sha256((root / 'agent/mcp_server.py').read_bytes()).hexdigest(), result['file_sha256'])

    def test_descriptor_size_change_refused(self):
        from unittest.mock import patch
        original = os.read
        changed = False
        def reading(fd, size):
            nonlocal changed
            value = original(fd, size)
            if not changed:
                changed = True
                with self.path.open('ab') as output:
                    output.write(b'new data')
            return value
        with patch('agent.code_evidence.os.read', side_effect=reading):
            with self.assertRaisesRegex(ValueError, '^code_source_unavailable$'):
                self.read()
