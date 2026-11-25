"""Actual bounded child, durable queue and recovery, explicit temp-root grant only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.code_task import configured_context, execute_code_report
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database


class CodeTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);(self.root / 'source.py').write_text('def sample():\n    return 1\n')
        self.env = patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': json.dumps({'root': str(self.root), 'approved_files': ['source.py']})})
        self.env.start();self.addCleanup(self.env.stop)
        config, self.digest = configured_context()
        self.payload = {'question': 'Which definition is here?', 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 1}], 'config_sha256': self.digest}

    def test_actual_child_quote_only_capped_context_and_no_path(self):
        result = execute_code_report(self.payload)
        self.assertEqual('def sample():\n', result['citations'][0]['quote'])
        self.assertEqual(self.digest, result['context_config_sha256'])
        self.assertNotIn(str(self.root), str(result))

    def test_no_config_changed_config_and_unapproved_file_fail_before_child(self):
        with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': ''}):
            with self.assertRaisesRegex(ValueError, '^code_task_not_configured$'):
                execute_code_report(self.payload)
        with patch.dict(os.environ, {'WORKSPACE_CODE_CONTEXT': json.dumps({'root': str(self.root), 'approved_files': ['other.py']})}):
            with self.assertRaisesRegex(ValueError, '^code_task_config_changed$'):
                execute_code_report(self.payload)
        bad = {**self.payload, 'selections': [{'path': 'other.py', 'start_line': 1, 'max_lines': 1}]}
        with self.assertRaisesRegex(ValueError, '^code_source_not_approved$'):
            execute_code_report(bad)

    def test_failed_source_sanitized(self):
        (self.root / 'source.py').unlink()
        with self.assertRaisesRegex(ValueError, '^code_task_read_failed$'):
            execute_code_report(self.payload)

    def test_persisted_same_task_report_survives_restore_without_reread(self):
        store = PlanStore(self.root / 'plans.sqlite3')
        plan = store.create_plan('Known local code quote', [{'task_type': 'local_code_report', 'payload': self.payload}, {'task_type': 'workspace_status'}])
        first = store.run_next_step(plan['id']);before = store.get_plan(plan['id'])
        self.assertEqual('checkpointed', before['status'])
        self.assertEqual('local_code_quote_only', before['steps'][0]['result']['kind'])
        (self.root / 'source.py').unlink()
        copy_database(store.db_path, self.root / 'restored.sqlite3')
        restored = PlanStore(self.root / 'restored.sqlite3')
        after = restored.run_until_done(plan['id'])
        self.assertEqual('completed', after['status'])
        self.assertEqual(before['steps'][0], restored.get_plan(plan['id'])['steps'][0])
        self.assertEqual(store.queue.get(first['task_id']), restored.queue.get(first['task_id']))
        self.assertEqual(1, restored.queue.get(first['task_id'])['attempts'])

    def test_payload_cannot_override_root_allowlist_or_exceed_bounds(self):
        for payload in ({**self.payload, 'root': '/'}, {**self.payload, 'approved_files': ['source.py']},
                {**self.payload, 'selections': [{'path': 'source.py', 'start_line': 1, 'max_lines': 51}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                execute_code_report(payload)

    def test_subprocess_launch_errors_sanitized(self):
        with patch('agent.code_task.subprocess.Popen', side_effect=OSError('private root /secret')):
            with self.assertRaisesRegex(ValueError, '^code_task_read_failed$'):
                execute_code_report(self.payload)

    def test_timeout_and_output_limit_stop_child(self):
        from unittest.mock import MagicMock
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = []
        with patch('agent.code_task.selectors.DefaultSelector', return_value=selector):
            with self.assertRaisesRegex(ValueError, '^code_task_timeout$'):
                execute_code_report(self.payload)
        import subprocess,sys
        factory = subprocess.Popen
        def oversized(command, **kwargs):
            return factory([sys.executable, '-c', 'import sys;sys.stdin.read();sys.stdout.write(chr(120)*140000)'], **kwargs)
        with patch('agent.code_task.subprocess.Popen', side_effect=oversized):
            with self.assertRaisesRegex(ValueError, '^code_task_result_too_large$'):
                execute_code_report(self.payload)

    def test_actual_stdio_task_checkpoint_restart_keeps_report_without_source(self):
        import asyncio
        import sys
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        database = self.root / 'stdio.sqlite3'
        parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.plan_mcp_server'],
            cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ,
                'WORKSPACE_PLAN_DB': str(database), 'EXA_API_KEY': '', 'FIRECRAWL_API_KEY': ''})
        async def call(session, tool, arguments):
            result = await session.call_tool(tool, arguments)
            self.assertFalse(result.isError, str(result))
            return json.loads(result.content[0].text)
        async def journey():
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    plan = await call(session, 'create_plan', {'title': 'Known local report', 'steps': [
                        {'task_type': 'local_code_report', 'payload': self.payload}, {'task_type': 'workspace_status'}]})
                    first = await call(session, 'run_next_plan_step', {'plan_id': plan['id']})
                    self.assertEqual('step_succeeded', first['status'])
                    self.assertIn('def sample():', str(first['result']))
            before = PlanStore(database).get_plan(plan['id'])
            (self.root / 'source.py').unlink()
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    same = await call(session, 'get_plan', {'plan_id': plan['id']})
                    self.assertEqual(before, same)
                    done = await call(session, 'run_plan_until_done', {'plan_id': plan['id']})
                    self.assertEqual('completed', done['status'])
                    self.assertEqual(1, len(done['steps_executed']))
            store = PlanStore(database)
            self.assertEqual(before['steps'][0], store.get_plan(plan['id'])['steps'][0])
            self.assertEqual(1, store.queue.get(first['task_id'])['attempts'])
        asyncio.run(journey())

    def test_monitoring_and_context_remain_read_only_for_new_task_type(self):
        from agent.plan_monitoring import plan_snapshot
        from agent.research_context import plan_evidence_envelope
        store = PlanStore(self.root / 'monitor.sqlite3')
        plan = store.create_plan('Code report', [{'task_type': 'local_code_report', 'payload': self.payload}])
        before = store.get_plan(plan['id'])
        with patch.dict(os.environ, {'WORKSPACE_PLAN_DB': str(store.db_path)}):
            result = plan_snapshot()
        self.assertEqual('local_code_report', result['plans'][0]['steps'][0]['task_type'])
        self.assertNotIn('source.py', str(result))
        self.assertNotIn(str(self.root), str(result))
        self.assertEqual(before, store.get_plan(plan['id']))

    def test_failed_report_pauses_without_automatic_source_retry(self):
        store = PlanStore(self.root / 'failure.sqlite3')
        plan = store.create_plan('Missing source', [{'task_type': 'local_code_report', 'payload': self.payload}])
        (self.root / 'source.py').unlink()
        first = store.run_next_step(plan['id'])
        self.assertEqual('paused', first['status'])
        task = store.queue.list_tasks()[0]
        self.assertEqual('code_task_read_failed', task['last_error'])
        self.assertEqual(1, task['attempts'])
        store.supervise(plan['id'])
        self.assertEqual(1, store.queue.get(task['id'])['attempts'])
