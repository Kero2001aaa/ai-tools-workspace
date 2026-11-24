"""Deterministic, tracked image attempt against one exact adapter (Step 5.2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from agent.adult.adult_attempt_tracker import AdultAttemptTracker, AttemptResult
from agent.adult.adult_image_evaluator import AdultImageEvaluator
from agent.adult.adult_image_input import ImageInputError, decode_image_input, decode_image_prompt
from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.adult_image_validator import ImageValidationError, validate_image
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.integrations.protocols import (
    ImageAdapterResult, ImageAnalysisRequest, ImageAnalysisResult,
    ImageEditRequest, ImageGenerationRequest,
)
from agent.adult.models import (
    AdultModelValidationError, AdultTaskRequest, AdultTaskSnapshot,
    CapabilityQuery, CostPolicy, EvaluationResult, PolicyDecision,
    RouteCandidate, StepRecord,
)
from agent.adult.payload_store import AdultArtifactStore, ArtifactRef

_IMAGE_TASK_TYPES = frozenset({"image.analyze", "image.generate", "image.edit"})
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-")
_TRACKED_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "image_tokens", "provider_units",
})


class ImagePipelineError(RuntimeError):
    """Stable contract error without image, prompt, or provider details."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code if error_code == "invalid_request" else "invalid_request"
        super().__init__(self.error_code)


@dataclass(frozen=True)
class ImagePipelineOutcome:
    """Bounded result envelope; image bytes and analysis text are never part of it."""

    task_id: str
    step_id: str
    status: str
    error_code: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    attempt: AttemptResult | None = None
    evaluation: EvaluationResult | None = None
    artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "step_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise AdultModelValidationError(f"Outcome {name} is invalid.")
        if self.status not in {"succeeded", "failed", "rejected", "unavailable"}:
            raise AdultModelValidationError("Outcome status is invalid.")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not self.error_code.isascii()
            or not self.error_code.replace("_", "").isalnum() or len(self.error_code) > 64
        ):
            raise AdultModelValidationError("Outcome error code is invalid.")
        if self.status == "succeeded":
            if (
                self.error_code is not None or self.attempt is None
                or self.attempt.status != "succeeded"
                or self.evaluation is None or not self.evaluation.accepted
                or self.artifact is None
            ):
                raise AdultModelValidationError("Successful outcomes require an accepted published artifact.")
        elif self.artifact is not None:
            raise AdultModelValidationError("Non-success outcomes cannot expose an artifact.")
        if self.status == "unavailable" and self.attempt is not None:
            raise AdultModelValidationError("Unavailable routes cannot create attempts.")


