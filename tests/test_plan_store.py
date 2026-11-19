"""Tests for durable bounded planning and step execution."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.plan_store import PlanStore


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlanStore(Path(self.temp.name) / "plans.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_create_and_retrieve_plan(self):
        plan = self.store.create_plan("Workspace audit", [
            {"task_type": "workspace_status", "description": "Inspect runtime"},
            {"task_type": "memory_recent", "payload": {"limit": 2}},
        ])
        self.assertEqual("queued", plan["status"])
        self.assertEqual(2, len(plan["steps"]))
        self.assertEqual("pending", plan["steps"][0]["status"])
        self.assertEqual(2, plan["steps"][1]["payload"]["limit"])

    def test_rejects_unknown_task_and_unsafe_payload(self):
        with self.assertRaises(ValueError):
            self.store.create_plan("Unsafe", [{"task_type": "shell_command"}])
        with self.assertRaises(ValueError):
            self.store.create_plan("Unsafe", [{"task_type": "workspace_status", "payload": {"cmd": "id"}}])
        with self.assertRaises(ValueError):
            self.store.create_plan("Unsafe", [{"task_type": "memory_recent", "payload": {"limit": True}}])

    def test_plan_bounds(self):
        with self.assertRaises(ValueError):
            self.store.create_plan(" ", [{"task_type": "workspace_status"}])
        with self.assertRaises(ValueError):
            self.store.create_plan("Too many", [{"task_type": "workspace_status"}] * 11)
        with self.assertRaises(ValueError):
            self.store.list_plans(0)

    def test_run_one_step_at_a_time_and_complete(self):
        plan = self.store.create_plan("Two steps", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        first = self.store.run_next_step(plan["id"])
        self.assertEqual("step_succeeded", first["status"])
        self.assertEqual("queued", first["plan_status"])
        current = self.store.get_plan(plan["id"])
        self.assertEqual(["succeeded", "pending"], [s["status"] for s in current["steps"]])
        second = self.store.run_next_step(plan["id"])
        self.assertEqual("completed", second["plan_status"])
        self.assertEqual("completed", self.store.get_plan(plan["id"])["status"])

    def test_failure_pauses_and_explicit_retry_resumes(self):
        plan = self.store.create_plan("Retry", [{"task_type": "workspace_status"}])
        with patch("agent.task_worker.execute_task", side_effect=RuntimeError("temporary failure")):
            failed = self.store.run_next_step(plan["id"])
        self.assertEqual("paused", failed["status"])
        step = self.store.get_plan(plan["id"])["steps"][0]
        self.assertEqual("failed", step["status"])
        self.store.retry_failed_step(plan["id"], step["id"])
        self.assertEqual("pending", self.store.get_plan(plan["id"])["steps"][0]["status"])
        result = self.store.run_next_step(plan["id"])
        self.assertEqual("completed", result["plan_status"])

    def test_plan_persists_across_store_instances(self):
        plan = self.store.create_plan("Persistent", [{"task_type": "workspace_status"}])
        reopened = PlanStore(Path(self.temp.name) / "plans.sqlite3")
        self.assertEqual(plan["id"], reopened.get_plan(plan["id"])["id"])

    def test_rejects_retry_for_non_failed_step(self):
        plan = self.store.create_plan("No retry", [{"task_type": "workspace_status"}])
        with self.assertRaises(ValueError):
            self.store.retry_failed_step(plan["id"], plan["steps"][0]["id"])


if __name__ == "__main__":
    unittest.main()
