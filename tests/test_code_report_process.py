"""Real code report rebuilt in a fresh Python process, no source execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from agent.code_report import build_code_quote_report


class CodeReportProcessTests(unittest.TestCase):
    def test_fresh_process_returns_identical_report_with_explicit_context(self):
        root = Path(__file__).resolve().parents[1]
        context = {'root': str(root), 'approved_files': ['agent/mcp_server.py'],
            'question': 'What read-only tools exist in the supplied file?',
            'selections': [{'path': 'agent/mcp_server.py', 'start_line': 1, 'max_lines': 100}]}
        expected = build_code_quote_report(**context)
        code = 'import json,sys;from agent.code_report import build_code_quote_report;print(json.dumps(build_code_quote_report(**json.load(sys.stdin))))'
        process = subprocess.run([sys.executable, '-c', code], input=json.dumps(context),
            text=True, capture_output=True, timeout=10, cwd=root,
            env={**os.environ, 'EXA_API_KEY': '', 'FIRECRAWL_API_KEY': ''})
        self.assertEqual(0, process.returncode, process.stderr)
        rebuilt = json.loads(process.stdout)
        self.assertEqual(expected, rebuilt)
        self.assertNotIn(str(root), process.stdout)
        self.assertEqual('explicit_approved_file_snapshot_only', rebuilt['verification'])
        self.assertIn('def text_stats(', rebuilt['citations'][0]['quote'])
