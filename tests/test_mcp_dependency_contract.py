"""The installed MCP API must match all direct local stdio server imports."""
import importlib.metadata
from pathlib import Path
import unittest


class MCPDependencyContractTests(unittest.TestCase):
    def test_base_requirements_pin_direct_fastmcp_dependency(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn('mcp==1.30.0',(root/'requirements.txt').read_text().splitlines())
        self.assertEqual('1.30.0',importlib.metadata.version('mcp'))
        from mcp.server.fastmcp import FastMCP
        self.assertTrue(callable(FastMCP))
