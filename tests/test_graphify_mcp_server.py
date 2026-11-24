"""Restricted Graphify boundary tests, no upstream process or real graph data."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from agent import graphify_mcp_server as bridge
from agent.tool_agent import build_workspace_server_config


class GraphifyBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_three_restricted_tools_and_no_path_selectors(self):
        tools = await bridge.mcp.list_tools()
        self.assertEqual({"graphify_graph_stats", "graphify_get_node", "graphify_query_graph"}, {t.name for t in tools})
        for tool in tools:
            self.assertNotIn("project_path", tool.inputSchema.get("properties", {}))
            self.assertNotIn("repo", tool.inputSchema.get("properties", {}))
        with self.assertRaises(ValueError):
            await bridge._query("list_prs", {})
        with self.assertRaises(ValueError):
            await bridge._query("graph_stats", {"project_path": "/other"})

    async def test_graph_validation_rejects_malformed_size_symlink_and_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "graph.json"
            for body in ('not-json', '[]', '{"nodes":[]}', '{"nodes":[],"links":{},"bad":NaN}'):
                p.write_text(body)
                with self.subTest(body=body), self.assertRaises(ValueError):
                    bridge.graph_bytes(p)
            p.write_text('{"nodes":[],"links":[]}')
            self.assertTrue(bridge.graph_bytes(p))
            with patch.object(bridge, 'MAX_GRAPH_BYTES', 3), self.assertRaises(ValueError):
                bridge.graph_bytes(p)
            link = Path(directory) / "link.json"
            link.symlink_to(p)
            with self.assertRaises(ValueError):
                bridge.graph_bytes(link)

    async def test_snapshot_env_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory) / "graph.json"
            raw = b'{"nodes":[],"links":[]}'
            graph.write_bytes(raw)
            (Path(directory) / '.graphify_labels.json').write_text('private sidecar')
            seen = {}
            async def invoke(command, snapshot, home, env, name, arguments):
                seen.update(home=home, snapshot=snapshot)
                self.assertEqual(raw, snapshot.read_bytes())
                self.assertNotEqual(graph, snapshot)
                self.assertEqual(["graph.json"], [p.name for p in Path(home).iterdir()])
                for key in ('MODEL_API_KEY','GH_TOKEN','GITHUB_TOKEN','PYTHONPATH','GRAPHIFY_QUERY_LOG','GRAPHIFY_OUT'):
                    self.assertNotIn(key, env)
                self.assertEqual('1', env['GRAPHIFY_QUERY_LOG_DISABLE'])
                self.assertEqual(home, env['HOME'])
                self.assertEqual(home, env['XDG_CONFIG_HOME'])
                self.assertEqual('query_graph', name)
                self.assertEqual({'question':'test','depth':2,'token_budget':100}, arguments)
                return 'synthetic result'
            with patch.dict(os.environ, {'GRAPHIFY_GRAPH_PATH':str(graph),'MODEL_API_KEY':'secret','GRAPHIFY_QUERY_LOG':'/private/log'}), \
                 patch.object(bridge.shutil,'which',return_value='/synthetic/graphify-mcp'), \
                 patch.object(bridge,'_verify_version') as verify, \
                 patch.object(bridge,'_invoke_snapshot',side_effect=invoke):
                result = await bridge.graphify_query_graph('test',2,100)
                self.assertEqual('synthetic result', result)
                verify.assert_called_once()
            self.assertFalse(Path(seen['home']).exists())

    async def test_version_mismatch_fails_without_invoking_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory)/'graph.json';graph.write_text('{"nodes":[],"links":[]}')
            with patch.dict(os.environ,{'GRAPHIFY_GRAPH_PATH':str(graph)}), \
                 patch.object(bridge.shutil,'which',return_value='/synthetic/graphify-mcp'), \
                 patch.object(bridge,'_verify_version',side_effect=RuntimeError('version mismatch')), \
                 patch.object(bridge,'_invoke_snapshot',new_callable=AsyncMock) as invoke:
                with self.assertRaises(RuntimeError): await bridge.graphify_graph_stats()
                invoke.assert_not_awaited()

    async def test_argument_bounds(self):
        for text in ('', 'x'*1001, '\x00'):
            with self.assertRaises(ValueError): await bridge.graphify_get_node(text)
        for depth,budget in ((True,100),(0,100),(7,100),(1,99),(1,4001),(1,True)):
            with self.assertRaises(ValueError): await bridge.graphify_query_graph('test',depth,budget)

    async def test_timeout_is_bounded_and_private_error_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = Path(directory)/'graph.json';graph.write_text('{"nodes":[],"links":[]}')
            async def wait(*args):
                import asyncio
                await asyncio.sleep(1)
            with patch.dict(os.environ,{'GRAPHIFY_GRAPH_PATH':str(graph)}), \
                 patch.object(bridge.shutil,'which',return_value='/synthetic/graphify-mcp'), \
                 patch.object(bridge,'_verify_version'),patch.object(bridge,'TIMEOUT_SECONDS',0.01), \
                 patch.object(bridge,'_invoke_snapshot',side_effect=wait):
                with self.assertRaisesRegex(RuntimeError,'restricted query failed') as cm: await bridge.graphify_graph_stats()
                self.assertTrue(cm.exception.__suppress_context__)

    def test_version_probe_requires_exact_metadata_and_console_script(self):
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory)/'graphify-mcp'
            import sys
            command.write_text('#!'+sys.executable+'\n')
            for output,code in ((b'0.9.117|1.30.0',0),(b'0.9.118|2.3.0',0)):
                with patch.object(bridge.subprocess,'run',return_value=SimpleNamespace(returncode=code,stdout=output)), self.assertRaises(RuntimeError):
                    bridge._verify_version(str(command),directory,{})
            with patch.object(bridge.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'0.9.118|1.30.0\n')):
                bridge._verify_version(str(command),directory,{})
            command.write_text('#!/usr/bin/env python\n')
            with self.assertRaises(RuntimeError): bridge._interpreter(command)

    def test_results_are_text_only_and_bounded_including_separators(self):
        def result(*texts):
            return SimpleNamespace(content=[SimpleNamespace(type="text",text=t) for t in texts])
        self.assertEqual('a\nb',bridge._result_text(result('a','b')))
        with patch.object(bridge,'MAX_RESULT_CHARS',2), self.assertRaises(RuntimeError):
            bridge._result_text(result('a','b'))
        with self.assertRaises(RuntimeError):
            bridge._result_text(SimpleNamespace(content=[SimpleNamespace(type="image")]))

    async def test_mocked_protocol_uses_snapshot_only_and_rejects_error(self):
        from contextlib import asynccontextmanager
        session = SimpleNamespace(initialize=AsyncMock(),call_tool=AsyncMock(return_value=SimpleNamespace(isError=False,content=[SimpleNamespace(type="text",text="ok")])))
        @asynccontextmanager
        async def client(params,errlog):
            self.assertEqual(['/snapshot/graph.json'],params.args)
            self.assertEqual('/temporary',params.cwd)
            self.assertEqual('1',params.env['GRAPHIFY_QUERY_LOG_DISABLE'])
            yield ('read','write')
        @asynccontextmanager
        async def connected(*args,**kwargs): yield session
        with patch.object(bridge,'stdio_client',side_effect=client),patch.object(bridge,'ClientSession',side_effect=connected):
            result=await bridge._invoke_snapshot('/graphify-mcp',Path('/snapshot/graph.json'),'/temporary',bridge._child_env('/temporary'),'graph_stats',{})
            self.assertEqual('ok',result)
            session.call_tool.assert_awaited_once_with('graph_stats',{})
            session.call_tool.return_value=SimpleNamespace(isError=True,content=[])
            with self.assertRaises(RuntimeError):
                await bridge._invoke_snapshot('/graphify-mcp',Path('/snapshot/graph.json'),'/temporary',bridge._child_env('/temporary'),'graph_stats',{})

    def test_registration_rejects_corrupt_graph_before_starting_process(self):
        with tempfile.TemporaryDirectory() as directory:
            graph=Path(directory)/'graph.json';graph.write_text('bad')
            with patch('agent.tool_agent.shutil.which',return_value='/synthetic/graphify-mcp'),self.assertRaises(ValueError):
                build_workspace_server_config({'GRAPHIFY_GRAPH_PATH':str(graph)})

if __name__=='__main__': unittest.main()
