"""Tests for MCP access to the allowlisted local task queue."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.task_queue import TaskQueue
from agent import task_mcp_server


class TaskMCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = TaskQueue(Path(self.temp.name) / "tasks.sqlite3")
        self.patch_queue = patch.object(task_mcp_server, "task_queue", self.queue)
        self.patch_queue.start()

    def tearDown(self):
        self.patch_queue.stop()
        self.temp.cleanup()

    def test_enqueue_and_list_task(self):
        task = task_mcp_server.enqueue_task("workspace_status")
        self.assertEqual("pending", task["status"])
        listed = task_mcp_server.list_tasks()
        self.assertEqual(task["id"], listed[0]["id"])

    def test_run_one_task_executes_only_one_queued_task(self):
        task = task_mcp_server.enqueue_task("workspace_status")
        result = task_mcp_server.run_one_task()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(task["id"], result["task_id"])
        self.assertEqual("succeeded", self.queue.get(task["id"])["status"])

    def test_rejects_unapproved_task_type(self):
        with self.assertRaises(ValueError):
            task_mcp_server.enqueue_task("shell_command")


if __name__ == "__main__":
    unittest.main()
