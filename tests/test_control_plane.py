"""Tests for the read-only local control plane."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from agent.control_plane import Handler, status_snapshot, task_snapshot, memory_snapshot
from agent.memory_store import MemoryStore
from agent.task_queue import TaskQueue


class ControlPlaneTests(unittest.TestCase):
    def test_status_does_not_expose_secret_values(self):
        with patch.dict(os.environ, {
            "MODEL_API_KEY": "do-not-leak-this",
            "MODEL_BASE_URL": "https://example.invalid/v1",
            "MODEL_NAME": "test-model",
            "ENABLE_AGENT_REACH_TOOLS": "true",
        }):
            snapshot = status_snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn("do-not-leak-this", encoded)
        self.assertTrue(snapshot["integrations"]["Model endpoint"])
        self.assertTrue(snapshot["integrations"]["Agent-Reach"])
        self.assertTrue(snapshot["security"]["read_only_dashboard"])

    def test_memory_and_task_snapshots_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.sqlite3"
            task_path = Path(directory) / "tasks.sqlite3"
            store = MemoryStore(memory_path)
            store.remember("dashboard smoke test")
            queue = TaskQueue(task_path)
            queue.enqueue("workspace_status")
            with patch("agent.control_plane.MemoryStore", return_value=store), \
                 patch("agent.control_plane.TaskQueue", return_value=queue):
                self.assertEqual(1, len(memory_snapshot(10)))
                self.assertEqual(1, len(task_snapshot(20)))

    def test_http_dashboard_and_json_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertIn("AI Workspace Control Plane", response.read().decode())
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertEqual("ai-tools-workspace", payload["workspace"])
            connection.request("POST", "/api/tasks")
            response = connection.getresponse()
            self.assertEqual(405, response.status)
            connection.request("GET", "/not-a-route")
            response = connection.getresponse()
            self.assertEqual(404, response.status)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
