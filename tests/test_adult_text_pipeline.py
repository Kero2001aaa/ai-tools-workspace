from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.adult_text_input import encode_text_input
from agent.adult.adult_text_pipeline import AdultTextPipeline, TextPipelineError
from agent.adult.integrations.protocols import TextAdapterResult
from agent.adult.models import (
    AdultTaskRequest, AdultTaskSnapshot, PolicyDecision, RouteCandidate, StepRecord,
)
from agent.adult.payload_store import AdultArtifactStore, ArtifactRef


TASK_ID = "task-123456789012"
STEP_ID = "step-123456789012"
PAYLOAD_ID = "a" * 32
ATTEMPT_ID = "b" * 32
INPUT_FINGERPRINT = "input_fingerprint_123"
CONFIG_FINGERPRINT = "config_fingerprint_123"
NOW = "2026-11-21T10:00:00.000000Z"


class AllowPolicy(AdultPolicies):
    pass


class DenyInputPolicy(AdultPolicies):
    def validate_text_task(self, task_type, instruction, text):
        return PolicyDecision(False, ("policy_rejected",))


class DenyOutputPolicy(AdultPolicies):
    def validate_text_output(self, task_type, output_text):
        return PolicyDecision(False, ("policy_rejected",))


class AdultTextPipelineTests(unittest.TestCase):
    def setUp(self):
        self.task = AdultTaskSnapshot(
            task_id=TASK_ID,
            task_type="text.rewrite",
            status="running",
            created_at=NOW,
            updated_at=NOW,
            state_version=3,
            current_step_id=STEP_ID,
            attempt_count=0,
            failed_attempts_since_approval=0,
            provider_calls=0,
            approvals_granted=0,
            max_total_attempts=10,
            max_wall_seconds=600,
            max_provider_calls=1,
            max_cost_microusd=0,
            spent_cost_microusd=0,
            deadline_at="2026-11-21T11:00:00.000000Z",
        )
        self.task_request = AdultTaskRequest(
            task_type="text.rewrite",
            input_fingerprint=INPUT_FINGERPRINT,
            payload_id=PAYLOAD_ID,
            retention_policy="ephemeral",
        )
        self.step = StepRecord(
            step_id=STEP_ID,
            task_id=TASK_ID,
            sequence_no=1,
            operation="text.rewrite",
            status="pending",
            input_payload_id=PAYLOAD_ID,
            config_fingerprint=CONFIG_FINGERPRINT,
        )
        self.route = RouteCandidate(
            adapter_id="omniroute_text",
            adapter_version="1.0.0",
            provider_id="provider_x",
            model_id="model_y",
            model_version="v1",
            tool_id=None,
            tool_version=None,
            task_type="text.rewrite",
            cost_class="free_verified",
            capability_version="c" * 64,
            verified_at=NOW,
        )
        self.artifact_store = object.__new__(AdultArtifactStore)
        self.task_store = object.__new__(AdultTaskStore)
        self.router = object.__new__(AdultModelRouter)
        self.tracker = object.__new__(AdultAttemptTracker)
        self.task_store.get_task_request = Mock(return_value=self.task_request)
        self.task_store.get_step = Mock(return_value=self.step)
        self.router.choose_routes = Mock(return_value=[self.route])
        self.tracker.begin = Mock(return_value=SimpleNamespace(attempt_id=ATTEMPT_ID))
        self.tracker.succeed = Mock(return_value=SimpleNamespace(status="succeeded"))
        self.tracker.fail = Mock(return_value=SimpleNamespace(status="failed"))
        self.artifact_store.load_input = Mock(
            return_value=encode_text_input("text.rewrite", "Original source text")
        )
        self.artifact = ArtifactRef(
            artifact_id="c" * 32,
            task_id=TASK_ID,
            mime_type="text/plain",
            size_bytes=15,
            persisted=False,
            expires_at=None,
        )
        self.artifact_store.save_output = Mock(return_value=self.artifact)
        self.artifact_store.discard_output = Mock(return_value=True)
        self.adapter = Mock()
        self.adapter.invoke.return_value = TextAdapterResult(
            "Rewritten source", {"prompt_tokens": 4, "completion_tokens": 3}, 0,
        )
        self.pipeline = self.make_pipeline()

    def make_pipeline(self, policies=None, adapters=None):
        policy = policies or AllowPolicy()
        return AdultTextPipeline(
            self.artifact_store,
            self.task_store,
            self.router,
            self.tracker,
            policy,
            AdultEvaluator(policy),
            adapters if adapters is not None else {
                ("omniroute_text", "1.0.0"): self.adapter,
            },
        )

    def run_pipeline(self):
        return self.pipeline.run_one_attempt(
            self.task, STEP_ID, self.task_request, CONFIG_FINGERPRINT, set(),
        )

    def test_success_invokes_one_adapter_and_publishes_only_artifact_reference(self):
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.artifact, self.artifact)
        self.assertEqual(outcome.attempt.status, "succeeded")
        self.adapter.invoke.assert_called_once()
        self.tracker.begin.assert_called_once()
        self.tracker.succeed.assert_called_once_with(
            self.tracker.begin.return_value, self.artifact.artifact_id,
            {"input_tokens": 4, "output_tokens": 3}, 0,
        )
        self.artifact_store.save_output.assert_called_once()
        self.assertNotIn("Rewritten source", repr(outcome))

    def test_each_supported_text_operation_uses_the_same_bounded_pipeline_contract(self):
        for task_type in ("text.analyze", "text.generate", "text.rewrite"):
            with self.subTest(task_type=task_type):
                self.task = replace(self.task, task_type=task_type)
                self.task_request = replace(self.task_request, task_type=task_type)
                self.step = replace(self.step, operation=task_type)
                self.route = replace(self.route, task_type=task_type)
                self.task_store.get_task_request.return_value = self.task_request
                self.task_store.get_step.return_value = self.step
                self.router.choose_routes.return_value = [self.route]
                self.artifact_store.load_input.return_value = encode_text_input(
                    task_type, "Source text for this operation",
                )
                self.adapter.invoke.return_value = TextAdapterResult(
                    "A valid generated text result", {}, 0,
                )
                self.pipeline = self.make_pipeline()
                outcome = self.run_pipeline()
                self.assertEqual("succeeded", outcome.status)
                self.adapter.invoke.assert_called_once()
                self.adapter.invoke.reset_mock()
                self.tracker.begin.reset_mock()
                self.tracker.succeed.reset_mock()

    def test_no_eligible_route_creates_no_attempt_or_artifact(self):
        self.router.choose_routes.return_value = []
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.error_code, "CAPABILITY_UNAVAILABLE")
        self.tracker.begin.assert_not_called()
        self.adapter.invoke.assert_not_called()
        self.artifact_store.save_output.assert_not_called()

    def test_input_policy_rejection_precedes_routing_and_attempt(self):
        self.pipeline = self.make_pipeline(DenyInputPolicy())
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.error_code, "POLICY_REJECTED")
        self.router.choose_routes.assert_not_called()
        self.tracker.begin.assert_not_called()

    def test_missing_exact_adapter_does_not_fall_back_or_create_attempt(self):
        self.pipeline = self.make_pipeline(adapters={})
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.error_code, "ADAPTER_UNAVAILABLE")
        self.tracker.begin.assert_not_called()
        self.adapter.invoke.assert_not_called()

    def test_invalid_model_output_is_retryable_and_never_saved(self):
        self.adapter.invoke.return_value = TextAdapterResult("   ", {}, 0)
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "MODEL_OUTPUT_INVALID")
        self.tracker.fail.assert_called_once()
        self.assertEqual(self.tracker.fail.call_args.args[1], "MODEL_OUTPUT_INVALID")
        self.assertTrue(self.tracker.fail.call_args.args[3])
        self.artifact_store.save_output.assert_not_called()

    def test_output_policy_rejection_is_terminal_and_never_saved(self):
        self.pipeline = self.make_pipeline(DenyOutputPolicy())
        outcome = self.run_pipeline()
        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.error_code, "POLICY_REJECTED")
        self.tracker.fail.assert_called_once()
        self.assertEqual(self.tracker.fail.call_args.args[1], "POLICY_REJECTED")
        self.assertFalse(self.tracker.fail.call_args.args[3])
        self.artifact_store.save_output.assert_not_called()

    def test_adapter_exception_is_sanitized_and_attempt_is_failed(self):
        self.adapter.invoke.side_effect = RuntimeError("private provider response body")
        outcome = self.run_pipeline()
        self.assertEqual(outcome.error_code, "PROVIDER_ERROR")
        self.assertEqual(self.tracker.fail.call_args.args[1], "PROVIDER_ERROR")
        self.assertNotIn("private provider response body", repr(outcome))

    def test_output_storage_failure_is_recorded_without_artifact(self):
        self.artifact_store.save_output.side_effect = OSError("private filesystem path")
        outcome = self.run_pipeline()
        self.assertEqual(outcome.error_code, "OUTPUT_STORAGE_FAILED")
        self.assertEqual(self.tracker.fail.call_args.args[1], "OUTPUT_STORAGE_FAILED")
        self.assertIsNone(outcome.artifact)

    def test_finalization_failure_discards_uncommitted_artifact(self):
        self.tracker.succeed.side_effect = RuntimeError("finalization failed")
        with self.assertRaisesRegex(RuntimeError, "finalization failed"):
            self.run_pipeline()
        self.artifact_store.discard_output.assert_called_once_with(
            self.artifact.artifact_id, TASK_ID, ATTEMPT_ID,
        )

    def test_step_operation_mismatch_is_rejected_before_payload_read(self):
        self.task_store.get_step.return_value = StepRecord(
            step_id=STEP_ID,
            task_id=TASK_ID,
            sequence_no=1,
            operation="text.analyze",
            status="pending",
            input_payload_id=PAYLOAD_ID,
            config_fingerprint=CONFIG_FINGERPRINT,
        )
        with self.assertRaises(TextPipelineError):
            self.run_pipeline()
        self.artifact_store.load_input.assert_not_called()
        self.tracker.begin.assert_not_called()

    def test_persisted_request_mismatch_is_rejected(self):
        self.task_store.get_task_request.return_value = AdultTaskRequest(
            task_type="text.rewrite",
            input_fingerprint="different_fingerprint_123",
            payload_id=PAYLOAD_ID,
        )
        with self.assertRaises(TextPipelineError):
            self.run_pipeline()
        self.router.choose_routes.assert_not_called()
        self.tracker.begin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
