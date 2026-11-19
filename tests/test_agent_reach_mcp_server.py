"""Tests for the public-only Agent Reach MCP bridge."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent.agent_reach_mcp_server import (
    _run_public,
    agent_reach_github_read,
    agent_reach_v2ex_node,
    _limit,
)


class AgentReachBridgeTests(unittest.TestCase):
    def test_limit_is_bounded_and_rejects_booleans(self):
        self.assertEqual(_limit(10), 10)
        for value in (0, 11, True, "5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _limit(value)

    def test_github_read_rejects_non_github_urls(self):
        for target in ("owner/repo; rm -rf /", "https://evil.example/owner/repo",
                       "https://github.com.evil.example/owner/repo"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                agent_reach_github_read(target)

    def test_v2ex_node_rejects_option_injection(self):
        with self.assertRaises(ValueError):
            agent_reach_v2ex_node("--backend=github-api")

    def test_only_allowlisted_actions_can_run(self):
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            _run_public("twitter", "search", ["anything"])

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server.subprocess.run")
    def test_github_uses_anonymous_api_and_fixed_arguments(self, run, _which):
        run.return_value.stdout = json.dumps({
            "schema_version": 1, "ok": True, "channel": "github",
            "action": "search", "items": [], "error": None,
        })
        run.return_value.returncode = 0
        result = _run_public("github", "search", ["langgraph", "--limit", "3"])
        self.assertTrue(result["ok"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "/usr/bin/agent-reach")
        self.assertIn("--backend", args[0])
        self.assertIn("github-api", args[0])
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("GH_TOKEN", kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        self.assertNotEqual(kwargs["env"]["HOME"], os.path.expanduser("~"))
        self.assertEqual(kwargs["timeout"], 20)

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server.subprocess.run")
    def test_invalid_cli_json_is_rejected(self, run, _which):
        run.return_value.stdout = "not json"
        run.return_value.returncode = 0
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            _run_public("v2ex", "hot", ["--limit", "3"])

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server.subprocess.run")
    def test_failed_cli_result_is_reported(self, run, _which):
        run.return_value.stdout = json.dumps({
            "schema_version": 1, "ok": False,
            "error": {"code": "RATE_LIMITED", "message": "slow down"},
        })
        run.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "RATE_LIMITED"):
            _run_public("v2ex", "hot", ["--limit", "3"])


if __name__ == "__main__":
    unittest.main()
