"""Actual MCP budget validation must happen before any deterministic dispatch."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent.plan_store import PlanStore


class StrictPlanBudgetTests(unittest.TestCase):
    def test_actual_stdio_invalid_budgets_never_mutate_then_valid_checkpoint_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=PlanStore(root/'plans.sqlite3')
            plan=store.create_plan('Strict batch', [{'task_type':'workspace_status'}]*2)
            before=store.get_plan(plan['id'])
            params=StdioServerParameters(command=sys.executable,args=['-m','agent.plan_mcp_server'],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={**os.environ,'WORKSPACE_PLAN_DB':str(store.db_path)})
            async def journey():
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        for tool in ('run_plan_until_done','supervise_plan'):
                            for field in ('max_steps','max_seconds'):
                                for value in (True,False,1.0,'1',None,0,301):
                                    with self.subTest(tool=tool,field=field,value=value):
                                        result=await session.call_tool(tool,{'plan_id':plan['id'],field:value})
                                        self.assertTrue(result.isError)
                                        self.assertEqual(before,store.get_plan(plan['id']))
                                        self.assertEqual([],store.queue.list_tasks())
                        result=await session.call_tool('run_plan_until_done',{'plan_id':plan['id'],'max_steps':1,'max_seconds':60})
                        self.assertFalse(result.isError)
                        self.assertEqual('checkpointed',json.loads(result.content[0].text)['status'])
                        result=await session.call_tool('supervise_plan',{'plan_id':plan['id'],'max_steps':1,'max_seconds':60})
                        self.assertFalse(result.isError)
                        self.assertEqual('completed',json.loads(result.content[0].text)['status'])
            asyncio.run(journey())
            self.assertEqual([1,1],[task['attempts'] for task in store.queue.list_tasks()])

    def test_tool_input_schemas_identical_to_previous_integer_annotations(self):
        # Build each version in a fresh process to avoid module/global-store leakage.
        repository=Path(__file__).resolve().parents[1]
        current=(repository/'agent/plan_mcp_server.py').read_text()
        previous=current.replace('from pydantic import StrictInt\n','').replace('max_steps: StrictInt','max_steps: int').replace('max_seconds: StrictInt','max_seconds: int').replace('expected_failure_count: StrictInt','expected_failure_count: int')
        runner='''import asyncio,json,sys
namespace={'__name__':'schema_probe'}
exec(sys.stdin.read(),namespace)
async def main():
 tools=await namespace['mcp'].list_tools()
 print(json.dumps({t.name:t.inputSchema for t in tools},sort_keys=True))
asyncio.run(main())
'''
        with tempfile.TemporaryDirectory() as directory:
            outputs=[]
            for source in (previous,current):
                process=subprocess.run([sys.executable,'-c',runner],input=source,text=True,capture_output=True,
                    cwd=repository,env={**os.environ,'WORKSPACE_PLAN_DB':str(Path(directory)/'schema.sqlite3')},timeout=10)
                self.assertEqual(0,process.returncode,process.stderr)
                outputs.append(json.loads(process.stdout))
        self.assertEqual(outputs[0],outputs[1])