class AdultImagePipeline:
    """Runs one tracked image attempt. An adapter result is untrusted until the evaluator accepts it."""

    def __init__(
        self,
        artifact_store: AdultArtifactStore,
        task_store: AdultTaskStore,
        router: AdultModelRouter,
        attempt_tracker: AdultAttemptTracker,
        image_policies: AdultImagePolicies,
        evaluator: AdultImageEvaluator,
        adapters: Mapping[tuple[str, str], object],
    ) -> None:
        for value, kind, name in (
            (artifact_store, AdultArtifactStore, "artifact_store"),
            (task_store, AdultTaskStore, "task_store"),
            (router, AdultModelRouter, "router"),
            (attempt_tracker, AdultAttemptTracker, "attempt_tracker"),
            (image_policies, AdultImagePolicies, "image_policies"),
            (evaluator, AdultImageEvaluator, "evaluator"),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}.")
        if not isinstance(adapters, Mapping):
            raise TypeError("adapters must be an explicit adapter mapping.")
        normalized: dict[tuple[str, str], object] = {}
        for key, adapter in adapters.items():
            if (
                not isinstance(key, tuple) or len(key) != 2
                or any(not isinstance(part, str) or not part or len(part) > 128 for part in key)
            ):
                raise TypeError("Adapter registry entries must be exact (adapter_id, version) keys.")
            normalized[key] = adapter
        self._artifact_store = artifact_store
        self._task_store = task_store
        self._router = router
        self._attempt_tracker = attempt_tracker
        self._image_policies = image_policies
        self._evaluator = evaluator
        self._adapters = normalized

    @staticmethod
    def _outcome(task, step_id, status, error_code=None, route=None,
                 attempt=None, evaluation=None, artifact=None) -> ImagePipelineOutcome:
        return ImagePipelineOutcome(
            task_id=task.task_id, step_id=step_id, status=status, error_code=error_code,
            adapter_id=route.adapter_id if route else None,
            adapter_version=route.adapter_version if route else None,
            provider_id=route.provider_id if route else None,
            model_id=route.model_id if route else None,
            attempt=attempt, evaluation=evaluation, artifact=artifact,
        )

    @staticmethod
    def _same_request(left: AdultTaskRequest, right: AdultTaskRequest) -> bool:
        return (
            left.task_type == right.task_type
            and left.input_fingerprint == right.input_fingerprint
            and left.instruction_fingerprint == right.instruction_fingerprint
            and left.payload_id == right.payload_id
            and left.retention_policy == right.retention_policy
            and left.requested_options == right.requested_options
        )

    @staticmethod
    def _safe_usage(usage: dict[str, int]) -> dict[str, int]:
        return {
            k: v for k, v in usage.items()
            if k in _TRACKED_USAGE_KEYS and isinstance(v, int)
            and not isinstance(v, bool) and 0 <= v <= 10**12
        }

    def _load_image(self, payload_id: str, task_id: str):
        data = self._artifact_store.load_input(payload_id, task_id)
        return validate_image(data, output=False)

    def run_one_attempt(
        self,
        task: AdultTaskSnapshot,
        step_id: str,
        task_request: AdultTaskRequest,
        config_fingerprint: str,
        excluded_approach_keys: set[str],
    ) -> ImagePipelineOutcome:
        """Execute no more than one adapter call; the caller retains lease ownership."""
        if not isinstance(task, AdultTaskSnapshot) or not isinstance(task_request, AdultTaskRequest):
            raise ImagePipelineError("invalid_request")
        if (
            task.task_type not in _IMAGE_TASK_TYPES
            or task_request.task_type != task.task_type
            or task.current_step_id != step_id
            or task.status not in {"analyzing", "running", "evaluating"}
            or not isinstance(config_fingerprint, str)
            or not 16 <= len(config_fingerprint) <= 128
            or any(char not in _SAFE_CHARS for char in config_fingerprint)
            or not isinstance(excluded_approach_keys, set)
        ):
            raise ImagePipelineError("invalid_request")
        try:
            persisted = self._task_store.get_task_request(task.task_id)
            step = self._task_store.get_step(task.task_id, step_id)
        except Exception:
            raise ImagePipelineError("invalid_request") from None
        if (
            persisted is None or not self._same_request(task_request, persisted)
            or task_request.payload_id is None or not isinstance(step, StepRecord)
            or step.task_id != task.task_id or step.step_id != step_id
            or step.operation != task.task_type or step.status not in {"pending", "running"}
            or step.input_payload_id != task_request.payload_id
            or step.config_fingerprint != config_fingerprint
        ):
            raise ImagePipelineError("invalid_request")

        task_type = task.task_type
        image = None
        try:
            raw_envelope = self._artifact_store.load_input(task_request.payload_id, task.task_id)
            envelope = decode_image_input(raw_envelope, task_type)
            if task_type != "image.generate":
                image = self._load_image(envelope.image_payload_id, task.task_id)
            if task_type != "image.analyze":
                decode_image_prompt(self._artifact_store.load_input(envelope.prompt_payload_id, task.task_id))
        except (ImageInputError, ImageValidationError):
            return self._outcome(task, step_id, "rejected", "INVALID_INPUT")
        except Exception:
            return self._outcome(task, step_id, "failed", "INVALID_INPUT")

        if image is not None:
            try:
                decision = self._image_policies.validate_image_input(task_type, image)
            except Exception:
                decision = None
            if not isinstance(decision, PolicyDecision):
                return self._outcome(task, step_id, "failed", "INVALID_INPUT")
            if not decision.allowed:
                return self._outcome(task, step_id, "rejected", "POLICY_REJECTED")

        modalities = {
            "image.analyze": (("image",), "text"),
            "image.generate": (("text",), "image"),
            "image.edit": (("image", "text"), "image"),
        }[task_type]
        try:
            if image is None:
                query = CapabilityQuery(
                    task_type=task_type, input_modalities=modalities[0],
                    output_modality=modalities[1],
                    input_fingerprint=task_request.input_fingerprint,
                    config_fingerprint=config_fingerprint,
                )
            else:
                query = CapabilityQuery(
                    task_type=task_type, input_modalities=modalities[0],
                    output_modality=modalities[1],
                    input_fingerprint=task_request.input_fingerprint,
                    config_fingerprint=config_fingerprint,
                    mime_type=image.mime_type, input_bytes=image.byte_count,
                    width=image.width, height=image.height,
                )
            cost_policy = CostPolicy(
                max_provider_calls=task.max_provider_calls,
                max_cost_microusd=task.max_cost_microusd, allow_local=True,
            )
            routes = self._router.choose_routes(query, cost_policy, excluded_approach_keys)
        except Exception:
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE")
        if not routes:
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE")
        route = routes[0]
        if (
            not isinstance(route, RouteCandidate) or route.task_type != task_type
            or route.cost_class not in {"local", "free_verified"}
        ):
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE")
        adapter = self._adapters.get((route.adapter_id, route.adapter_version))
        method = {"image.analyze": "analyze", "image.generate": "generate", "image.edit": "edit"}[task_type]
        if adapter is None or not callable(getattr(adapter, method, None)):
            return self._outcome(task, step_id, "unavailable", "ADAPTER_UNAVAILABLE", route)

        try:
            handle = self._attempt_tracker.begin(
                task.task_id, step_id, route, task_request.input_fingerprint, config_fingerprint,
            )
        except Exception:
            return self._outcome(task, step_id, "unavailable", "ATTEMPT_UNAVAILABLE", route)

        def fail(code: str, text: str, retryable: bool, status: str = "failed", evaluation=None):
            attempt = self._attempt_tracker.fail(handle, code, text, retryable)
            return self._outcome(task, step_id, status, code, route, attempt, evaluation)

        try:
            if task_type == "image.analyze":
                request = ImageAnalysisRequest(task.task_id, step_id, envelope.image_payload_id, image.mime_type)
            elif task_type == "image.generate":
                request = ImageGenerationRequest(task.task_id, step_id, envelope.prompt_payload_id)
            else:
                request = ImageEditRequest(
                    task.task_id, step_id, envelope.image_payload_id,
                    envelope.prompt_payload_id, image.mime_type,
                )
            result = getattr(adapter, method)(request)
        except TimeoutError:
            return fail("PROVIDER_TIMEOUT", "Provider request timed out.", True)
        except Exception as exc:
            adapter_code = getattr(exc, "error_code", None)
            if adapter_code in {"input_invalid", "input_unavailable"}:
                return fail("INVALID_INPUT", "Adapter input validation failed.", False)
            if adapter_code == "provider_timeout":
                return fail("PROVIDER_TIMEOUT", "Provider request timed out.", True)
            if adapter_code == "client_unavailable":
                return fail("BACKEND_UNAVAILABLE", "Image backend is unavailable.", True)
            if adapter_code in {"output_invalid", "output_too_large"}:
                return fail("MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", True)
            return fail("PROVIDER_ERROR", "Image adapter invocation failed.", True)

        expected = ImageAnalysisResult if task_type == "image.analyze" else ImageAdapterResult
        if not isinstance(result, expected):
            return fail("MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", True)

        try:
            if task_type == "image.analyze":
                evaluation = self._evaluator.evaluate(task_type, result)
            else:
                evaluation = self._evaluator.evaluate(
                    task_type, result,
                    output_mime_types=route.output_mime_types,
                    max_bytes=route.max_output_bytes,
                    max_width=route.max_output_width,
                    max_height=route.max_output_height,
                )
        except Exception:
            return fail("MODEL_OUTPUT_INVALID", "Output evaluation failed.", False)
        if not isinstance(evaluation, EvaluationResult):
            return fail("MODEL_OUTPUT_INVALID", "Output evaluation failed.", False)
        if not evaluation.accepted:
            if any(code in {"policy_rejected", "policy_unavailable"} for code in evaluation.reason_codes):
                return fail("POLICY_REJECTED", "Output policy rejected the generated result.", False, "rejected", evaluation)
            return fail("MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", evaluation.needs_revision, "failed", evaluation)
        if result.cost_microusd not in {None, 0}:
            return fail("COST_POLICY_BLOCKED", "Non-zero model cost is not permitted.", False, "failed", evaluation)

        usage = self._safe_usage(result.usage)
        try:
            if task_type == "image.analyze":
                data, mime = result.text.encode("utf-8", errors="strict"), "text/plain"
            else:
                data, mime = result.image_bytes, result.mime_type
            artifact = self._artifact_store.save_output(task.task_id, handle.attempt_id, data, mime)
        except Exception:
            return fail("OUTPUT_STORAGE_FAILED", "Output artifact could not be stored.", False, "failed", evaluation)

        try:
            attempt = self._attempt_tracker.succeed(handle, artifact.artifact_id, usage, result.cost_microusd)
        except Exception:
            try:
                self._artifact_store.discard_output(artifact.artifact_id, task.task_id, handle.attempt_id)
            except Exception:
                pass
            raise
        finally:
            del result
        return self._outcome(task, step_id, "succeeded", route=route, attempt=attempt,
                             evaluation=evaluation, artifact=artifact)
