"""Tests for durable task queue and bounded worker execution."""
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.memory_store import MemoryStore
from agent.task_queue import TaskQueue
from agent.task_worker import run_once


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = TaskQueue(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_enqueue_claim_and_complete(self):
        task = self.queue.enqueue("workspace_status")
        claimed = self.queue.claim_next()
        self.assertEqual(task["id"], claimed["id"])
        self.assertEqual("running", claimed["status"])
        completed = self.queue.complete(task["id"], {"ok": True})
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual({"ok": True}, completed["result"])

    def test_future_task_is_not_claimed(self):
        self.queue.enqueue(
            "workspace_status",
            run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.assertIsNone(self.queue.claim_next())

    def test_retry_then_fail_after_attempt_limit(self):
        task = self.queue.enqueue("workspace_status", max_attempts=1)
        self.queue.claim_next()
        failed = self.queue.fail(task["id"], "temporary issue")
        self.assertEqual("failed", failed["status"])
        self.assertEqual(1, failed["attempts"])

    def test_rejects_unknown_task_and_invalid_payload(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue("shell_command")
        with self.assertRaises(ValueError):
            self.queue.enqueue("workspace_status", ["not", "an", "object"])

    def test_expired_lease_is_requeued(self):
        now = datetime.now(timezone.utc)
        task = self.queue.enqueue("workspace_status", max_attempts=2, run_at=now)
        self.queue.claim_next(now=now)
        recovered = self.queue.claim_next(
            now=now + timedelta(seconds=301), lease_seconds=300
        )
        self.assertEqual(task["id"], recovered["id"])
        self.assertEqual(2, recovered["attempts"])

    def test_worker_executes_one_task(self):
        task = self.queue.enqueue("workspace_status")
        result = run_once(self.queue)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(task["id"], result["task_id"])
        self.assertIn("python", result["result"])

    def test_memory_recent_task_returns_bounded_local_memories(self):
        memory_db = Path(self.temp.name) / "memory.sqlite3"
        with patch.dict("os.environ", {"WORKSPACE_MEMORY_DB": str(memory_db)}):
            store = MemoryStore()
            store.remember("Stage 11 task queue verified")
            store.remember("Stage 12 planning started")
            task = self.queue.enqueue("memory_recent", {"limit": 1})
            result = run_once(self.queue)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(task["id"], result["task_id"])
        self.assertEqual(1, len(result["result"]["memories"]))
        self.assertEqual("Stage 12 planning started", result["result"]["memories"][0]["content"])

    def test_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            self.queue.list_tasks(0)
        with self.assertRaises(ValueError):
            self.queue.claim_next(lease_seconds=1)


if __name__ == "__main__":
    unittest.main()
