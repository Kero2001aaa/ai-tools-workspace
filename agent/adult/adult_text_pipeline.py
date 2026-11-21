"""One-attempt Adult text execution pipeline for Stage 7.

The caller owns task claiming, lease lifecycle, task status transitions, and retries.
This module performs at most one adapter invocation and never returns plaintext output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from agent.adult.adult_attempt_tracker import (
    AdultAttemptTracker,
    AttemptHandle,
    AttemptResult,
)
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import (
    AdultModelValidationError,
    AdultTaskRequest,
    AdultTaskSnapshot,
    CapabilityQuery,
    CostPolicy,
    EvaluationResult,
    PolicyDecision,
    RouteCandidate,
    StepRecord,
)
from agent.adult.payload_store import (
    AdultArtifactStore,
    ArtifactRef,
)
from agent.adult.adult_text_input import TextInputError, decode_text_input
from agent.adult.integrations.protocols import TextAdapterResult, TextRequest

_TEXT_TASK_TYPES = frozenset({"text.analyze", "text.generate", "text.rewrite"})
_TRACKED_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "image_tokens", "provider_units",
})
_USAGE_ALIASES = {
    "input_tokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "total_tokens": "total_tokens",
    "image_tokens": "image_tokens",
    "provider_units": "provider_units",
}


class TextPipelineError(RuntimeError):
    """Stable pipeline contract error without input, output, or provider details."""

    def __init__(self, error_code: str) -> None:
        allowed = {
            "invalid_request", "invalid_input", "policy_rejected",
            "capability_unavailable", "adapter_unavailable",
            "attempt_unavailable", "provider_error", "provider_timeout",
            "model_output_invalid", "output_storage_failed",
        }
        self.error_code = error_code if error_code in allowed else "invalid_request"
        super().__init__(self.error_code)


@dataclass(frozen=True)
class TextPipelineOutcome:
    """Bounded result envelope; generated/source text is never part of this DTO."""

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
        if not isinstance(self.task_id, str) or not self.task_id or len(self.task_id) > 128:
            raise AdultModelValidationError("Outcome task_id is invalid.")
        if not isinstance(self.step_id, str) or not self.step_id or len(self.step_id) > 128:
            raise AdultModelValidationError("Outcome step_id is invalid.")
        if self.status not in {"succeeded", "failed", "rejected", "unavailable"}:
            raise AdultModelValidationError("Outcome status is invalid.")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not self.error_code.isascii()
            or not self.error_code.replace("_", "").isalnum()
            or len(self.error_code) > 64
        ):
            raise AdultModelValidationError("Outcome error code is invalid.")
        for name in ("adapter_id", "adapter_version", "provider_id", "model_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 128
            ):
                raise AdultModelValidationError(f"Outcome {name} is invalid.")
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


class AdultTextPipeline:
    """Runs one deterministic, tracked text attempt against one exact adapter."""

    def __init__(
        self,
        artifact_store: AdultArtifactStore,
        task_store: AdultTaskStore,
        router: AdultModelRouter,
        attempt_tracker: AdultAttemptTracker,
        policies: AdultPolicies,
        evaluator: AdultEvaluator,
        adapters: Mapping[tuple[str, str], object],
    ) -> None:
        if not isinstance(artifact_store, AdultArtifactStore):
            raise TypeError("artifact_store must be an AdultArtifactStore.")
        if not isinstance(task_store, AdultTaskStore):
            raise TypeError("task_store must be an AdultTaskStore.")
        if not isinstance(router, AdultModelRouter):
            raise TypeError("router must be an AdultModelRouter.")
        if not isinstance(attempt_tracker, AdultAttemptTracker):
            raise TypeError("attempt_tracker must be an AdultAttemptTracker.")
        if not isinstance(policies, AdultPolicies):
            raise TypeError("policies must be an AdultPolicies.")
        if not isinstance(evaluator, AdultEvaluator):
            raise TypeError("evaluator must be an AdultEvaluator.")
        if not isinstance(adapters, Mapping):
            raise TypeError("adapters must be an explicit adapter mapping.")
        normalized: dict[tuple[str, str], object] = {}
        for key, adapter in adapters.items():
            if (
                not isinstance(key, tuple) or len(key) != 2
                or any(not isinstance(part, str) or not part or len(part) > 128 for part in key)
                or not callable(getattr(adapter, "invoke", None))
            ):
                raise TypeError("Adapter registry entries must be exact (adapter_id, version) invoke adapters.")
            normalized[key] = adapter
        self._artifact_store = artifact_store
        self._task_store = task_store
        self._router = router
        self._attempt_tracker = attempt_tracker
        self._policies = policies
        self._evaluator = evaluator
        self._adapters = normalized

    @staticmethod
    def _outcome(
        task: AdultTaskSnapshot,
        step_id: str,
        status: str,
        error_code: str | None = None,
        route: RouteCandidate | None = None,
        attempt: AttemptResult | None = None,
        evaluation: EvaluationResult | None = None,
        artifact: ArtifactRef | None = None,
    ) -> TextPipelineOutcome:
        return TextPipelineOutcome(
            task_id=task.task_id,
            step_id=step_id,
            status=status,
            error_code=error_code,
            adapter_id=route.adapter_id if route else None,
            adapter_version=route.adapter_version if route else None,
            provider_id=route.provider_id if route else None,
            model_id=route.model_id if route else None,
            attempt=attempt,
            evaluation=evaluation,
            artifact=artifact,
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
        result: dict[str, int] = {}
        for source_key, value in usage.items():
            target = _USAGE_ALIASES.get(source_key)
            if target in _TRACKED_USAGE_KEYS and target not in result:
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12:
                    result[target] = value
        return result

    def _fail(
        self,
        handle: AttemptHandle,
        error_code: str,
        sanitized_error: str,
        retryable: bool,
    ) -> AttemptResult:
        return self._attempt_tracker.fail(
            handle, error_code, sanitized_error, retryable,
        )

    def run_one_attempt(
        self,
        task: AdultTaskSnapshot,
        step_id: str,
        task_request: AdultTaskRequest,
        config_fingerprint: str,
        excluded_approach_keys: set[str],
    ) -> TextPipelineOutcome:
        """Execute no more than one adapter call; the caller retains lease ownership."""
        if not isinstance(task, AdultTaskSnapshot) or not isinstance(task_request, AdultTaskRequest):
            raise TextPipelineError("invalid_request")
        if (
            task.task_type not in _TEXT_TASK_TYPES
            or task_request.task_type != task.task_type
            or task.current_step_id != step_id
            or task.status not in {"analyzing", "running", "evaluating"}
            or not isinstance(config_fingerprint, str)
            or not 16 <= len(config_fingerprint) <= 128
            or not config_fingerprint.isascii()
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-" for char in config_fingerprint)
            or not isinstance(excluded_approach_keys, set)
        ):
            raise TextPipelineError("invalid_request")

        try:
            persisted_request = self._task_store.get_task_request(task.task_id)
            step = self._task_store.get_step(task.task_id, step_id)
        except Exception:
            raise TextPipelineError("invalid_request") from None
        if (
            persisted_request is None
            or not self._same_request(task_request, persisted_request)
            or task_request.payload_id is None
            or step is None
            or not isinstance(step, StepRecord)
            or step.task_id != task.task_id
            or step.step_id != step_id
            or step.operation != task.task_type
            or step.status not in {"pending", "running"}
            or step.input_payload_id != task_request.payload_id
            or step.config_fingerprint != config_fingerprint
        ):
            raise TextPipelineError("invalid_request")

        try:
            raw_input = self._artifact_store.load_input(task_request.payload_id, task.task_id)
            envelope = decode_text_input(raw_input, task.task_type, policies=self._policies)
        except TextInputError as exc:
            return self._outcome(task, step_id, "rejected", "POLICY_REJECTED" if exc.error_code == "input_policy_rejected" else "INVALID_INPUT")
        except Exception:
            return self._outcome(task, step_id, "failed", "INVALID_INPUT")

        try:
            input_decision = self._policies.validate_text_task(
                envelope.task_type, envelope.instruction, envelope.text,
            )
        except Exception:
            input_decision = None
        if not isinstance(input_decision, PolicyDecision):
            return self._outcome(task, step_id, "failed", "INVALID_INPUT")
        if not input_decision.allowed:
            return self._outcome(task, step_id, "rejected", "POLICY_REJECTED")

        query = CapabilityQuery(
            task_type=task.task_type,
            input_modalities=("text",),
            output_modality="text",
            input_fingerprint=task_request.input_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        cost_policy = CostPolicy(
            max_provider_calls=task.max_provider_calls,
            max_cost_microusd=task.max_cost_microusd,
            allow_local=True,
        )
        try:
            routes = self._router.choose_routes(query, cost_policy, excluded_approach_keys)
        except Exception:
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE")
        if not routes:
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE")
        route = routes[0]
        if (
            not isinstance(route, RouteCandidate)
            or route.task_type != task.task_type
            or route.cost_class not in {"local", "free_verified"}
        ):
            return self._outcome(task, step_id, "unavailable", "CAPABILITY_UNAVAILABLE", route if isinstance(route, RouteCandidate) else None)
        adapter = self._adapters.get((route.adapter_id, route.adapter_version))
        if adapter is None or not callable(getattr(adapter, "invoke", None)):
            return self._outcome(task, step_id, "unavailable", "ADAPTER_UNAVAILABLE", route)

        try:
            handle = self._attempt_tracker.begin(
                task.task_id, step_id, route, task_request.input_fingerprint,
                config_fingerprint,
            )
        except Exception:
            return self._outcome(task, step_id, "unavailable", "ATTEMPT_UNAVAILABLE", route)

        request = TextRequest(
            task_id=task.task_id,
            step_id=step_id,
            task_type=task.task_type,
            input_payload_id=task_request.payload_id,
            max_output_chars=20000,
        )
        try:
            adapter_result = adapter.invoke(request)
        except TimeoutError:
            attempt = self._fail(handle, "PROVIDER_TIMEOUT", "Provider request timed out.", True)
            return self._outcome(task, step_id, "failed", "PROVIDER_TIMEOUT", route, attempt)
        except Exception as exc:
            adapter_code = getattr(exc, "error_code", None)
            if adapter_code in {"input_invalid", "input_unavailable"}:
                code, safe, retryable = "INVALID_INPUT", "Adapter input validation failed.", False
            elif adapter_code == "client_unavailable":
                code, safe, retryable = "BACKEND_UNAVAILABLE", "Text backend is unavailable.", True
            elif adapter_code in {"output_invalid", "output_too_large"}:
                code, safe, retryable = "MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", True
            else:
                code, safe, retryable = "PROVIDER_ERROR", "Text adapter invocation failed.", True
            attempt = self._fail(handle, code, safe, retryable)
            return self._outcome(task, step_id, "failed", code, route, attempt)

        if not isinstance(adapter_result, TextAdapterResult):
            attempt = self._fail(handle, "MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", True)
            return self._outcome(task, step_id, "failed", "MODEL_OUTPUT_INVALID", route, attempt)

        evaluation = self._evaluator.evaluate(task.task_type, adapter_result.text)
        if not evaluation.accepted:
            if any(code in {"policy_rejected", "policy_unavailable"} for code in evaluation.reason_codes):
                attempt = self._fail(handle, "POLICY_REJECTED", "Output policy rejected the generated text.", False)
                return self._outcome(task, step_id, "rejected", "POLICY_REJECTED", route, attempt, evaluation)
            attempt = self._fail(handle, "MODEL_OUTPUT_INVALID", "Model output failed deterministic validation.", True)
            return self._outcome(task, step_id, "failed", "MODEL_OUTPUT_INVALID", route, attempt, evaluation)

        usage = self._safe_usage(adapter_result.usage)
        cost = adapter_result.cost_microusd
        if cost not in {None, 0}:
            attempt = self._fail(handle, "COST_POLICY_BLOCKED", "Non-zero model cost is not permitted.", False)
            return self._outcome(task, step_id, "failed", "COST_POLICY_BLOCKED", route, attempt, evaluation)

        artifact: ArtifactRef | None = None
        output_bytes: bytes | None = None
        try:
            output_bytes = adapter_result.text.encode("utf-8", errors="strict")
            artifact = self._artifact_store.save_output(
                task.task_id, handle.attempt_id, output_bytes, "text/plain",
            )
        except Exception:
            attempt = self._fail(handle, "OUTPUT_STORAGE_FAILED", "Output artifact could not be stored.", False)
            return self._outcome(task, step_id, "failed", "OUTPUT_STORAGE_FAILED", route, attempt, evaluation)

        try:
            attempt = self._attempt_tracker.succeed(
                handle, artifact.artifact_id, usage, cost,
            )
        except Exception:
            try:
                self._artifact_store.discard_output(
                    artifact.artifact_id, task.task_id, handle.attempt_id,
                )
            except Exception:
                # The artifact remains unreachable from the task result and is bounded by retention.
                pass
            raise
        finally:
            # Do not retain a plaintext reference in the returned pipeline object.
            del adapter_result
            del envelope
            del raw_input
            del output_bytes

        return self._outcome(
            task, step_id, "succeeded", route=route, attempt=attempt,
            evaluation=evaluation, artifact=artifact,
        )
