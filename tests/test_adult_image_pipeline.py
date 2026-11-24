from __future__ import annotations

import struct
import unittest
import zlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_image_evaluator import AdultImageEvaluator
from agent.adult.adult_image_input import encode_image_input, encode_image_prompt
from agent.adult.adult_image_pipeline import (
    AdultImagePipeline, ImagePipelineError, ImagePipelineOutcome,
)
from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.integrations.protocols import ImageAdapterResult, ImageAnalysisResult
from agent.adult.models import (
    AdultTaskRequest, AdultTaskSnapshot, ImageInputEnvelope, RouteCandidate, StepRecord,
)
from agent.adult.payload_store import AdultArtifactStore, ArtifactRef

TASK_ID = "task-123456789012"
STEP_ID = "step-123456789012"
PAYLOAD_ID = "a" * 32
IMAGE_ID = "d" * 32
PROMPT_ID = "e" * 32
ATTEMPT_ID = "b" * 32
INPUT_FP = "input_fingerprint_123"
CONFIG_FP = "config_fingerprint_123"
NOW = "2026-11-21T10:00:00.000000Z"


def png_bytes(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class AdultImagePipelineTests(unittest.TestCase):
    def build(self, task_type: str, **route_overrides):
        self.task_type = task_type
        self.task = AdultTaskSnapshot(
            task_id=TASK_ID, task_type=task_type, status="running", created_at=NOW,
            updated_at=NOW, state_version=3, current_step_id=STEP_ID, attempt_count=0,
            failed_attempts_since_approval=0, provider_calls=0, approvals_granted=0,
            max_total_attempts=10, max_wall_seconds=600, max_provider_calls=1,
            max_cost_microusd=0, spent_cost_microusd=0,
            deadline_at="2026-11-21T11:00:00.000000Z",
        )
        self.request = AdultTaskRequest(
            task_type=task_type, input_fingerprint=INPUT_FP, payload_id=PAYLOAD_ID,
            retention_policy="ephemeral",
        )
        step = StepRecord(
            step_id=STEP_ID, task_id=TASK_ID, sequence_no=1, operation=task_type,
            status="pending", input_payload_id=PAYLOAD_ID, config_fingerprint=CONFIG_FP,
        )
        fields = dict(
            adapter_id="img_adapter", adapter_version="1.0.0", provider_id="p", model_id="m",
            model_version="v1", tool_id=None, tool_version=None, task_type=task_type,
            cost_class="free_verified", capability_version="c" * 64, verified_at=NOW,
            output_mime_types=("image/png",), max_output_bytes=1024 * 1024,
            max_output_width=512, max_output_height=512,
        )
        fields.update(route_overrides)
        self.route = RouteCandidate(**fields)
        self.store = object.__new__(AdultArtifactStore)
        self.task_store = object.__new__(AdultTaskStore)
        self.router = object.__new__(AdultModelRouter)
        self.tracker = object.__new__(AdultAttemptTracker)
        self.task_store.get_task_request = Mock(return_value=self.request)
        self.task_store.get_step = Mock(return_value=step)
        self.router.choose_routes = Mock(return_value=[self.route])
        self.tracker.begin = Mock(return_value=SimpleNamespace(attempt_id=ATTEMPT_ID))
        self.tracker.succeed = Mock(return_value=SimpleNamespace(status="succeeded"))
        self.tracker.fail = Mock(return_value=SimpleNamespace(status="failed"))
        envelope = ImageInputEnvelope(
            task_type=task_type,
            image_payload_id=None if task_type == "image.generate" else IMAGE_ID,
            prompt_payload_id=None if task_type == "image.analyze" else PROMPT_ID,
        )
        self.input_image = png_bytes()
        blobs = {
            PAYLOAD_ID: encode_image_input(envelope),
            IMAGE_ID: self.input_image,
            PROMPT_ID: encode_image_prompt("a red square"),
        }
        self.store.load_input = Mock(side_effect=lambda pid, tid: blobs[pid])
        self.artifact = ArtifactRef(
            artifact_id="c" * 32, task_id=TASK_ID, mime_type="image/png",
            size_bytes=10, persisted=False, expires_at=None,
        )
        self.store.save_output = Mock(return_value=self.artifact)
        self.store.discard_output = Mock(return_value=True)
        self.adapter = Mock()
        self.output = png_bytes(4, 4)
        self.adapter.generate.return_value = ImageAdapterResult(self.output, "image/png", {}, 0)
        self.adapter.edit.return_value = ImageAdapterResult(self.output, "image/png", {}, 0)
        self.adapter.analyze.return_value = ImageAnalysisResult("A small image.", {}, 0)
        policies = AdultImagePolicies()
        self.pipeline = AdultImagePipeline(
            self.store, self.task_store, self.router, self.tracker, policies,
            AdultImageEvaluator(policies), {("img_adapter", "1.0.0"): self.adapter},
        )

    def run_once(self):
        return self.pipeline.run_one_attempt(self.task, STEP_ID, self.request, CONFIG_FP, set())

    def test_generate_succeeds_and_persists_after_evaluation(self):
        self.build("image.generate")
        outcome = self.run_once()
        self.assertEqual("succeeded", outcome.status)
        self.assertIs(self.artifact, outcome.artifact)
        self.adapter.generate.assert_called_once()
        self.adapter.edit.assert_not_called()
        self.adapter.analyze.assert_not_called()
        self.store.save_output.assert_called_once_with(TASK_ID, ATTEMPT_ID, self.output, "image/png")
        query = self.router.choose_routes.call_args.args[0]
        self.assertEqual(("text",), query.input_modalities)
        self.assertIsNone(query.mime_type)

    def test_edit_passes_image_metadata_and_dispatches_edit(self):
        self.build("image.edit")
        outcome = self.run_once()
        self.assertEqual("succeeded", outcome.status)
        self.adapter.edit.assert_called_once()
        request = self.adapter.edit.call_args.args[0]
        self.assertEqual(IMAGE_ID, request.image_payload_id)
        self.assertEqual(PROMPT_ID, request.prompt_payload_id)
        self.assertEqual("image/png", request.mime_type)
        query = self.router.choose_routes.call_args.args[0]
        self.assertEqual("image/png", query.mime_type)
        self.assertEqual((2, 2), (query.width, query.height))

    def test_analyze_persists_text_without_output_image_constraints(self):
        self.build(
            "image.analyze", output_mime_types=None, max_output_bytes=None,
            max_output_width=None, max_output_height=None,
        )
        outcome = self.run_once()
        self.assertEqual("succeeded", outcome.status)
        self.adapter.analyze.assert_called_once()
        self.store.save_output.assert_called_once_with(
            TASK_ID, ATTEMPT_ID, "A small image.".encode("utf-8"), "text/plain",
        )

    def test_missing_route_output_constraints_fail_closed_without_persisting(self):
        self.build("image.generate", max_output_width=None)
        outcome = self.run_once()
        self.assertEqual("failed", outcome.status)
        self.assertEqual("MODEL_OUTPUT_INVALID", outcome.error_code)
        self.store.save_output.assert_not_called()
        self.tracker.succeed.assert_not_called()

    def test_oversized_dimensions_are_rejected_before_persisting(self):
        self.build("image.generate", max_output_width=2, max_output_height=2)
        outcome = self.run_once()
        self.assertEqual("failed", outcome.status)
        self.assertIsNone(outcome.artifact)
        self.store.save_output.assert_not_called()

    def test_mime_not_in_route_allowlist_is_rejected(self):
        self.build("image.generate", output_mime_types=("image/webp",))
        outcome = self.run_once()
        self.assertNotEqual("succeeded", outcome.status)
        self.store.save_output.assert_not_called()

    def test_invalid_input_image_never_reaches_router_or_adapter(self):
        self.build("image.edit")
        good = self.store.load_input.side_effect
        self.store.load_input.side_effect = (
            lambda pid, tid: b"not an image" if pid == IMAGE_ID else good(pid, tid)
        )
        outcome = self.run_once()
        self.assertEqual("rejected", outcome.status)
        self.router.choose_routes.assert_not_called()
        self.adapter.edit.assert_not_called()

    def test_no_eligible_route_is_unavailable_without_attempt(self):
        self.build("image.generate")
        self.router.choose_routes.return_value = []
        outcome = self.run_once()
        self.assertEqual("unavailable", outcome.status)
        self.tracker.begin.assert_not_called()

    def test_paid_route_is_not_attempted(self):
        self.build("image.generate", cost_class="paid")
        outcome = self.run_once()
        self.assertEqual("unavailable", outcome.status)
        self.adapter.generate.assert_not_called()

    def test_missing_adapter_is_unavailable(self):
        self.build("image.generate")
        self.pipeline = AdultImagePipeline(
            self.store, self.task_store, self.router, self.tracker, AdultImagePolicies(),
            AdultImageEvaluator(AdultImagePolicies()), {},
        )
        outcome = self.run_once()
        self.assertEqual("ADAPTER_UNAVAILABLE", outcome.error_code)
        self.tracker.begin.assert_not_called()

    def test_adapter_timeout_fails_attempt_without_artifact(self):
        self.build("image.generate")
        self.adapter.generate.side_effect = TimeoutError()
        outcome = self.run_once()
        self.assertEqual("PROVIDER_TIMEOUT", outcome.error_code)
        self.store.save_output.assert_not_called()
        self.adapter.generate.assert_called_once()

    def test_nonzero_cost_is_blocked_before_persisting(self):
        self.build("image.generate")
        self.adapter.generate.return_value = ImageAdapterResult(self.output, "image/png", {}, 5)
        outcome = self.run_once()
        self.assertEqual("COST_POLICY_BLOCKED", outcome.error_code)
        self.store.save_output.assert_not_called()

    def test_tracker_failure_after_save_discards_artifact(self):
        self.build("image.generate")
        self.tracker.succeed.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.store.discard_output.assert_called_once_with("c" * 32, TASK_ID, ATTEMPT_ID)

    def test_outcome_does_not_expose_image_bytes(self):
        self.build("image.generate")
        outcome = self.run_once()
        self.assertNotIn("PNG", repr(outcome))

    def test_wrong_task_type_is_invalid_request(self):
        self.build("image.generate")
        bad = replace(self.request, task_type="text.rewrite") if False else None
        task = replace(self.task, task_type="text.rewrite")
        with self.assertRaises(ImagePipelineError):
            self.pipeline.run_one_attempt(task, STEP_ID, self.request, CONFIG_FP, set())

    def test_non_success_outcome_cannot_carry_artifact(self):
        self.build("image.generate")
        with self.assertRaises(Exception):
            ImagePipelineOutcome(TASK_ID, STEP_ID, "failed", "X", artifact=self.artifact)


if __name__ == "__main__":
    unittest.main()
