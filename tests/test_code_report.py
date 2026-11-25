"""Actual approved source file quotes, no inference or source-code execution."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.code_report import build_code_quote_report
from agent.code_evidence import read_code_excerpt


class CodeQuoteReportTests(unittest.TestCase):
    def test_real_repository_question_exact_line_citations(self):
        root = Path(__file__).resolve().parents[1]
        result = build_code_quote_report(root, ['agent/mcp_server.py'], 'Which read-only tools exist?',
            [{'path': 'agent/mcp_server.py', 'start_line': 1, 'max_lines': 100}])
        self.assertEqual('local_code_quote_only', result['kind'])
        self.assertIn('def text_stats(', result['citations'][0]['quote'])
        self.assertIn('def workspace_status(', result['citations'][0]['quote'])
        self.assertNotIn(str(root), str(result))
        self.assertNotIn('Exa', str(result))
        self.assertNotIn('Firecrawl', str(result))
        self.assertEqual('explicit_approved_file_snapshot_only', result['verification'])

    def test_source_change_between_same_file_ranges_fails_whole_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);file = root / 'source.py';file.write_text('first\nsecond\n')
            calls = 0
            def reading(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    file.write_text('changed\nsecond\n')
                return read_code_excerpt(*args, **kwargs)
            with patch('agent.code_report.read_code_excerpt', side_effect=reading):
                with self.assertRaisesRegex(ValueError, '^code_report_source_changed$'):
                    build_code_quote_report(root, ['source.py'], 'Question',
                        [{'path': 'source.py', 'start_line': line, 'max_lines': 1} for line in (1, 2)])

    def test_bad_selection_cannot_inject_quote_or_bypass_allowlist(self):
        root = Path(__file__).resolve().parents[1]
        for selections in ([{'path': 'agent/mcp_server.py', 'start_line': 1, 'max_lines': 1, 'quote': 'invented'}],
                [{'path': 'agent/model.py', 'start_line': 1, 'max_lines': 1}], [{}] * 6):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                build_code_quote_report(root, ['agent/mcp_server.py'], 'Question', selections)

    def test_empty_file_report_refused_and_input_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);file = root / 'empty.py';file.touch()
            with self.assertRaisesRegex(ValueError, '^code_report_empty_quote$'):
                build_code_quote_report(root, ['empty.py'], 'Question', [{'path': 'empty.py', 'start_line': 1, 'max_lines': 1}])
            self.assertEqual(b'', file.read_bytes())

    def test_unicode_question_stable_and_no_reader_called(self):
        with patch('agent.code_report.read_code_excerpt', side_effect=AssertionError('no file read')):
            with self.assertRaisesRegex(ValueError, '^code_report_invalid$'):
                build_code_quote_report(None, [], chr(0xd800), [])
