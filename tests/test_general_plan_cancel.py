import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from agent.plan_store import PlanStore
from agent.plan_recovery import copy_database
from agent.plan_monitoring import plan_snapshot


class GeneralPlanCancelTests(unittest.TestCase):
    def test_checkpoint_cancel_restores_terminal_no_dispatch_and_preserves_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=PlanStore(root/'plans.db')
            plan=store.create_plan('test',[{'task_type':'workspace_status'},{'task_type':'workspace_status'}])
            with patch('agent.task_worker.execute_task',return_value={'synthetic':'saved'}):store.run_next_step(plan['id'])
            before=store.get_plan(plan['id']);cancelled=store.cancel_plan(plan['id'],before['updated_at'])
            self.assertEqual(['succeeded','cancelled'],[s['status'] for s in cancelled['steps']])
            self.assertEqual(before['steps'][0],cancelled['steps'][0])
            copy_database(store.db_path,root/'backup.db');copy_database(root/'backup.db',root/'restored.db');restarted=PlanStore(root/'restored.db')
            with patch('agent.task_worker.execute_task',side_effect=AssertionError('no replay')):
                self.assertEqual('cancelled',restarted.run_next_step(plan['id'])['status'])
                self.assertEqual('stop',restarted.supervise(plan['id'])['decision'])
                self.assertEqual('cancelled',restarted.run_until_done(plan['id'])['status'])
                with self.assertRaises(ValueError):restarted.retry_failed_step(plan['id'],cancelled['steps'][1]['id'])
            with patch.dict(os.environ,{'WORKSPACE_PLAN_DB':str(root/'restored.db')}):self.assertEqual('cancelled',plan_snapshot()['plans'][0]['status'])

    def test_stale_or_running_state_refuses_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            store=PlanStore(Path(directory)/'plans.db');plan=store.create_plan('test',[{'task_type':'workspace_status'}])
            for expected in ('', 'stale'):
                with self.assertRaises(ValueError):store.cancel_plan(plan['id'],expected)
            with sqlite3.connect(store.db_path) as db:db.execute("UPDATE plan_steps SET status='running' WHERE plan_id=?",(plan['id'],))
            before=store.get_plan(plan['id'])
            with self.assertRaisesRegex(ValueError,'running'):store.cancel_plan(plan['id'],before['updated_at'])
            self.assertEqual(before,store.get_plan(plan['id']))

    def test_known_failure_cancel_keeps_attempts_and_error_no_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store=PlanStore(Path(directory)/'plans.db');plan=store.create_plan('test',[{'task_type':'workspace_status'}])
            with patch('agent.task_worker.execute_task',side_effect=ValueError('synthetic failure')):store.run_next_step(plan['id'])
            before=store.get_plan(plan['id']);cancelled=store.cancel_plan(plan['id'],before['updated_at']);self.assertEqual('synthetic failure',cancelled['steps'][0]['last_error'])
            with sqlite3.connect(store.db_path) as db:self.assertEqual(('cancelled',1),db.execute('SELECT status,attempts FROM tasks').fetchone())
            with self.assertRaises(ValueError):store.cancel_plan(plan['id'],cancelled['updated_at'])

    def test_bound_unknown_or_conflicting_task_refuses_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            store=PlanStore(Path(directory)/'plans.db');plan=store.create_plan('test',[{'task_type':'workspace_status'}])
            with store._connect() as db:
                db.execute('BEGIN IMMEDIATE');step=db.execute('SELECT * FROM plan_steps').fetchone();task=store.queue.bind_plan_step(db,step);db.commit()
            for change in ("status='running'", "status='succeeded'", "status='pending',payload='{} ',task_type='memory_recent'"):
                with sqlite3.connect(store.db_path) as db:db.execute('UPDATE tasks SET '+change)
                before=store.get_plan(plan['id']);task_before=store.queue.get(task['id'])
                with self.assertRaisesRegex(ValueError,'reconciled'):store.cancel_plan(plan['id'],before['updated_at'])
                self.assertEqual(before,store.get_plan(plan['id']));self.assertEqual(task_before,store.queue.get(task['id']))

    def test_actual_stdio_cancel_after_checkpoint_restart_no_replay(self):
        import asyncio
        import json
        import sys
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            parameters=StdioServerParameters(command=sys.executable,args=['-m','agent.plan_mcp_server'],cwd=str(Path(__file__).resolve().parents[1]),env={**os.environ,'WORKSPACE_PLAN_DB':str(root/'plans.db')})
            async def call(session,name,arguments):
                response=await session.call_tool(name,arguments);self.assertFalse(response.isError);return json.loads(response.content[0].text)
            async def journey():
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        plan=await call(session,'create_plan',{'title':'cancel fixture','steps':[{'task_type':'workspace_status'},{'task_type':'workspace_status'}]})
                        await call(session,'run_next_plan_step',{'plan_id':plan['id']})
                        current=await call(session,'get_plan',{'plan_id':plan['id']})
                        denied=await session.call_tool('cancel_plan',{'plan_id':plan['id'],'expected_updated_at':plan['updated_at']});self.assertTrue(denied.isError)
                        cancelled=await call(session,'cancel_plan',{'plan_id':plan['id'],'expected_updated_at':current['updated_at']});self.assertEqual('cancelled',cancelled['status'])
                before=PlanStore(root/'plans.db').get_plan(plan['id'])
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize();outcome=await call(session,'supervise_plan',{'plan_id':plan['id']});self.assertEqual(('stop','cancelled'),(outcome['decision'],outcome['status']))
                        self.assertEqual('cancelled',(await call(session,'run_plan_until_done',{'plan_id':plan['id']}))['status'])
                self.assertEqual(before,PlanStore(root/'plans.db').get_plan(plan['id']))
                self.assertEqual(1,len(PlanStore(root/'plans.db').queue.list_tasks()))
            asyncio.run(journey())
