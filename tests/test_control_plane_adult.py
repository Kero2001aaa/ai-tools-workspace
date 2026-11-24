"""Read-only Adult status routes in the local control plane."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import AdultTaskRequest, TaskBudget
from agent.control_plane import Handler

TOKEN = "adult-test-token"
FINGERPRINT = "hmac-sha256:0123456789abcdef"


class AdultStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "adult.sqlite3"
        self.env = patch.dict(os.environ, {"ADULT_STATE_DB": str(self.db)})
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.control_token = TOKEN
        self.server.action_lock = threading.Lock()
        self.server.action_times = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.env.stop()
        self.temp.cleanup()

    def get(self, path, token=TOKEN, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": "Bearer " + token} if token else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_unauthorized_denied(self):
        self.assertEqual(401, self.get("/api/adult/tasks", token=None)[0])
        self.assertEqual(401, self.get("/api/adult/tasks", token="wrong")[0])

    def test_missing_store_is_not_created(self):
        status, body = self.get("/api/adult/tasks")
        self.assertEqual(200, status)
        self.assertEqual({"available": False, "tasks": []}, json.loads(body))
        self.assertFalse(self.db.exists())

    def test_real_lifecycle_visible_without_private_fields(self):
        store = AdultTaskStore(self.db)
        task = store.create_task(AdultTaskRequest("text.analyze", FINGERPRINT), TaskBudget())
        status, body = self.get("/api/adult/tasks")
        data = json.loads(body)
        self.assertEqual(200, status)
        self.assertEqual(1, len(data["tasks"]))
        row = data["tasks"][0]
        self.assertEqual(task.task_id, row["task_id"])
        self.assertEqual(task.status, row["status"])
        self.assertEqual("task_created", row["latest_event"]["event_type"])
        self.assertEqual("initial", row["latest_checkpoint"]["reason"])
        store.claim_task(task.task_id, "status-worker", lease_seconds=60)
        detail = json.loads(self.get("/api/adult/tasks/" + task.task_id)[1])
        self.assertIn("task_claimed", [e["event_type"] for e in detail["events"]])
        raw = json.dumps(data) + json.dumps(detail)
        for forbidden in (FINGERPRINT, "fingerprint", "payload", "metadata", "status-worker",
                          str(self.temp.name), "lease"):
            self.assertNotIn(forbidden, raw)

    def test_filters_and_bad_input(self):
        store = AdultTaskStore(self.db)
        task = store.create_task(AdultTaskRequest("text.analyze", FINGERPRINT), TaskBudget())
        self.assertEqual(400, self.get("/api/adult/tasks?status=bogus")[0])
        self.assertEqual(400, self.get("/api/adult/tasks?limit=0")[0])
        self.assertEqual(400, self.get("/api/adult/tasks?limit=x")[0])
        self.assertEqual(400, self.get("/api/adult/tasks?extra=1")[0])
        self.assertEqual(400, self.get("/api/adult/tasks/..%2Fetc")[0])
        self.assertEqual(404, self.get("/api/adult/tasks/does-not-exist")[0])
        ok = json.loads(self.get("/api/adult/tasks?status=" + task.status)[1])
        self.assertEqual(1, len(ok["tasks"]))

    def test_post_adult_routes_not_supported_and_old_routes_kept(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("POST", "/api/adult/tasks/x/cancel", body="{}",
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
        self.assertEqual(405, conn.getresponse().status)
        conn.close()
        self.assertEqual(200, self.get("/api/status")[0])

    def test_status_open_is_genuinely_read_only(self):
        import sqlite3
        store = AdultTaskStore(self.db)
        store.create_task(AdultTaskRequest("text.analyze", FINGERPRINT), TaskBudget())
        before = self.db.read_bytes()
        modified = self.db.stat().st_mtime_ns
        with patch.object(AdultTaskStore, "_migrate", side_effect=AssertionError("migration forbidden")):
            self.assertEqual(200, self.get("/api/adult/tasks")[0])
            readonly = AdultTaskStore(self.db, read_only=True)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(modified, self.db.stat().st_mtime_ns)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.create_task(AdultTaskRequest("text.analyze", FINGERPRINT), TaskBudget())
        self.assertEqual(before, self.db.read_bytes())

    def test_unknown_reason_is_hidden_and_newest_event_is_real(self):
        store = AdultTaskStore(self.db)
        task = store.create_task(AdultTaskRequest("text.analyze", FINGERPRINT), TaskBudget())
        lease = store.claim_task(task.task_id, "status-worker", lease_seconds=60)
        for _ in range(205):
            task = store.get_task(task.task_id)
            next_status = "evaluating" if task.status == "running" else "running"
            store.transition(task.task_id, {task.status}, next_status, "state_transition",
                             task.state_version, {"reason_code":"private_project_name"}, lease)
        row = json.loads(self.get("/api/adult/tasks")[1])["tasks"][0]
        self.assertEqual("state_transition", row["latest_event"]["event_type"])
        self.assertIsNone(row["latest_event"]["reason_code"])
        detail = json.loads(self.get("/api/adult/tasks/" + task.task_id)[1])
        self.assertEqual(50, len(detail["events"]))
        self.assertTrue(all(e["event_type"] == "state_transition" for e in detail["events"]))
        self.assertNotIn("private_project_name", json.dumps(detail))

    def test_readonly_missing_parent_not_created_and_old_schema_rejected(self):
        import sqlite3
        missing = Path(self.temp.name) / "absent" / "adult.sqlite3"
        with self.assertRaises(Exception): AdultTaskStore(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE adult_schema_migrations(version INTEGER,name TEXT)")
            db.execute("INSERT INTO adult_schema_migrations VALUES(1,'adult_operational_store_v1')")
        before = self.db.read_bytes()
        self.assertEqual(503, self.get("/api/adult/tasks")[0])
        self.assertEqual(before, self.db.read_bytes())


if __name__ == "__main__":
    unittest.main()
