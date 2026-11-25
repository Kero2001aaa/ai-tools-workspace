"""Actual MCP subprocess checkpoint/restart using the existing two read-only tasks."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent.memory_store import MemoryStore
from agent.plan_store import PlanStore


class PlanMCPJourneyTests(unittest.TestCase):
    def test_fifth_failure_mcp_explicit_continue_no_dispatch_then_real_repair(self):
        import uuid
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);memory=MemoryStore(root/'m.sqlite3');memory.remember('synthetic repair note')
            import sqlite3
            with sqlite3.connect(memory.db_path) as db:db.execute("UPDATE memories SET metadata_json='invalid'")
            store=PlanStore(root/'p.sqlite3');plan=store.create_plan('MCP ceiling',[{'task_type':'memory_recent'}])
            # Genuine production worker failures on corrupt synthetic metadata, individually explicit retries.
            from unittest.mock import patch
            with patch.dict(os.environ,{'WORKSPACE_MEMORY_DB':str(memory.db_path)}):
                for index in range(5):
                    if index:store.retry_failed_step(plan['id'],plan['steps'][0]['id'],'synthetic_repair_assertion_unverified')
                    self.assertEqual('paused',store.run_next_step(plan['id'])['status'])
            tid=store.queue.list_tasks()[0]['id']
            parameters=StdioServerParameters(command=sys.executable,args=['-m','agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]),env={**os.environ,
                'WORKSPACE_PLAN_DB':str(store.db_path),'WORKSPACE_MEMORY_DB':str(memory.db_path)})
            async def journey():
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        response=await session.call_tool('get_failure_checkpoint',{'plan_id':plan['id']})
                        state=json.loads(response.content[0].text);self.assertTrue(state['continuation_required'])
                        response=await session.call_tool('retry_failed_plan_step',{'plan_id':plan['id'],'step_id':plan['steps'][0]['id'],'retry_reason':'specific_repair_assertion'})
                        self.assertTrue(response.isError);self.assertIn('EXPLICIT_CONTINUATION_REQUIRED',response.content[0].text)
                        args={'plan_id':plan['id'],'expected_updated_at':state['updated_at'],'expected_failure_count':5,'decision_id':str(uuid.uuid4())}
                        response=await session.call_tool('continue_after_decision',args)
                        self.assertFalse(response.isError);self.assertFalse(json.loads(response.content[0].text)['dispatched'])
                        response=await session.call_tool('continue_after_decision',args);self.assertTrue(response.isError)
                        self.assertEqual(5,store.queue.get(tid)['attempts'])
                        response=await session.call_tool('retry_failed_plan_step',{'plan_id':plan['id'],'step_id':plan['steps'][0]['id']})
                        self.assertTrue(response.isError);self.assertIn('REPAIR_ASSERTION_REQUIRED',response.content[0].text)
                        with sqlite3.connect(memory.db_path) as db:db.execute("UPDATE memories SET metadata_json='{}'")
                        response=await session.call_tool('retry_failed_plan_step',{'plan_id':plan['id'],'step_id':plan['steps'][0]['id'],'retry_reason':'synthetic_memory_metadata_repaired'})
                        self.assertFalse(response.isError)
                        response=await session.call_tool('run_next_plan_step',{'plan_id':plan['id']})
                        self.assertFalse(response.isError);self.assertEqual('completed',json.loads(response.content[0].text)['plan_status'])
            asyncio.run(journey())
            self.assertEqual(6,store.queue.get(tid)['attempts'])

    def test_stdio_restart_memory_scope_and_completed_no_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / 'memory.sqlite3')
            memory.remember('general MCP acceptance note')
            memory.adult_scope().remember('privatemcpuniquemarker', memory_type='note')
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ,
                'WORKSPACE_PLAN_DB': str(root / 'plans.sqlite3'), 'WORKSPACE_MEMORY_DB': str(root / 'memory.sqlite3')})
            async def journey():
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual({'create_plan', 'get_plan', 'list_plans', 'run_next_plan_step',
                            'run_plan_until_done', 'supervise_plan', 'retry_failed_plan_step', 'cancel_plan', 'get_failure_checkpoint', 'continue_after_decision'}, {tool.name for tool in tools.tools})
                        response = await session.call_tool('create_plan', {'title': 'Real MCP checkpoint', 'steps': [
                            {'task_type': 'workspace_status'}, {'task_type': 'memory_recent', 'payload': {'limit': 5}}]})
                        self.assertFalse(response.isError)
                        plan = json.loads(response.content[0].text)
                        response = await session.call_tool('run_plan_until_done', {'plan_id': plan['id'], 'max_steps': 1})
                        self.assertFalse(response.isError)
                        self.assertEqual('checkpointed', json.loads(response.content[0].text)['status'])
                before = PlanStore(root / 'plans.sqlite3').get_plan(plan['id'])
                task_before = PlanStore(root / 'plans.sqlite3').queue.list_tasks()[0]
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        response = await session.call_tool('supervise_plan', {'plan_id': plan['id'], 'max_steps': 1})
                        self.assertFalse(response.isError)
                        self.assertEqual('completed', json.loads(response.content[0].text)['status'])
                        response = await session.call_tool('run_plan_until_done', {'plan_id': plan['id']})
                        self.assertEqual([], json.loads(response.content[0].text)['steps_executed'])
                        denied = await session.call_tool('create_plan', {'title': 'No Adult bridge', 'steps': [{'task_type': 'image.generate'}]})
                        self.assertTrue(denied.isError)
                store = PlanStore(root / 'plans.sqlite3')
                after = store.get_plan(plan['id'])
                self.assertEqual(before['steps'][0], after['steps'][0])
                self.assertEqual(task_before, store.queue.get(task_before['id']))
                self.assertEqual(2, len(store.queue.list_tasks()))
                self.assertEqual([1, 1], [task['attempts'] for task in store.queue.list_tasks()])
                self.assertEqual(['general MCP acceptance note'], [row['content'] for row in after['steps'][1]['result']['memories']])
                self.assertNotIn('privatemcpuniquemarker', json.dumps(after))
            asyncio.run(journey())

    def test_stdio_known_failure_waits_until_explicit_retry_after_reopen(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / 'memory.sqlite3')
            memory.remember('synthetic retry note')
            with sqlite3.connect(memory.db_path) as db:
                db.execute("UPDATE memories SET metadata_json='invalid'")
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ,
                'WORKSPACE_PLAN_DB': str(root / 'plans.sqlite3'), 'WORKSPACE_MEMORY_DB': str(memory.db_path)})
            async def call(session, name, arguments):
                response = await session.call_tool(name, arguments)
                self.assertFalse(response.isError)
                return json.loads(response.content[0].text)
            async def journey():
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        plan = await call(session, 'create_plan', {'title': 'Known failure', 'steps': [
                            {'task_type': 'workspace_status'}, {'task_type': 'memory_recent'}]})
                        result = await call(session, 'run_plan_until_done', {'plan_id': plan['id']})
                        self.assertEqual('paused', result['status'])
                store = PlanStore(root / 'plans.sqlite3')
                before = store.get_plan(plan['id'])['steps'][0]
                failed = next(task for task in store.queue.list_tasks() if task['status'] == 'failed')
                with sqlite3.connect(memory.db_path) as db:
                    db.execute("UPDATE memories SET metadata_json='{}'")
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        waiting = await call(session, 'supervise_plan', {'plan_id': plan['id']})
                        self.assertEqual('paused', waiting['status'])
                        self.assertEqual(1, store.queue.get(failed['id'])['attempts'])
                        refused = await session.call_tool('retry_failed_plan_step', {'plan_id': plan['id'], 'step_id': plan['steps'][1]['id']})
                        self.assertTrue(refused.isError)
                        self.assertIn('REPAIR_ASSERTION_REQUIRED', refused.content[0].text)
                        self.assertEqual('failed', store.queue.get(failed['id'])['status'])
                        self.assertEqual(1, len(store.queue.list_plan_attempts(failed['id'])))
                        retried = await call(session, 'retry_failed_plan_step', {'plan_id': plan['id'], 'step_id': plan['steps'][1]['id'], 'retry_reason': 'synthetic_memory_metadata_repaired'})
                        self.assertEqual('queued', retried['status'])
                        result = await call(session, 'run_plan_until_done', {'plan_id': plan['id']})
                        self.assertEqual('completed', result['status'])
                self.assertEqual(before, store.get_plan(plan['id'])['steps'][0])
                self.assertEqual(2, store.queue.get(failed['id'])['attempts'])
                self.assertEqual('succeeded', store.queue.get(failed['id'])['status'])
                self.assertEqual(2, len(store.queue.list_tasks()))
            asyncio.run(journey())

    def test_stdio_legacy_unknown_running_waits_without_minting_task(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlanStore(root / 'plans.sqlite3')
            plan = store.create_plan('Legacy unknown outcome', [{'task_type': 'workspace_status'}])
            with sqlite3.connect(store.db_path) as db:
                db.execute("UPDATE plan_steps SET status='running'")
                db.execute("UPDATE plans SET status='running'")
            before = store.get_plan(plan['id'])
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ, 'WORKSPACE_PLAN_DB': str(store.db_path)})
            async def journey():
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for tool in ('supervise_plan', 'run_next_plan_step'):
                            response = await session.call_tool(tool, {'plan_id': plan['id']})
                            self.assertFalse(response.isError)
                            result = json.loads(response.content[0].text)
                            self.assertEqual('running', result['status'])
                            if tool == 'supervise_plan':
                                self.assertEqual('wait', result['decision'])
                self.assertEqual(before, store.get_plan(plan['id']))
                self.assertEqual([], store.queue.list_tasks())
            asyncio.run(journey())

    def test_stdio_resumes_clean_restored_plan_and_memory_databases(self):
        from agent.plan_recovery import copy_database
        from agent.memory_recovery import copy_memory_database
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / 'source-memory.sqlite3')
            memory.remember('restored general MCP note')
            memory.adult_scope().remember('restoredprivatemcpmarker', memory_type='note')
            store = PlanStore(root / 'source-plans.sqlite3')
            plan = store.create_plan('Restored MCP', [{'task_type': 'workspace_status'}, {'task_type': 'memory_recent'}])
            first = store.run_next_step(plan['id'])
            before = store.get_plan(plan['id'])
            copy_database(store.db_path, root / 'restored-plans.sqlite3')
            copy_memory_database(memory.db_path, root / 'restored-memory.sqlite3')
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ,
                'WORKSPACE_PLAN_DB': str(root / 'restored-plans.sqlite3'),
                'WORKSPACE_MEMORY_DB': str(root / 'restored-memory.sqlite3')})
            async def journey():
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        response = await session.call_tool('run_plan_until_done', {'plan_id': plan['id']})
                        self.assertFalse(response.isError)
                        self.assertEqual(1, len(json.loads(response.content[0].text)['steps_executed']))
            asyncio.run(journey())
            recovered = PlanStore(root / 'restored-plans.sqlite3')
            after = recovered.get_plan(plan['id'])
            self.assertEqual('completed', after['status'])
            self.assertEqual(before['steps'][0], after['steps'][0])
            self.assertEqual(store.queue.get(first['task_id']), recovered.queue.get(first['task_id']))
            self.assertEqual(['restored general MCP note'], [row['content'] for row in after['steps'][1]['result']['memories']])
            self.assertNotIn('restoredprivatemcpmarker', json.dumps(after))
            self.assertEqual(before, store.get_plan(plan['id']))


if __name__ == '__main__':
    unittest.main()
