"""Opt-in local MCP roundtrip and inventory, no provider or gateway expansion."""
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent.adult import local_stats_mcp_server as server


class LocalStatsMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'state'
        self.environment={'LOCAL_STATS_STATE_DIR':str(self.root),
            'ADULT_PAYLOAD_ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
        self.environment_patch=patch.dict(os.environ,self.environment);self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def test_direct_roundtrip_and_cancel_no_source_echo(self):
        staged=server.local_stats_stage('one two\nthree');task=staged['task_id']
        self.assertEqual(('checkpointed',0),(staged['status'],staged['attempts']))
        self.assertEqual('completed',server.local_stats_run(task)['status'])
        result=server.local_stats_result(task)
        self.assertEqual(3,result['statistics']['words_whitespace'])
        self.assertEqual(1,server.local_stats_run(task)['attempts'])
        self.assertNotIn('one two',json.dumps(result))
        second=server.local_stats_stage('cancel this safely')['task_id']
        self.assertEqual('cancelled',server.local_stats_cancel(second)['status'])
        self.assertEqual(0,server.local_stats_run(second)['attempts'])

    def test_import_has_no_state_and_default_agent_gateway_inventory_unchanged(self):
        from agent.tool_agent import build_workspace_server_config
        from agent.gateway_mcp_server import PUBLIC_TOOLS
        importlib.reload(server)
        self.assertFalse(self.root.exists())
        self.assertNotIn('local_stats',build_workspace_server_config({}))
        self.assertNotIn('adult',build_workspace_server_config({}))
        self.assertNotIn('local_stats',PUBLIC_TOOLS)
        self.assertNotIn('adult',PUBLIC_TOOLS)

    def test_missing_config_invalid_input_and_read_missing_state_fail_closed(self):
        with patch.dict(os.environ,{'LOCAL_STATS_STATE_DIR':''}):
            with self.assertRaisesRegex(ValueError,'local_stats_operation_failed'):
                server.local_stats_stage('safe input')
        with self.assertRaises(ValueError):server.local_stats_stage('')
        with self.assertRaises(ValueError):server.local_stats_get_task('missing-task')
        self.assertFalse(self.root.exists())

    def test_actual_stdio_schema_and_restart_roundtrip(self):
        async def call_sequence():
            parameters=StdioServerParameters(command=sys.executable,
                args=['-m','agent.adult.local_stats_mcp_server'],env={**os.environ,**self.environment})
            async with stdio_client(parameters) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize();tools=await session.list_tools()
                    self.assertEqual({'local_stats_stage','local_stats_get_task','local_stats_run',
                        'local_stats_result','local_stats_cancel'},{tool.name for tool in tools.tools})
                    staged=await session.call_tool('local_stats_stage',{'text':'one two\nthree'})
                    self.assertFalse(staged.isError)
                    task=json.loads(staged.content[0].text)['task_id']
            async with stdio_client(parameters) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    run=await session.call_tool('local_stats_run',{'task_id':task})
                    self.assertFalse(run.isError)
                    self.assertEqual('completed',json.loads(run.content[0].text)['status'])
                    result=await session.call_tool('local_stats_result',{'task_id':task})
                    self.assertEqual(3,json.loads(result.content[0].text)['statistics']['words_whitespace'])
                    bad=await session.call_tool('local_stats_stage',{'text':''})
                    self.assertTrue(bad.isError)
                    self.assertNotIn(str(self.root),str(bad))
        asyncio.run(call_sequence())
