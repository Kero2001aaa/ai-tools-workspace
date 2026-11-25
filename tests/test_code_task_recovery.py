"""Actual restored pending/unknown plan behavior, no provider/model."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from agent.code_task import configured_context
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database


class CodeTaskRecoveryTests(unittest.TestCase):
    def test_pending_restored_config_change_pauses_before_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / 'source.py').write_text('original approved source')
            config = json.dumps({'root': str(root), 'approved_files': ['source.py']})
            with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': config}):
                digest = configured_context()[1]
                store = PlanStore(root / 'original.sqlite3')
                payload = {'question': 'q', 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 1}], 'config_sha256': digest}
                plan = store.create_plan('Pending', [{'task_type': 'local_code_report', 'payload': payload}])
                copy_database(store.db_path, root / 'restored.sqlite3')
            changed = json.dumps({'root': str(root), 'approved_files': ['different.py']})
            with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': changed}), patch('agent.code_task.subprocess.Popen', side_effect=AssertionError('no child')):
                restored = PlanStore(root / 'restored.sqlite3')
                result = restored.run_next_step(plan['id'])
            self.assertEqual('paused', result['status'])
            self.assertEqual('code_task_config_changed', restored.queue.list_tasks()[0]['last_error'])
            self.assertEqual('pending', store.get_plan(plan['id'])['steps'][0]['status'])

    def test_unknown_code_execution_restores_and_waits_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / 'source.sqlite3')
            payload = {'question': 'q', 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 1}], 'config_sha256': 'a' * 64}
            plan = store.create_plan('Unknown result', [{'task_type': 'local_code_report', 'payload': payload}])
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                step = db.execute('SELECT * FROM plan_steps WHERE id=?', (plan['steps'][0]['id'],)).fetchone()
                task = store.queue.bind_plan_step(db, step)
                db.execute("UPDATE tasks SET status='running',attempts=1 WHERE id=?", (task['id'],))
                db.execute("UPDATE plan_steps SET status='running'")
                db.execute("UPDATE plans SET status='running'")
                db.commit()
            copy_database(store.db_path, root / 'restored.sqlite3')
            restored = PlanStore(root / 'restored.sqlite3')
            before = restored.get_plan(plan['id'])
            with patch('agent.code_task.subprocess.Popen', side_effect=AssertionError('no replay')):
                outcome = restored.supervise(plan['id'])
            self.assertEqual('wait', outcome['decision'])
            after = restored.get_plan(plan['id'])
            self.assertEqual('running', after['status'])
            self.assertEqual(before['steps'][0]['id'], after['steps'][0]['id'])
            self.assertEqual(before['steps'][0]['payload'], after['steps'][0]['payload'])
            self.assertIsNone(after['steps'][0]['result'])
            self.assertEqual('running', after['steps'][0]['status'])
            self.assertEqual(1, restored.queue.list_tasks()[0]['attempts'])

    def test_actual_sleeping_child_deadline_terminates_and_reaps(self):
        import subprocess
        import sys
        import time
        from agent.code_task import execute_code_report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / 'source.py').write_text('source')
            config = json.dumps({'root': str(root), 'approved_files': ['source.py']})
            factory = subprocess.Popen;spawned = []
            def sleeping(command, **kwargs):
                child = factory([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
                spawned.append(child)
                return child
            with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': config}):
                digest = configured_context()[1]
                payload = {'question': 'q', 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 1}], 'config_sha256': digest}
                started = time.monotonic()
                with patch('agent.code_task.subprocess.Popen', side_effect=sleeping):
                    with self.assertRaisesRegex(ValueError, '^code_task_timeout$'):
                        execute_code_report(payload)
                self.assertLess(time.monotonic() - started, 10)
                self.assertIsNotNone(spawned[0].poll())
