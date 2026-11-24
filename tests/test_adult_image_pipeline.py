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
        with self.assertRaises(ImagePipelineError) as raised:
            self.run_once()
        self.assertEqual("finalization_failed", raised.exception.error_code)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.store.discard_output.assert_called_once_with("c" * 32, TASK_ID, ATTEMPT_ID)

    def test_outcome_does_not_expose_image_bytes(self):
        self.build("image.generate")
        outcome = self.run_once()
        self.assertNotIn("PNG", repr(outcome))

    def test_wrong_task_type_is_invalid_request(self):
        self.build("image.generate")
        task = replace(self.task, task_type="text.rewrite")
        with self.assertRaises(ImagePipelineError):
            self.pipeline.run_one_attempt(task, STEP_ID, self.request, CONFIG_FP, set())

    def test_invalid_prompt_never_reaches_router_or_adapter(self):
        for task_type in ("image.generate", "image.edit"):
            with self.subTest(task_type=task_type):
                self.build(task_type)
                good = self.store.load_input.side_effect
                self.store.load_input.side_effect = lambda pid, tid: b"" if pid == PROMPT_ID else good(pid, tid)
                self.assertEqual("rejected", self.run_once().status)
                self.router.choose_routes.assert_not_called()
                self.tracker.begin.assert_not_called()

    def test_missing_input_never_creates_attempt(self):
        self.build("image.edit")
        self.store.load_input.side_effect = FileNotFoundError()
        self.assertEqual("INVALID_INPUT", self.run_once().error_code)
        self.tracker.begin.assert_not_called()

    def test_evaluator_exception_finalizes_attempt_fail_closed(self):
        self.build("image.generate")
        self.pipeline._evaluator.evaluate = Mock(side_effect=RuntimeError())
        self.assertEqual("MODEL_OUTPUT_INVALID", self.run_once().error_code)
        self.tracker.fail.assert_called_once()
        self.store.save_output.assert_not_called()

    def test_invalid_evaluator_result_finalizes_attempt_fail_closed(self):
        self.build("image.generate")
        self.pipeline._evaluator.evaluate = Mock(return_value=None)
        self.assertEqual("MODEL_OUTPUT_INVALID", self.run_once().error_code)
        self.tracker.fail.assert_called_once()
        self.store.save_output.assert_not_called()

    def test_missing_route_policy_is_not_retryable(self):
        self.build("image.generate", max_output_width=None)
        self.run_once()
        self.assertFalse(self.tracker.fail.call_args.args[3])

    def test_provider_timeout_error_code_maps_to_timeout(self):
        self.build("image.generate")
        error = RuntimeError()
        error.error_code = "provider_timeout"
        self.adapter.generate.side_effect = error
        self.assertEqual("PROVIDER_TIMEOUT", self.run_once().error_code)
        self.adapter.generate.assert_called_once()

    def test_storage_failure_does_not_succeed_attempt(self):
        self.build("image.generate")
        self.store.save_output.side_effect = RuntimeError()
        self.assertEqual("OUTPUT_STORAGE_FAILED", self.run_once().error_code)
        self.tracker.succeed.assert_not_called()

    def test_tracker_budget_refusal_prevents_adapter_call(self):
        # Budget enforcement is owned by AttemptTracker; no adapter call on refusal.
        self.build("image.generate")
        from agent.adult.adult_attempt_tracker import AttemptBudgetExhausted
        self.tracker.begin.side_effect = AttemptBudgetExhausted()
        self.assertEqual("ATTEMPT_UNAVAILABLE", self.run_once().error_code)
        self.adapter.generate.assert_not_called()

    def test_task_owned_subpayload_failures_never_route(self):
        import tempfile
        from agent.adult.payload_store import TransientPayloadCache
        for target, operation in ((IMAGE_ID, "image.edit"), (PROMPT_ID, "image.generate")):
            for failure in ("cross_task", "wrong_kind", "expired", "missing"):
                with self.subTest(target=target, failure=failure), tempfile.TemporaryDirectory() as folder:
                    self.build(operation)
                    clock = [0.0]
                    cache = TransientPayloadCache(ttl_seconds=10, clock=lambda: clock[0])
                    store = AdultArtifactStore(folder, cache=cache)
                    if failure != "missing":
                        cache._put_reference(
                            "other-task-123456" if failure == "cross_task" else TASK_ID,
                            target, png_bytes() if target == IMAGE_ID else encode_image_prompt("a square"),
                            kind="output" if failure == "wrong_kind" else "input",
                        )
                    if failure == "expired":
                        clock[0] = 11.0
                    cache._put_reference(TASK_ID, PAYLOAD_ID, encode_image_input(ImageInputEnvelope(
                        task_type=operation, image_payload_id=IMAGE_ID if operation == "image.edit" else None,
                        prompt_payload_id=PROMPT_ID,
                    )))
                    if target == IMAGE_ID:
                        cache._put_reference(TASK_ID, PROMPT_ID, encode_image_prompt("a square"))
                    self.pipeline._artifact_store = store
                    self.assertEqual("INVALID_INPUT", self.run_once().error_code)
                    self.router.choose_routes.assert_not_called()
                    self.tracker.begin.assert_not_called()
                    self.adapter.edit.assert_not_called()
                    self.adapter.generate.assert_not_called()

    def test_input_policy_exceptions_and_malformed_decisions_fail_closed(self):
        for decision in (None, True, "allowed"):
            with self.subTest(decision=decision):
                self.build("image.edit")
                self.pipeline._image_policies.validate_image_input = Mock(return_value=decision)
                self.assertEqual("INVALID_INPUT", self.run_once().error_code)
                self.router.choose_routes.assert_not_called()
        self.build("image.edit")
        self.pipeline._image_policies.validate_image_input = Mock(side_effect=RuntimeError("private detail"))
        self.assertEqual("INVALID_INPUT", self.run_once().error_code)
        self.tracker.begin.assert_not_called()

    def test_output_policy_denial_is_terminal_and_never_persisted(self):
        from agent.adult.models import PolicyDecision
        self.build("image.generate")
        self.pipeline._image_policies.validate_image_output = Mock(
            return_value=PolicyDecision(False, ("policy_rejected",)),
        )
        outcome = self.run_once()
        self.assertEqual("rejected", outcome.status)
        self.assertEqual("POLICY_REJECTED", outcome.error_code)
        self.assertFalse(self.tracker.fail.call_args.args[3])
        self.store.save_output.assert_not_called()

    def test_adapter_exception_is_sanitized_in_outcome_and_tracker(self):
        self.build("image.generate")
        marker = "private-provider-response-content"
        self.adapter.generate.side_effect = RuntimeError(marker)
        outcome = self.run_once()
        self.assertNotIn(marker, repr(outcome))
        self.assertNotIn(marker, repr(self.tracker.fail.call_args))
        self.assertEqual("PROVIDER_ERROR", outcome.error_code)
        self.adapter.generate.assert_called_once()

    def test_real_store_and_tracker_lifecycle_for_all_operations(self):
        self._real_lifecycle()

    def test_real_cancellation_during_adapter_call_cannot_publish_success(self):
        self._real_lifecycle(cancel_during_call=True)

    def test_real_expired_lease_during_adapter_call_cannot_publish_success(self):
        self._real_lifecycle(expire_during_call=True)

    def _real_lifecycle(self, cancel_during_call=False, expire_during_call=False):
        import tempfile
        from pathlib import Path
        from agent.adult.models import TaskBudget
        for operation in ("image.analyze", "image.generate", "image.edit"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                self.build(operation)
                store = AdultTaskStore(Path(folder) / "tasks.sqlite3")
                artifacts = AdultArtifactStore(Path(folder) / "artifacts")
                task = store.create_task(self.request, TaskBudget(max_provider_calls=1))
                lease = store.claim_task(task.task_id, "test-worker", lease_seconds=300)
                task = store.get_task(task.task_id)
                step_id = "image-step-123456789"
                step = StepRecord(
                    step_id=step_id, task_id=task.task_id, sequence_no=1,
                    operation=operation, status="pending", input_payload_id=PAYLOAD_ID,
                    config_fingerprint=CONFIG_FP,
                )
                store.save_step(task.task_id, step, lease, task.state_version)
                task = store.get_task(task.task_id)
                store.save_step(task.task_id, replace(step, status="running"), lease, task.state_version)
                task = store.get_task(task.task_id)
                envelope = ImageInputEnvelope(
                    task_type=operation,
                    image_payload_id=None if operation == "image.generate" else IMAGE_ID,
                    prompt_payload_id=None if operation == "image.analyze" else PROMPT_ID,
                )
                for pid, data in (
                    (PAYLOAD_ID, encode_image_input(envelope)),
                    (IMAGE_ID, png_bytes()), (PROMPT_ID, encode_image_prompt("a red square")),
                ):
                    artifacts.cache._put_reference(task.task_id, pid, data)
                tracker = AdultAttemptTracker(store)
                policies = AdultImagePolicies()
                pipeline = AdultImagePipeline(
                    artifacts, store, self.router, tracker, policies,
                    AdultImageEvaluator(policies), {("img_adapter", "1.0.0"): self.adapter},
                )
                if cancel_during_call or expire_during_call:
                    method = {"image.analyze": "analyze", "image.generate": "generate", "image.edit": "edit"}[operation]
                    result = getattr(self.adapter, method).return_value
                    def cancel_and_return(request):
                        if cancel_during_call:
                            store.request_cancel(task.task_id)
                        else:
                            from datetime import datetime, timedelta, timezone
                            tracker._clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=10)
                        return result
                    getattr(self.adapter, method).side_effect = cancel_and_return
                    artifacts.discard_output = Mock(wraps=artifacts.discard_output)
                    with self.assertRaises(Exception):
                        pipeline.run_one_attempt(task, step_id, self.request, CONFIG_FP, set())
                    artifacts.discard_output.assert_called_once()
                    artifact_id = artifacts.discard_output.call_args.args[0]
                    from agent.adult.payload_store import PayloadNotFound
                    with self.assertRaises(PayloadNotFound):
                        artifacts.read_chunk(artifact_id, 0)
                    continue
                outcome = pipeline.run_one_attempt(task, step_id, self.request, CONFIG_FP, set())
                self.assertEqual("succeeded", outcome.status)
                self.assertEqual("succeeded", outcome.attempt.status)
                self.assertEqual(1, tracker.total_attempts(task.task_id))
                self.assertEqual(1, store.get_task(task.task_id).provider_calls)
                chunk = artifacts.read_chunk(outcome.artifact.artifact_id, 0)
                expected = b"A small image." if operation == "image.analyze" else self.output
                self.assertEqual(expected, chunk.data)
                db = store._connect()
                try:
                    metadata = "\n".join(db.iterdump())
                finally:
                    db.close()
                for private_value in ("a red square", "A small image.", IMAGE_ID, PROMPT_ID):
                    self.assertNotIn(private_value, metadata)

    def test_unsupported_format_signatures_fail_before_routing(self):
        signatures = (b"BM", b"II*\x00", b"MM\x00*", b"\x00\x00\x00\x18ftypavif", b"GIF89a", b"<svg>")
        for signature in signatures:
            with self.subTest(signature=signature):
                self.build("image.edit")
                good = self.store.load_input.side_effect
                self.store.load_input.side_effect = lambda pid, tid: signature + b"x" * 32 if pid == IMAGE_ID else good(pid, tid)
                self.assertEqual("rejected", self.run_once().status)
                self.router.choose_routes.assert_not_called()
                self.tracker.begin.assert_not_called()
                self.adapter.edit.assert_not_called()

    def test_unknown_cost_route_is_not_attempted(self):
        self.build("image.generate", cost_class="unknown")
        self.assertEqual("unavailable", self.run_once().status)
        self.tracker.begin.assert_not_called()
        self.adapter.generate.assert_not_called()

    def test_corrupt_declared_mime_and_route_byte_output_fail_before_storage(self):
        for failure in ("corrupt", "declared_mime", "route_bytes"):
            with self.subTest(failure=failure):
                self.build("image.generate")
                if failure == "corrupt":
                    self.adapter.generate.return_value = ImageAdapterResult(b"not-an-image", "image/png", {}, 0)
                elif failure == "declared_mime":
                    self.adapter.generate.return_value = ImageAdapterResult(self.output, "image/jpeg", {}, 0)
                else:
                    self.router.choose_routes.return_value = [replace(self.route, max_output_bytes=1)]
                self.assertEqual("MODEL_OUTPUT_INVALID", self.run_once().error_code)
                self.store.save_output.assert_not_called()
                self.tracker.succeed.assert_not_called()
                self.adapter.generate.assert_called_once()

    def test_finalization_and_cleanup_errors_are_sanitized(self):
        import traceback
        self.build("image.generate")
        marker = "private-analysis-secret-/tmp/private-path"
        self.tracker.succeed.side_effect = RuntimeError(marker)
        self.store.discard_output.side_effect = RuntimeError(marker)
        with self.assertRaises(ImagePipelineError) as raised:
            self.run_once()
        self.assertEqual("finalization_failed", raised.exception.error_code)
        self.assertNotIn(marker, "".join(traceback.format_exception(raised.exception)))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.store.discard_output.assert_called_once_with("c" * 32, TASK_ID, ATTEMPT_ID)

    def test_failed_attempt_finalization_error_is_sanitized(self):
        import traceback
        self.build("image.generate")
        marker = "private-provider-finalization-detail"
        self.adapter.generate.side_effect = RuntimeError(marker)
        self.tracker.fail.side_effect = RuntimeError(marker)
        with self.assertRaises(ImagePipelineError) as raised:
            self.run_once()
        self.assertEqual("finalization_failed", raised.exception.error_code)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertNotIn(marker, "".join(traceback.format_exception(raised.exception)))
        self.store.save_output.assert_not_called()

    def test_real_router_zero_provider_budget_creates_no_attempt(self):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from agent.adult.capability_registry import ModelCapabilityRegistry
        from agent.adult.models import CapabilityEvidence, ModelCapabilitySpec
        self.build("image.analyze")
        self.task = replace(self.task, max_provider_calls=0)
        with tempfile.TemporaryDirectory() as folder:
            store = AdultTaskStore(Path(folder) / "registry.sqlite3")
            registry = ModelCapabilityRegistry(store, clock=lambda: datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
            spec = ModelCapabilitySpec(
                adapter_id="img_adapter", adapter_version="1.0.0", provider_id="p", model_id="m",
                model_version="v1", task_type="image.analyze", input_modalities=("image",),
                output_modalities=("text",), cost_class="free_verified",
                accepted_mime_types=("image/png",), max_input_bytes=4096,
                max_input_width=512, max_input_height=512,
            )
            record = registry.register(spec)
            evidence = CapabilityEvidence(
                adapter_id=spec.adapter_id, adapter_version=spec.adapter_version, provider_id="p", model_id="m",
                model_version="v1", task_type=spec.task_type, input_modalities=spec.input_modalities,
                output_modalities=spec.output_modalities, capability_source="official_provider_docs",
                capability_reference="https://provider.example/docs", verified_at=NOW,
                expires_at="2026-12-21T12:00:00.000000Z", cost_source="official_free_tier_terms",
                cost_reference="https://provider.example/free", cost_verified_at=NOW,
                cost_expires_at="2026-11-28T12:00:00.000000Z", cost_is_zero=True,
            )
            record = registry.verify(record.capability_id, evidence, record.state_version)
            registry.enable(record.capability_id, record.state_version)
            router = AdultModelRouter(registry, approach_key_builder=AdultAttemptTracker.approach_keys)
            self.pipeline._router = router
            self.assertEqual("unavailable", self.run_once().status)
            self.tracker.begin.assert_not_called()
            self.adapter.analyze.assert_not_called()
            self.store.save_output.assert_not_called()
            self.task = replace(self.task, max_provider_calls=1)
            self.assertEqual("succeeded", self.run_once().status)
            self.adapter.analyze.assert_called_once()

    def test_non_success_outcome_cannot_carry_artifact(self):
        self.build("image.generate")
        with self.assertRaises(Exception):
            ImagePipelineOutcome(TASK_ID, STEP_ID, "failed", "X", artifact=self.artifact)


if __name__ == "__main__":
    unittest.main()
