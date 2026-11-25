"""Actual offline research MCP inventory and no-key failure path, no provider."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ResearchMCPJourneyTests(unittest.TestCase):
    def test_unconfigured_stdio_status_and_errors(self):
        parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent.research_mcp_server'],
            cwd=str(Path(__file__).resolve().parents[1]), env={**os.environ,
            'EXA_API_KEY': '', 'FIRECRAWL_API_KEY': ''})
        async def journey():
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({'web_search', 'scrape_url', 'research_provider_status'}, {tool.name for tool in tools.tools})
                    response = await session.call_tool('research_provider_status', {})
                    self.assertFalse(response.isError)
                    status = json.loads(response.content[0].text)
                    self.assertFalse(status['exa_configured'])
                    self.assertFalse(status['firecrawl_configured'])
                    for name, arguments in [('web_search', {'query': 'synthetic offline'}),
                            ('scrape_url', {'url': 'https://example.com/source'}),
                            ('scrape_url', {'url': 'http://127.0.0.1/private'})]:
                        result = await session.call_tool(name, arguments)
                        self.assertTrue(result.isError)
                        self.assertNotIn(str(Path.cwd()), str(result))
        asyncio.run(journey())
