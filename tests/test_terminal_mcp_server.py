"""Security and behavior tests for the restricted terminal MCP server."""
import unittest
from unittest.mock import patch

from agent import terminal_mcp_server as terminal


class TerminalPolicyTests(unittest.TestCase):
    def test_only_fixed_tasks_are_available(self):
        self.assertEqual(
            set(terminal.TASK_COMMANDS),
            {"run_tests", "git_status", "git_diff_check", "git_log", "python_version"},
        )

    def test_rejects_arbitrary_commands_and_shell_injection(self):
        for task in ("ls", "echo hello", "git status; rm -rf .", "", None, True):
            with self.subTest(task=task), self.assertRaises(ValueError):
                terminal.execute_workspace_task(task)

    @patch("agent.terminal_mcp_server.subprocess.run")
    def test_uses_fixed_argv_no_shell_and_fixed_project_root(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "ok"
        result = terminal.execute_workspace_task("git_status")
        self.assertEqual(result["exit_code"], 0)
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["cwd"], terminal.ROOT)
        self.assertEqual(kwargs["stdin"], terminal.subprocess.DEVNULL)
        self.assertNotIn("MODEL_API_KEY", kwargs["env"])
        self.assertEqual(run.call_args.args[0], ["git", "status", "--short", "--branch"])

    @patch("agent.terminal_mcp_server.subprocess.run")
    def test_output_is_bounded(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "x" * (terminal.MAX_OUTPUT_CHARS + 10)
        result = terminal.execute_workspace_task("python_version")
        self.assertEqual(len(result["output"]), terminal.MAX_OUTPUT_CHARS)
        self.assertTrue(result["output_truncated"])

    @patch("agent.terminal_mcp_server.subprocess.run")
    def test_timeout_is_reported(self, run):
        run.side_effect = terminal.subprocess.TimeoutExpired(["python"], 1, output=b"partial")
        result = terminal.execute_workspace_task("run_tests")
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["output"], "partial")


if __name__ == "__main__":
    unittest.main()
