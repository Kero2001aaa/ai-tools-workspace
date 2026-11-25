"""Actual saved-plan result retrieval, no source reread or execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from agent.code_report_cli import read_saved_report
from agent.code_task import configured_context
from agent.plan_store import PlanStore


class SavedCodeReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.source = self.root / 'source.py'
        self.source.write_text('def sample():\n    return 1\n')
        config = json.dumps({'root': str(self.root), 'approved_files': ['source.py']})
        with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': config}):
            digest = configured_context()[1]
            self.store = PlanStore(self.root / 'plans.sqlite3')
            self.plan = self.store.create_plan('Private title', [{'task_type': 'local_code_report', 'payload': {
                'question': 'Which function?\x1b[31m', 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 1}], 'config_sha256': digest}}])
            self.first = self.store.run_next_step(self.plan['id'])
        self.step = self.plan['steps'][0]['id']

    def read(self):
        return read_saved_report(self.store.db_path, self.plan['id'], self.step)

    def test_same_saved_result_without_source_or_config_no_state_change(self):
        before = self.store.get_plan(self.plan['id']);self.source.unlink()
        with patch('agent.code_task.execute_code_report', side_effect=AssertionError('no worker')):
            result = self.read()
        self.assertEqual(self.first['task_id'], result['task_id'])
        self.assertEqual('def sample():\n', result['citations'][0]['quote'])
        self.assertEqual(before, self.store.get_plan(self.plan['id']))
        self.assertNotIn('Private title', str(result));self.assertNotIn(str(self.root), str(result))

    def test_cli_actual_fresh_process_safe_escapes_controls(self):
        process = subprocess.run([sys.executable, '-m', 'agent.code_report_cli', '--db', str(self.store.db_path),
            '--plan-id', self.plan['id'], '--step-id', self.step], cwd=Path(__file__).resolve().parents[1],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertNotIn(chr(27), process.stdout)
        self.assertIn('Which function?', json.loads(process.stdout)['question'])

    def test_missing_pair_or_file_never_created(self):
        missing = self.root / 'missing.sqlite3'
        with self.assertRaisesRegex(ValueError, '^saved_code_report_unavailable$'):
            read_saved_report(missing, self.plan['id'], self.step)
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            read_saved_report(self.store.db_path, self.step, self.plan['id'])

    def test_tampered_queue_step_mismatch_refused(self):
        with self.store._connect() as db:
            db.execute("UPDATE tasks SET result='{}'")
        with self.assertRaisesRegex(ValueError, '^saved_code_report_unavailable$'):
            self.read()

    def test_matching_but_bad_quote_digest_refused(self):
        with self.store._connect() as db:
            row = db.execute('SELECT result FROM tasks').fetchone()[0]
            result = json.loads(row);result['citations'][0]['quote'] = 'tampered'
            encoded = json.dumps(result)
            db.execute('UPDATE tasks SET result=?', (encoded,));db.execute('UPDATE plan_steps SET result=?', (encoded,))
        with self.assertRaises(ValueError):
            self.read()

    def test_pending_wrong_type_or_oversize_report_refused(self):
        before = self.store.get_plan(self.plan['id'])
        with self.store._connect() as db:
            db.execute("UPDATE plan_steps SET status='pending'")
        with self.assertRaises(ValueError):
            self.read()
        with self.store._connect() as db:
            db.execute("UPDATE plan_steps SET status='succeeded',result=?", ('x' * 262145,))
            db.execute('UPDATE tasks SET result=?', ('x' * 262145,))
        with self.assertRaisesRegex(ValueError, '^saved_code_report_unavailable$'):
            self.read()
