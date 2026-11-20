"""Security and behavior tests for the restricted terminal MCP server."""
import tempfile
import unittest
from pathlib import Path
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
        self.assertEqual(Path(run.call_args.args[0][0]).name, "git")
        self.assertEqual(run.call_args.args[0][1:], ["status", "--short", "--branch"])

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


class ProjectFilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "agent").mkdir()
        (self.root / "tests").mkdir()
        self.patch_root = patch.object(terminal, "ROOT", self.root)
        self.patch_root.start()

    def tearDown(self):
        self.patch_root.stop()
        self.temp.cleanup()

    def test_write_and_read_project_file(self):
        result = terminal._write_project_file("agent/example.py", "VALUE = 42\n")
        self.assertEqual(result["bytes_written"], len("VALUE = 42\n".encode()))
        read = terminal._read_project_file("agent/example.py")
        self.assertEqual(read["content"], "VALUE = 42\n")

    def test_rejects_traversal_and_protected_paths(self):
        for path in ("../outside.txt", ".env", ".git/config", ".venv/bin/python",
                     ".data/secrets.db", ".vscode/settings.json", "random/file.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                terminal._resolve_project_path(path)

    def test_rejects_symlink_escape(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir(exist_ok=True)
        try:
            (self.root / "agent" / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                terminal._resolve_project_path("agent/escape/payload.txt")
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_enforces_write_size_limit(self):
        with self.assertRaisesRegex(ValueError, "write limit"):
            terminal._write_project_file("agent/large.py", "x" * (terminal.MAX_WRITE_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
