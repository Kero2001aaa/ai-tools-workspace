"""Unit tests for the local MCP server tools."""
import unittest

from agent.mcp_server import text_stats, workspace_status


class MCPServerToolTests(unittest.TestCase):
    def test_text_stats(self):
        self.assertEqual(
            text_stats("CNC plasma\nmachine"),
            {"characters": 18, "words": 3, "lines": 2},
        )

    def test_text_stats_empty_input(self):
        self.assertEqual(
            text_stats(""),
            {"characters": 0, "words": 0, "lines": 0},
        )

    def test_workspace_status_is_non_sensitive(self):
        status = workspace_status()
        self.assertEqual(status["workspace"], "ai-tools-workspace")
        self.assertIn("python_version", status)
        self.assertNotIn("cwd", status)
        self.assertNotIn("environment_variables", status)


if __name__ == "__main__":
    unittest.main()
