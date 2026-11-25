"""Reject invalid deterministic task inputs before persistence or attempt usage."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from agent.task_queue import TaskQueue
from agent.plan_store import PlanStore
from agent.task_worker import run_once


class QueueIntakeValidationTests(unittest.TestCase):
    def test_invalid_payloads_and_attempts_never_create_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=TaskQueue(Path(directory)/'queue.db')
            for payload in ([],False,0,'',{'extra':'x'}):
                with self.assertRaises(ValueError):queue.enqueue('workspace_status',payload)
            for payload in ({'limit':True},{'limit':1.5},{'limit':0},{'limit':21},{'extra':1}):
                with self.assertRaises(ValueError):queue.enqueue('memory_recent',payload)
            for attempts in (True,False,1.5,'1',0,11):
                with self.assertRaises(ValueError):queue.enqueue('workspace_status',max_attempts=attempts)
            self.assertEqual([],queue.list_tasks())
            task=queue.enqueue('workspace_status',None,max_attempts=1);self.assertEqual({},task['payload']);self.assertEqual('succeeded',run_once(queue)['status'])
            self.assertEqual(1,queue.get(task['id'])['attempts'])

    def test_plan_and_standalone_share_payload_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store=PlanStore(Path(directory)/'plans.db')
            for kind,payload in [('workspace_status',{'x':1}),('memory_recent',{'limit':False}),('local_code_report',{})]:
                with self.assertRaises(ValueError):store.create_plan('invalid',[{'task_type':kind,'payload':payload}])
                with self.assertRaises(ValueError):store.queue.enqueue(kind,payload)
            self.assertEqual([],store.list_plans());self.assertEqual([],store.queue.list_tasks())

    def test_actual_mcp_invalid_schemas_leave_empty_queue_then_normal_task_succeeds(self):
        from mcp import ClientSession,StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            parameters=StdioServerParameters(command=sys.executable,args=['-m','agent.task_mcp_server'],cwd=directory,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
            async def journey():
                async with stdio_client(parameters) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        for arguments in ({'task_type':'workspace_status','payload':{'extra':'x'}},{'task_type':'memory_recent','payload':{'limit':True}}):
                            self.assertTrue((await session.call_tool('enqueue_task',arguments)).isError)
                        response=await session.call_tool('list_tasks',{});self.assertFalse(response.isError);self.assertEqual([],response.content)
                        response=await session.call_tool('enqueue_task',{'task_type':'workspace_status','max_attempts':1});self.assertFalse(response.isError)
                        response=await session.call_tool('run_one_task',{});self.assertEqual('succeeded',json.loads(response.content[0].text)['status'])
            asyncio.run(journey())
