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
        self.assertEqual("checkpointed", first["plan_status"])
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


    def test_plan_cannot_skip_failed_step_without_explicit_retry(self):
        plan = self.store.create_plan("Ordered failure", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        with patch("agent.task_worker.execute_task", side_effect=RuntimeError("failure")):
            failed = self.store.run_next_step(plan["id"])
        self.assertEqual("paused", failed["status"])
        blocked = self.store.run_next_step(plan["id"])
        self.assertEqual("paused", blocked["status"])
        self.assertIn("explicitly retried", blocked["reason"])
        current = self.store.get_plan(plan["id"])
        self.assertEqual(["failed", "pending"], [s["status"] for s in current["steps"]])


    def test_plan_does_not_start_another_step_while_one_is_running(self):
        plan = self.store.create_plan("Serialized", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        first_step = plan["steps"][0]
        with self.store._connect() as db:
            db.execute(
                "UPDATE plan_steps SET status='running',started_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", first_step["id"]),
            )
        blocked = self.store.run_next_step(plan["id"])
        self.assertEqual("running", blocked["status"])
        self.assertEqual("Another step is already running", blocked["reason"])
        current = self.store.get_plan(plan["id"])
        self.assertEqual(["running", "pending"], [s["status"] for s in current["steps"]])


    def test_plan_does_not_start_another_step_while_one_is_running(self):
        plan = self.store.create_plan("Serialized", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        first_step = plan["steps"][0]
        with self.store._connect() as db:
            db.execute(
                "UPDATE plan_steps SET status='running',started_at=? WHERE id=?",
                ("2099-01-01T00:00:00+00:00", first_step["id"]),
            )
        blocked = self.store.run_next_step(plan["id"])
        self.assertEqual("running", blocked["status"])
        self.assertEqual("Another step is already running", blocked["reason"])
        current = self.store.get_plan(plan["id"])
        self.assertEqual(["running", "pending"], [s["status"] for s in current["steps"]])

    def test_plan_persists_across_store_instances(self):
        plan = self.store.create_plan("Persistent", [{"task_type": "workspace_status"}])
        reopened = PlanStore(Path(self.temp.name) / "plans.sqlite3")
        self.assertEqual(plan["id"], reopened.get_plan(plan["id"])["id"])

    def test_run_until_done_completes_within_step_budget(self):
        plan = self.store.create_plan("Autonomous batch", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        result = self.store.run_until_done(plan["id"], max_steps=3, max_seconds=30)
        self.assertEqual("completed", result["status"])
        self.assertEqual(3, len(result["steps_executed"]))
        self.assertEqual(0, result["remaining_steps"])
        self.assertEqual("completed", self.store.get_plan(plan["id"])["status"])

    def test_run_until_done_checkpoints_and_resumes(self):
        plan = self.store.create_plan("Resumable batch", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        first = self.store.run_until_done(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("checkpointed", first["status"])
        self.assertEqual(1, first["remaining_steps"])
        reopened = PlanStore(self.store.db_path)
        second = reopened.run_until_done(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("completed", second["status"])
        self.assertEqual(1, len(second["steps_executed"]))
        self.assertEqual("completed", reopened.get_plan(plan["id"])["status"])

    def test_run_until_done_stops_on_failure(self):
        plan = self.store.create_plan("Stop on failure", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        with patch("agent.task_worker.execute_task", side_effect=RuntimeError("failure")):
            result = self.store.run_until_done(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("paused", result["status"])
        self.assertEqual(0, len(result["steps_executed"]))
        self.assertEqual(["failed", "pending"], [
            step["status"] for step in self.store.get_plan(plan["id"])["steps"]
        ])

    def test_run_until_done_rejects_unbounded_budgets(self):
        plan = self.store.create_plan("Budget validation", [{"task_type": "workspace_status"}])
        for kwargs in ({"max_steps": 0}, {"max_steps": 11}, {"max_seconds": 0}, {"max_seconds": 301}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.run_until_done(plan["id"], **kwargs)

    def test_supervisor_checkpoints_and_resumes_with_one_decision_per_call(self):
        plan = self.store.create_plan("Supervised", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        first = self.store.supervise(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("resume_later", first["decision"])
        self.assertEqual("checkpointed", first["status"])
        self.assertEqual("checkpointed", self.store.get_plan(plan["id"])["status"])
        second = self.store.supervise(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("stop", second["decision"])
        self.assertEqual("completed", second["status"])
        third = self.store.supervise(plan["id"])
        self.assertEqual("stop", third["decision"])
        self.assertIsNone(third["run"])

    def test_supervisor_stops_on_failure_without_automatic_retry(self):
        plan = self.store.create_plan("Supervisor failure", [
            {"task_type": "workspace_status"},
            {"task_type": "workspace_status"},
        ])
        with patch("agent.task_worker.execute_task", side_effect=RuntimeError("failure")):
            result = self.store.supervise(plan["id"], max_steps=2, max_seconds=30)
        self.assertEqual("stop", result["decision"])
        self.assertEqual("paused", result["status"])
        self.assertEqual("paused", self.store.get_plan(plan["id"])["status"])
    def test_rejects_retry_for_non_failed_step(self):
        plan = self.store.create_plan("No retry", [{"task_type": "workspace_status"}])
        with self.assertRaises(ValueError):
            self.store.retry_failed_step(plan["id"], plan["steps"][0]["id"])


if __name__ == "__main__":
    unittest.main()
