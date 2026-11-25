"""Repeatable known project-source acceptance command, no live model/provider."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class LocalCodeJourneyTests(unittest.TestCase):
    def test_actual_command_new_private_output_and_no_overwrite(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'new-acceptance'
            command = [sys.executable, '-m', 'scripts.local_code_journey', '--output-dir', str(output)]
            process = subprocess.run(command, text=True, capture_output=True, cwd=repository, timeout=15)
            self.assertEqual(0, process.returncode, process.stderr)
            result = json.loads(process.stdout)
            self.assertTrue(result['stored_report_retrieved'])
            self.assertTrue(result['restored_plan_completed'])
            self.assertEqual(1, result['completed_task_attempts'])
            self.assertFalse(result['stage_accepted'])
            self.assertFalse(result['model_called'])
            self.assertIn('def text_stats(', result['report']['citations'][0]['quote'])
            self.assertEqual(result, json.loads((output / 'report.json').read_text()))
            self.assertEqual(0o700, output.stat().st_mode & 0o777)
            self.assertEqual(0o600, (output / 'report.json').stat().st_mode & 0o777)
            before = (output / 'plans.sqlite3').read_bytes()
            again = subprocess.run(command, text=True, capture_output=True, cwd=repository, timeout=15)
            self.assertEqual(1, again.returncode)
            self.assertEqual('local_code_journey_failed\n', again.stderr)
            self.assertEqual(before, (output / 'plans.sqlite3').read_bytes())

    def test_operator_environment_restored_after_project_only_journey(self):
        from scripts.local_code_journey import run_journey
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': 'prior-context'}):
            run_journey(Path(directory) / 'new')
            self.assertEqual('prior-context', os.environ['WORKSPACE_CODE_CONTEXT'])
