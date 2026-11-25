"""Known actual project query, restored durable result, fresh process retrieval."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from agent.code_task import configured_context
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database


class SavedReportJourneyTests(unittest.TestCase):
    def test_actual_repo_known_query_saved_restored_retrieved_without_rerun(self):
        repository = Path(__file__).resolve().parents[1]
        relative = 'agent/mcp_server.py'
        data = (repository / relative).read_bytes()
        line = next(i for i, text in enumerate(data.decode().splitlines(), 1) if text.startswith('def text_stats('))
        config = json.dumps({'root': str(repository), 'approved_files': [relative]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': config}):
                digest = configured_context()[1]
                store = PlanStore(root / 'source.sqlite3')
                plan = store.create_plan('Known query', [{'task_type': 'local_code_report', 'payload': {
                    'question': 'Which function returns supplied text statistics?', 'selections': [
                        {'path': relative, 'start_line': line, 'max_lines': 7}], 'config_sha256': digest}},
                    {'task_type': 'workspace_status'}])
                first = store.run_next_step(plan['id'])
            self.assertEqual('step_succeeded', first['status'])
            before = store.get_plan(plan['id'])
            copy_database(store.db_path, root / 'restored.sqlite3')
            command = [sys.executable, '-m', 'agent.code_report_cli', '--db', str(root / 'restored.sqlite3'),
                '--plan-id', plan['id'], '--step-id', plan['steps'][0]['id']]
            process = subprocess.run(command, text=True, capture_output=True, timeout=10, cwd=repository,
                env={**os.environ, 'WORKSPACE_CODE_CONTEXT': '', 'EXA_API_KEY': '', 'FIRECRAWL_API_KEY': ''})
            self.assertEqual(0, process.returncode, process.stderr)
            report = json.loads(process.stdout)
            self.assertIn('def text_stats(', report['citations'][0]['quote'])
            self.assertEqual(first['task_id'], report['task_id'])
            restored = PlanStore(root / 'restored.sqlite3')
            self.assertEqual('completed', restored.run_until_done(plan['id'])['status'])
            self.assertEqual(before['steps'][0], restored.get_plan(plan['id'])['steps'][0])
            self.assertEqual(1, restored.queue.get(first['task_id'])['attempts'])
            self.assertEqual(data, (repository / relative).read_bytes())
