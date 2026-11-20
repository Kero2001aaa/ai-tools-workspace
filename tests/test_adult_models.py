"""Contract tests for immutable Adult Stage 3 DTOs."""
import unittest

from agent.adult.models import (
    AdultModelValidationError,
    AdultTaskRequest,
    CheckpointSnapshot,
    StepRecord,
    TaskBudget,
    TaskLease,
    TaskPage,
)

STAMP = "2026-11-21T00:00:00.000000Z"
FINGERPRINT = "hmac-sha256:0123456789abcdef0123456789abcdef"


class AdultModelTests(unittest.TestCase):
    def test_valid_text_request_and_default_budget(self):
        request = AdultTaskRequest("text.analyze", FINGERPRINT)
        self.assertEqual("ephemeral", request.retention_policy)
        self.assertEqual({}, request.requested_options)
        budget = TaskBudget()
        self.assertEqual((20, 600, 0, 0, 5), (
            budget.max_total_attempts, budget.max_wall_seconds,
            budget.max_provider_calls, budget.max_cost_microusd,
            budget.failure_checkpoint_interval,
        ))

    def test_rejects_raw_or_invalid_fingerprints_and_unknown_options(self):
        for value in ("raw user prompt content", "", "short"):
            with self.subTest(value=value), self.assertRaises(AdultModelValidationError):
                AdultTaskRequest("text.generate", value)
        with self.assertRaises(AdultModelValidationError):
            AdultTaskRequest("text.generate", FINGERPRINT, requested_options={"temperature": 0.2})
        with self.assertRaises(AdultModelValidationError):
            AdultTaskRequest("text.generate", FINGERPRINT, requested_options={"prompt": "raw"})

    def test_rejects_encrypted_policy_only_at_store_boundary_but_validates_enum(self):
        request = AdultTaskRequest("image.analyze", FINGERPRINT, retention_policy="encrypted")
        self.assertEqual("encrypted", request.retention_policy)
        with self.assertRaises(AdultModelValidationError):
            AdultTaskRequest("image.analyze", FINGERPRINT, retention_policy="persistent")

    def test_budget_rejects_bool_and_out_of_range_values(self):
        for kwargs in (
            {"max_total_attempts": True},
            {"max_total_attempts": 21},
            {"max_wall_seconds": 0},
            {"max_provider_calls": -1},
            {"max_cost_microusd": 1},
            {"failure_checkpoint_interval": 4},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(AdultModelValidationError):
                TaskBudget(**kwargs)

    def test_task_lease_cannot_be_constructed_by_callers(self):
        with self.assertRaises(TypeError):
            TaskLease("task-id", "worker", 1, STAMP, 0)
        lease = TaskLease._from_store("task-id", "worker", 1, STAMP, 0)
        self.assertEqual(1, lease.lease_generation)

    def test_step_rejects_caller_timestamp_format_and_invalid_operation(self):
        with self.assertRaises(AdultModelValidationError):
            StepRecord("step-1", "task-1", 0, "shell.exec", "pending")
        with self.assertRaises(AdultModelValidationError):
            StepRecord("step-1", "task-1", 0, "intake", "pending", started_at="yesterday")
        with self.assertRaises(AdultModelValidationError):
            StepRecord("step-1", "task-1", 0, "intake", "pending", completed_at=STAMP)

    def test_checkpoint_snapshot_is_bounded_and_serializes_only_safe_metadata(self):
        snapshot = CheckpointSnapshot(
            schema_version=1, task_id="task-1", task_type="text.analyze",
            status="queued", input_fingerprint=FINGERPRINT,
            plan=("step-1",), pending_step_ids=("step-1",),
        )
        encoded = snapshot.to_json()
        self.assertIn('"input_fingerprint"', encoded)
        self.assertNotIn("prompt", encoded)
        with self.assertRaises(AdultModelValidationError):
            CheckpointSnapshot(
                schema_version=1, task_id="task-1", task_type="text.analyze",
                status="queued", sanitized_summary="See https://example.com",
            )
        with self.assertRaises(AdultModelValidationError):
            CheckpointSnapshot(
                schema_version=1, task_id="task-1", task_type="text.analyze",
                status="queued", plan=("raw prompt text",),
            )

    def test_task_page_is_immutable_and_type_checked(self):
        with self.assertRaises(AdultModelValidationError):
            TaskPage(items=[], next_cursor=None, has_more=False)


if __name__ == "__main__":
    unittest.main()
