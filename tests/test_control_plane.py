"""Tests for the read-only local control plane."""
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from http.server import ThreadingHTTPServer

from agent.control_plane import Handler, allowed_authorities, _page, status_snapshot, task_snapshot, memory_snapshot
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
        self.assertTrue(snapshot["security"]["authenticated_task_controls"])

    def test_memory_and_task_snapshots_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.sqlite3"
            task_path = Path(directory) / "tasks.sqlite3"
            store = MemoryStore(memory_path)
            store.remember("dashboard smoke test")
            queue = TaskQueue(task_path)
            queue.enqueue("workspace_status")
            with patch("agent.control_plane.MemoryStore", return_value=store), \
                 patch("agent.task_monitoring.DEFAULT_DB", task_path):
                self.assertEqual(1, len(memory_snapshot(10)))
                self.assertEqual(1, len(task_snapshot(20)))

    def test_authenticated_task_controls_and_audit_log(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = TaskQueue(Path(directory) / "tasks.sqlite3")
            audit_path = Path(directory) / "audit.sqlite3"
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.control_token = "test-one-time-token"
            server.action_lock = threading.Lock()
            server.action_times = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                body = json.dumps({"task_type": "workspace_status", "payload": {}, "max_attempts": 1})
                connection.request("POST", "/api/tasks/enqueue", body=body,
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(401, response.status)
                response.read()

                connection.request("POST", "/api/tasks/enqueue", body=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-one-time-token",
                    "Origin": "https://attacker.example",
                })
                response = connection.getresponse()
                self.assertEqual(403, response.status)
                response.read()

                with patch("agent.control_plane.TaskQueue", return_value=queue), patch.dict(os.environ, {"CONTROL_AUDIT_DB": str(audit_path)}):
                    connection.request("POST", "/api/tasks/enqueue", body=body, headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-one-time-token",
                    })
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(201, response.status)
                    self.assertEqual("queued", payload["status"])
                    self.assertEqual("workspace_status", payload["task_type"])

                    with patch("agent.control_plane.run_once", return_value={
                        "status": "succeeded", "task_id": "test-task",
                    }):
                        connection.request("POST", "/api/tasks/run-once", body="{}", headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer test-one-time-token",
                        })
                        response = connection.getresponse()
                        result = json.loads(response.read())
                        self.assertEqual(200, response.status)
                        self.assertEqual("succeeded", result["status"])

                with sqlite3.connect(audit_path) as db:
                    count = db.execute("SELECT COUNT(*) FROM control_audit").fetchone()[0]
                self.assertEqual(2, count)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_agent_is_disabled_by_default(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.control_token = "test-token"
        server.action_lock = threading.Lock()
        server.action_times = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            with patch.dict(os.environ, {"ENABLE_DASHBOARD_AGENT": ""}):
                connection.request("POST", "/api/agent/run", body=json.dumps({"request": "hello"}),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"})
                response = connection.getresponse()
                self.assertEqual(503, response.status)
                payload = json.loads(response.read())
                self.assertIn("disabled", payload["error"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_agent_requires_auth_and_runs_explicit_request(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.sqlite3"
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.control_token = "test-token"
            server.action_lock = threading.Lock()
            server.action_times = []
            server.agent_lock = threading.Lock()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                body = json.dumps({"request": "Summarize the project architecture"})
                with patch.dict(os.environ, {"ENABLE_DASHBOARD_AGENT": "true", "CONTROL_AUDIT_DB": str(audit_path)}), \
                     patch("agent.tool_agent.run_request", new=AsyncMock(return_value="Architecture summary")) as run_request:
                    connection.request("POST", "/api/agent/run", body=body,
                        headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(401, response.status)
                    response.read()

                    connection.request("POST", "/api/agent/run", body=body,
                        headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(200, response.status)
                    self.assertEqual("Architecture summary", payload["answer"])
                    run_request.assert_awaited_once_with("Summarize the project architecture")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_agent_rejects_extra_fields(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.control_token = "test-token"
        server.action_lock = threading.Lock()
        server.action_times = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            with patch.dict(os.environ, {"ENABLE_DASHBOARD_AGENT": "true"}):
                connection.request("POST", "/api/agent/run", body=json.dumps({"request": "hello", "tools": ["shell"]}),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"})
                response = connection.getresponse()
                self.assertEqual(400, response.status)
                response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_task_control_rejects_unapproved_task_type(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.control_token = "test-token"
        server.action_lock = threading.Lock()
        server.action_times = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/api/tasks/enqueue",
                body=json.dumps({"task_type": "run_shell", "payload": {}}),
                headers={"Content-Type": "application/json", "Authorization": "Bearer test-token"})
            response = connection.getresponse()
            self.assertEqual(400, response.status)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_private_gets_and_host_validation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.control_token = "test-token"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, headers=None, method="GET"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            result = response.status, response.read()
            conn.close()
            return result
        auth = {"Authorization": "Bearer test-token"}
        try:
            with patch("agent.control_plane.memory_snapshot", return_value=["private-marker"]) as memory, \
                 patch("agent.control_plane.task_snapshot", return_value=["private-task"]) as tasks, \
                 patch("agent.control_plane.status_snapshot", return_value={"status": "online"}) as status:
                for path in ("/api/memories", "/api/tasks", "/api/status"):
                    self.assertEqual(401, request(path)[0])
                    self.assertEqual(401, request(path, {"Authorization": "Bearer wrong"})[0])
                    self.assertEqual(403, request(path, {**auth, "Origin": "https://evil.invalid"})[0])
                    self.assertEqual(403, request(path, {**auth, "Sec-Fetch-Site": "cross-site"})[0])
                self.assertEqual(403, request("/api/memories", {**auth, "Origin": "http://["})[0])
                memory.assert_not_called()
                tasks.assert_not_called()
                status.assert_not_called()
                for path in ("/", "/api/memories", "/not-found", "/api/tasks/enqueue"):
                    for host in ("evil.invalid", "127.0.0.1.evil.invalid", "localhost:1", "127.0.0.1:80@evil.invalid"):
                        self.assertEqual(403, request(path, {**auth, "Host": host}, "POST" if "enqueue" in path else "GET")[0])
                self.assertIn(b"private-marker", request("/api/memories", auth)[1])
                local = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(200, request("/api/tasks", {**auth, "Origin": local, "Sec-Fetch-Site": "same-origin"})[0])
                forwarded = "example-space-8000.app.github.dev"
                server.allowed_authorities = {forwarded}
                self.assertEqual(200, request("/api/status", {**auth, "Host": forwarded, "Origin": "https://" + forwarded})[0])
                self.assertEqual(403, request("/api/status", {**auth, "Host": "another-space-8000.app.github.dev"})[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_host_allowlist_is_exact_and_proxy_headers_are_not_trusted(self):
        self.assertEqual({"localhost:8123", "127.0.0.1:8123"}, allowed_authorities(8123, {}))
        hosts = allowed_authorities(8000, {"CODESPACE_NAME": "example-space", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"})
        self.assertIn("example-space-8000.app.github.dev", hosts)
        self.assertNotIn("other-space-8000.app.github.dev", hosts)
        self.assertEqual(2, len(allowed_authorities(8000, {"CODESPACE_NAME": "evil/a", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"})))

    def test_dashboard_reads_require_explicit_token_and_do_not_persist_it(self):
        page = _page()
        self.assertEqual(1, page.count('id="control-token"'))
        self.assertIn("Authorization:'Bearer '+token", page)
        self.assertIn("if(!response.ok)", page)
        self.assertIn("addEventListener('input',clearPrivateData)", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("sessionStorage", page)

    def test_http_dashboard_and_json_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.control_token = "test-token"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertIn("AI Workspace Control Plane", response.read().decode())
            connection.request("GET", "/api/status", headers={"Authorization": "Bearer test-token"})
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
