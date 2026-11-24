"""Tests for the public-only Agent Reach MCP bridge."""
import json
import os
import tempfile
import unittest
import sys
import time
from pathlib import Path
from unittest.mock import patch

from agent.agent_reach_mcp_server import (
    _run_public, _public_env, _capture_bounded,
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
    @patch("agent.agent_reach_mcp_server._capture_bounded")
    def test_github_uses_anonymous_api_and_fixed_arguments(self, run, _which):
        payload = json.dumps({
            "schema_version": 1, "ok": True, "channel": "github",
            "action": "search", "items": [], "error": None,
        })
        run.return_value = (payload, 0)
        result = _run_public("github", "search", ["langgraph", "--limit", "3"])
        self.assertTrue(result["ok"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "/usr/bin/agent-reach")
        self.assertIn("--backend", args[0])
        self.assertIn("github-api", args[0])
        env = args[2]
        self.assertEqual(env, _public_env(args[1]))
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotEqual(env["HOME"], os.path.expanduser("~"))
        self.assertEqual(env["PATH"], os.defpath)

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server._capture_bounded")
    def test_invalid_cli_json_is_rejected(self, run, _which):
        payload = "not json"
        run.return_value = (payload, 0)
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            _run_public("v2ex", "hot", ["--limit", "3"])

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server._capture_bounded")
    def test_failed_cli_result_is_reported(self, run, _which):
        payload = json.dumps({
            "schema_version": 1, "ok": False,
            "error": {"code": "RATE_LIMITED", "message": "slow down"},
        })
        run.return_value = (payload, 1)
        with self.assertRaisesRegex(RuntimeError, "RATE_LIMITED"):
            _run_public("v2ex", "hot", ["--limit", "3"])

    @patch("agent.agent_reach_mcp_server.shutil.which", return_value="/usr/bin/agent-reach")
    @patch("agent.agent_reach_mcp_server._capture_bounded")
    def test_nonzero_exit_cannot_claim_success(self, capture, _which):
        capture.return_value = (json.dumps({"schema_version": 1, "ok": True}), 9)
        with self.assertRaisesRegex(RuntimeError, "exited unsuccessfully"):
            _run_public("v2ex", "hot", [])

    def test_environment_is_allowlisted_not_a_token_denylist(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
                                    "PYTHONPATH": "/private", "XDG_CONFIG_HOME": "/private",
                                    "HTTPS_PROXY": "http://private", "GIT_CONFIG_COUNT": "1"}):
            env = _public_env("/temporary")
        for key in ("MODEL_API_KEY", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "HTTPS_PROXY", "GIT_CONFIG_COUNT"):
            self.assertNotIn(key, env)
        self.assertEqual("/temporary", env["XDG_CONFIG_HOME"])

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_real_process_stdout_stderr_and_exit(self):
        with tempfile.TemporaryDirectory() as home:
            output, code = _capture_bounded([sys.executable, "-c", "import sys;print('ok');print('hidden',file=sys.stderr);sys.exit(7)"], home, _public_env(home))
        self.assertEqual("ok\n", output)
        self.assertEqual(7, code)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_real_process_output_floods_are_stopped(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as home, \
                 patch("agent.agent_reach_mcp_server._MAX_OUTPUT_BYTES", 1024):
                with self.assertRaisesRegex(RuntimeError, "safety limit"):
                    _capture_bounded([sys.executable, "-c", f"import sys;sys.{stream}.write('x'*1000000)"], home, _public_env(home))

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_real_process_timeout_kills_descendants(self):
        with tempfile.TemporaryDirectory() as home, \
             patch("agent.agent_reach_mcp_server._TIMEOUT_SECONDS", 0.25):
            marker = Path(home) / "must-not-exist"
            child = "import time,pathlib;time.sleep(0.8);pathlib.Path('must-not-exist').touch()"
            code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);time.sleep(5)"
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                _capture_bounded([sys.executable, "-c", code], home, _public_env(home))
            time.sleep(1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
