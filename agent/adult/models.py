"""Immutable, validated data-transfer objects for Adult task persistence."""
from __future__ import annotations

import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Literal

TASK_TYPES = frozenset({
    "text.analyze", "text.generate", "text.rewrite",
    "image.analyze", "image.generate", "image.edit",
})
SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
TASK_STATUSES = frozenset({
    "queued", "analyzing", "running", "evaluating", "checkpointed",
    "waiting_user", "needs_input", "paused", "completed", "failed", "cancelled",
})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"analyzing", "running", "evaluating"})
STEP_OPERATIONS = frozenset({
    "intake", "text.analyze", "text.generate", "text.rewrite",
    "image.analyze", "image.generate", "image.edit", "evaluate",
})
STEP_STATUSES = frozenset({"pending", "running", "succeeded", "failed", "skipped"})
CHECKPOINT_REASONS = frozenset({
    "initial", "step_completed", "approval_required", "manual_yield",
    "lease_recovered", "needs_input", "paused", "cancelled",
})
EVENT_TYPES = frozenset({
    "task_created", "task_claimed", "lease_renewed", "lease_released",
    "step_created", "step_transition", "state_transition", "checkpoint_saved",
    "waiting_user", "task_resumed", "cancel_requested", "task_cancelled",
    "lease_expired_recovered", "cancel_recovered", "budget_exhausted",
})
RELEASE_REASONS = frozenset({
    "worker_released", "yielded", "cancel_observed", "budget_exhausted",
})
DECISIONS = frozenset({"continue", "cancel"})
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{15,127}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FORBIDDEN_KEY_RE = re.compile(
    r"(?i)(prompt|instruction|image|base64|payload_bytes|raw|secret|password|"
    r"api[_-]?key|authorization|token|credential|absolute_path|file_path|"
    r"provider_response|provider_request|content)"
)
_FORBIDDEN_VALUE_RE = re.compile(
    r"(?i)(https?://|[\\/](?:home|root|users|tmp)[\\/]|"
    r"\b(?:api[_-]?key|password|authorization|bearer)\b)"
)


class AdultModelValidationError(ValueError):
    """A caller supplied a DTO value outside the Stage 3 contract."""


def _timestamp(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AdultModelValidationError(f"{name} must be a normalized UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AdultModelValidationError(f"{name} must be a valid UTC timestamp.") from exc
    if parsed.utcoffset() is None:
        raise AdultModelValidationError(f"{name} must include UTC timezone information.")


def _integer(name: str, value: Any, minimum: int, maximum: int | None = None) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise AdultModelValidationError(f"{name} must be an integer >= {minimum}.")
    if maximum is not None and value > maximum:
        raise AdultModelValidationError(f"{name} must be <= {maximum}.")


def _identifier(name: str, value: Any, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise AdultModelValidationError(f"{name} must be a normalized opaque identifier.")


def _fingerprint(name: str, value: Any, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise AdultModelValidationError(f"{name} must be a keyed fingerprint, not source content.")


def _json_value(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise AdultModelValidationError("JSON nesting exceeds the supported depth.")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 2048:
            raise AdultModelValidationError("JSON string value is too long.")
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise AdultModelValidationError("Non-finite JSON numbers are not supported.")
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise AdultModelValidationError("JSON arrays are too large.")
        for item in value:
            _json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100:
            raise AdultModelValidationError("JSON objects are too large.")
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_KEY_RE.search(key):
                raise AdultModelValidationError("JSON contains an unapproved key.")
            _json_value(item, depth=depth + 1)
        return
    raise AdultModelValidationError("Value is not JSON-safe.")


def _safe_metadata(value: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdultModelValidationError("Metadata must be a JSON object.")
    _json_value(value)
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise AdultModelValidationError("Metadata exceeds its encoded size limit.")
    def inspect(item: Any) -> None:
        if isinstance(item, str) and _FORBIDDEN_VALUE_RE.search(item):
            raise AdultModelValidationError("Metadata contains a prohibited value.")
        if isinstance(item, dict):
            for child in item.values():
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)
    return value


@dataclass(frozen=True)
class AdultTaskRequest:
    task_type: str
    input_fingerprint: str
    instruction_fingerprint: str | None = None
    payload_id: str | None = None
    retention_policy: Literal["ephemeral", "encrypted"] = "ephemeral"
    requested_options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.task_type not in TASK_TYPES:
            raise AdultModelValidationError("Unsupported Adult task type.")
        _fingerprint("input_fingerprint", self.input_fingerprint)
        _fingerprint("instruction_fingerprint", self.instruction_fingerprint, optional=True)
        _identifier("payload_id", self.payload_id, optional=True)
        if self.retention_policy not in {"ephemeral", "encrypted"}:
            raise AdultModelValidationError("Unsupported payload retention policy.")
        if not isinstance(self.requested_options, dict) or self.requested_options:
            raise AdultModelValidationError("Stage 3 accepts no requested option keys.")
        _safe_metadata(self.requested_options, max_bytes=8192)


CAPABILITY_MODALITIES = frozenset({"text", "image"})
CAPABILITY_COST_CLASSES = frozenset({"local", "free_verified", "paid", "unknown"})
_CAPABILITY_TASK_MODALITIES = {
    "text.analyze": (("text",), ("text",)),
    "text.generate": (("text",), ("text",)),
    "text.rewrite": (("text",), ("text",)),
    "image.analyze": (("image",), ("text",)),
    "image.generate": (("text",), ("image",)),
    "image.edit": (("image", "text"), ("image",)),
}
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$")
_CAPABILITY_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _evidence_reference(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AdultModelValidationError(f"{name} must be a bounded non-secret reference.")
    if value.startswith("https://"):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or len(value) > 512
        ):
            raise AdultModelValidationError(f"{name} must be a sanitized HTTPS reference.")
        return
    if not _EVIDENCE_REF_RE.fullmatch(value) or ".." in value:
        raise AdultModelValidationError(f"{name} must be a normalized evidence reference.")


@dataclass(frozen=True)
class ModelCapabilitySpec:
    """Immutable capability contract supplied for registry registration/revision."""

    adapter_id: str
    adapter_version: str
    task_type: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    cost_class: Literal["local", "free_verified", "paid", "unknown"]
    provider_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    accepted_mime_types: tuple[str, ...] = ()
    output_mime_types: tuple[str, ...] = ()
    max_input_bytes: int | None = None
    max_output_bytes: int | None = None
    max_input_width: int | None = None
    max_input_height: int | None = None
    max_output_width: int | None = None
    max_output_height: int | None = None

    def __post_init__(self) -> None:
        _identifier("adapter_id", self.adapter_id)
        _identifier("adapter_version", self.adapter_version)
        for name in ("provider_id", "model_id", "model_version"):
            _identifier(name, getattr(self, name), optional=True)
        if self.task_type not in TASK_TYPES:
            raise AdultModelValidationError("Unsupported capability task_type.")
        if self.cost_class not in CAPABILITY_COST_CLASSES:
            raise AdultModelValidationError("Unsupported capability cost_class.")
        expected_input, expected_output = _CAPABILITY_TASK_MODALITIES[self.task_type]
        if (
            not isinstance(self.input_modalities, tuple)
            or len(self.input_modalities) != len(set(self.input_modalities))
            or set(self.input_modalities) != set(expected_input)
        ):
            raise AdultModelValidationError("input_modalities must exactly match the task contract.")
        if (
            not isinstance(self.output_modalities, tuple)
            or len(self.output_modalities) != len(set(self.output_modalities))
            or set(self.output_modalities) != set(expected_output)
        ):
            raise AdultModelValidationError("output_modalities must exactly match the task contract.")
        if self.cost_class == "free_verified" and (self.provider_id is None or self.model_id is None):
            raise AdultModelValidationError("free_verified capabilities require provider_id and model_id.")
        if not self.input_modalities or not self.output_modalities:
            raise AdultModelValidationError("Capability modalities cannot be empty.")

        has_image_input = "image" in self.input_modalities
        if has_image_input:
            if not isinstance(self.accepted_mime_types, tuple) or not 1 <= len(self.accepted_mime_types) <= 16:
                raise AdultModelValidationError("Image input capabilities require a MIME allowlist.")
            if len(set(self.accepted_mime_types)) != len(self.accepted_mime_types):
                raise AdultModelValidationError("Duplicate input MIME types are not allowed.")
            if any(not isinstance(v, str) or v not in SUPPORTED_IMAGE_MIME_TYPES for v in self.accepted_mime_types):
                raise AdultModelValidationError("Input MIME types must be explicit and cannot be wildcards.")
            _integer("max_input_bytes", self.max_input_bytes, 1, 15 * 1024 * 1024)
            _integer("max_input_width", self.max_input_width, 1, 10000)
            _integer("max_input_height", self.max_input_height, 1, 10000)
        elif (
            self.accepted_mime_types or self.max_input_bytes is not None
            or self.max_input_width is not None or self.max_input_height is not None
        ):
            raise AdultModelValidationError("Image input constraints are invalid for a text-only input.")

        has_image_output = "image" in self.output_modalities
        if has_image_output:
            if not isinstance(self.output_mime_types, tuple) or not 1 <= len(self.output_mime_types) <= 16:
                raise AdultModelValidationError("Image output capabilities require a MIME allowlist.")
            if len(set(self.output_mime_types)) != len(self.output_mime_types):
                raise AdultModelValidationError("Duplicate output MIME types are not allowed.")
            if any(not isinstance(v, str) or v not in SUPPORTED_IMAGE_MIME_TYPES for v in self.output_mime_types):
                raise AdultModelValidationError("Output MIME types must be explicit and cannot be wildcards.")
            _integer("max_output_bytes", self.max_output_bytes, 1, 20 * 1024 * 1024)
            _integer("max_output_width", self.max_output_width, 1, 10000)
            _integer("max_output_height", self.max_output_height, 1, 10000)
        elif (
            self.output_mime_types or self.max_output_bytes is not None
            or self.max_output_width is not None or self.max_output_height is not None
        ):
            raise AdultModelValidationError("Image output constraints are invalid for text output.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id, "adapter_version": self.adapter_version,
            "provider_id": self.provider_id, "model_id": self.model_id,
            "model_version": self.model_version, "task_type": self.task_type,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "accepted_mime_types": list(self.accepted_mime_types),
            "output_mime_types": list(self.output_mime_types),
            "max_input_bytes": self.max_input_bytes, "max_output_bytes": self.max_output_bytes,
            "max_input_width": self.max_input_width, "max_input_height": self.max_input_height,
            "max_output_width": self.max_output_width, "max_output_height": self.max_output_height,
            "cost_class": self.cost_class,
        }


@dataclass(frozen=True)
class CapabilityEvidence:
    """Evidence is bound to one exact adapter/model/operation contract."""

    capability_source: Literal[
        "official_provider_docs", "official_adapter_docs", "local_adapter_smoke_test"
    ]
    capability_reference: str
    verified_at: str
    expires_at: str
    adapter_id: str
    adapter_version: str
    task_type: str
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    provider_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    cost_source: Literal[
        "official_provider_pricing", "official_free_tier_terms"
    ] | None = None
    cost_reference: str | None = None
    cost_verified_at: str | None = None
    cost_expires_at: str | None = None
    cost_is_zero: bool | None = None

    def __post_init__(self) -> None:
        if self.capability_source not in {
            "official_provider_docs", "official_adapter_docs", "local_adapter_smoke_test"
        }:
            raise AdultModelValidationError("Unsupported capability evidence source.")
        _identifier("adapter_id", self.adapter_id)
        _identifier("adapter_version", self.adapter_version)
        _identifier("provider_id", self.provider_id, optional=True)
        _identifier("model_id", self.model_id, optional=True)
        _identifier("model_version", self.model_version, optional=True)
        if self.task_type not in TASK_TYPES:
            raise AdultModelValidationError("Unsupported evidence task_type.")
        expected_input, expected_output = _CAPABILITY_TASK_MODALITIES[self.task_type]
        if (
            not isinstance(self.input_modalities, tuple)
            or len(self.input_modalities) != len(set(self.input_modalities))
            or set(self.input_modalities) != set(expected_input)
        ):
            raise AdultModelValidationError("Evidence input modalities do not match task_type.")
        if (
            not isinstance(self.output_modalities, tuple)
            or len(self.output_modalities) != len(set(self.output_modalities))
            or set(self.output_modalities) != set(expected_output)
        ):
            raise AdultModelValidationError("Evidence output modalities do not match task_type.")
        _evidence_reference("capability_reference", self.capability_reference)
        _timestamp("verified_at", self.verified_at)
        _timestamp("expires_at", self.expires_at)
        if datetime.fromisoformat(self.expires_at[:-1] + "+00:00") <= datetime.fromisoformat(self.verified_at[:-1] + "+00:00"):
            raise AdultModelValidationError("Capability evidence must expire after verification.")

        cost_values = (self.cost_source, self.cost_reference, self.cost_verified_at, self.cost_expires_at)
        if all(value is None for value in cost_values):
            if self.cost_is_zero is not None:
                raise AdultModelValidationError("cost_is_zero requires complete cost evidence.")
            return
        if any(value is None for value in cost_values):
            raise AdultModelValidationError("Cost evidence fields must be supplied together.")
        if self.cost_source not in {"official_provider_pricing", "official_free_tier_terms"}:
            raise AdultModelValidationError("Unsupported cost evidence source.")
        if not isinstance(self.cost_is_zero, bool):
            raise AdultModelValidationError("Cost evidence must state whether the route is zero-cost.")
        _evidence_reference("cost_reference", self.cost_reference)
        _timestamp("cost_verified_at", self.cost_verified_at)
        _timestamp("cost_expires_at", self.cost_expires_at)
        if datetime.fromisoformat(self.cost_expires_at[:-1] + "+00:00") <= datetime.fromisoformat(self.cost_verified_at[:-1] + "+00:00"):
            raise AdultModelValidationError("Cost evidence must expire after verification.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id, "adapter_version": self.adapter_version,
            "provider_id": self.provider_id, "model_id": self.model_id,
            "model_version": self.model_version, "task_type": self.task_type,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "capability_source": self.capability_source,
            "capability_reference": self.capability_reference,
            "verified_at": self.verified_at, "expires_at": self.expires_at,
            "cost_source": self.cost_source, "cost_reference": self.cost_reference,
            "cost_verified_at": self.cost_verified_at, "cost_expires_at": self.cost_expires_at,
            "cost_is_zero": self.cost_is_zero,
        }


@dataclass(frozen=True)
class ModelCapability:
    capability_id: str
    spec: ModelCapabilitySpec
    enabled: bool
    verification_status: Literal["unverified", "verified", "revoked"]
    capability_version: str
    evidence: CapabilityEvidence | None
    state_version: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _identifier("capability_id", self.capability_id)
        if not isinstance(self.spec, ModelCapabilitySpec):
            raise AdultModelValidationError("spec must be a ModelCapabilitySpec.")
        if not isinstance(self.enabled, bool):
            raise AdultModelValidationError("enabled must be a boolean.")
        if self.verification_status not in {"unverified", "verified", "revoked"}:
            raise AdultModelValidationError("Invalid capability verification_status.")
        if not isinstance(self.capability_version, str) or not _CAPABILITY_HASH_RE.fullmatch(self.capability_version):
            raise AdultModelValidationError("capability_version must be a lowercase SHA-256 hash.")
        if self.verification_status == "verified" and not isinstance(self.evidence, CapabilityEvidence):
            raise AdultModelValidationError("Verified capabilities require evidence.")
        if self.verification_status != "verified" and self.evidence is not None:
            raise AdultModelValidationError("Unverified/revoked capabilities cannot retain active evidence.")
        if self.enabled and self.verification_status != "verified":
            raise AdultModelValidationError("Only verified capabilities can be enabled.")
        _integer("state_version", self.state_version, 1)
        _timestamp("created_at", self.created_at)
        _timestamp("updated_at", self.updated_at)


@dataclass(frozen=True)
class CapabilityQuery:
    task_type: str
    input_modalities: tuple[str, ...]
    output_modality: str
    input_fingerprint: str
    config_fingerprint: str
    mime_type: str | None = None
    input_bytes: int | None = None
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if self.task_type not in TASK_TYPES:
            raise AdultModelValidationError("Unsupported capability query task_type.")
        expected_input, expected_output = _CAPABILITY_TASK_MODALITIES[self.task_type]
        if not isinstance(self.input_modalities, tuple) or set(self.input_modalities) != set(expected_input) or len(self.input_modalities) != len(set(self.input_modalities)):
            raise AdultModelValidationError("Query input_modalities do not match task_type.")
        if self.output_modality != expected_output[0]:
            raise AdultModelValidationError("Query output_modality does not match task_type.")
        _fingerprint("input_fingerprint", self.input_fingerprint)
        _fingerprint("config_fingerprint", self.config_fingerprint)
        if "image" in self.input_modalities:
            if not isinstance(self.mime_type, str) or self.mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
                raise AdultModelValidationError("Image queries require an explicit MIME type.")
            _integer("input_bytes", self.input_bytes, 1, 15 * 1024 * 1024)
            _integer("width", self.width, 1, 10000)
            _integer("height", self.height, 1, 10000)
        elif any(value is not None for value in (self.mime_type, self.input_bytes, self.width, self.height)):
            raise AdultModelValidationError("Image metadata is not valid for a text-only input.")


@dataclass(frozen=True)
class CostPolicy:
    max_provider_calls: int
    max_cost_microusd: int = 0
    allow_local: bool = True

    def __post_init__(self) -> None:
        _integer("max_provider_calls", self.max_provider_calls, 0, 20)
        _integer("max_cost_microusd", self.max_cost_microusd, 0, 0)
        if not isinstance(self.allow_local, bool):
            raise AdultModelValidationError("allow_local must be a boolean.")

    @classmethod
    def from_budget(cls, budget: Any) -> "CostPolicy":
        if not isinstance(budget, TaskBudget):
            raise AdultModelValidationError("A validated TaskBudget is required.")
        return cls(
            max_provider_calls=budget.max_provider_calls,
            max_cost_microusd=budget.max_cost_microusd,
        )


@dataclass(frozen=True)
class RouteCandidate:
    """Verified route descriptor shared by Stage 5 tracking and Stage 6 routing."""

    adapter_id: str
    adapter_version: str
    provider_id: str | None
    model_id: str | None
    model_version: str | None
    tool_id: str | None
    tool_version: str | None
    task_type: str
    cost_class: Literal["local", "free_verified", "paid", "unknown"]
    capability_version: str
    verified_at: str

    def __post_init__(self) -> None:
        for name in ("adapter_id", "adapter_version", "capability_version"):
            _identifier(name, getattr(self, name))
        for name in ("provider_id", "model_id", "model_version", "tool_id", "tool_version"):
            _identifier(name, getattr(self, name), optional=True)
        if self.task_type not in TASK_TYPES:
            raise AdultModelValidationError("Route task_type is unsupported.")
        if self.cost_class not in {"local", "free_verified", "paid", "unknown"}:
            raise AdultModelValidationError("Route cost_class is unsupported.")
        _timestamp("verified_at", self.verified_at)


@dataclass(frozen=True)
class TaskBudget:
    max_total_attempts: int = 20
    max_wall_seconds: int = 600
    max_provider_calls: int = 0
    max_cost_microusd: int = 0
    failure_checkpoint_interval: int = 5

    def __post_init__(self) -> None:
        _integer("max_total_attempts", self.max_total_attempts, 1, 20)
        _integer("max_wall_seconds", self.max_wall_seconds, 1, 600)
        _integer("max_provider_calls", self.max_provider_calls, 0, 20)
        _integer("max_cost_microusd", self.max_cost_microusd, 0, 0)
        if self.failure_checkpoint_interval != 5 or isinstance(self.failure_checkpoint_interval, bool):
            raise AdultModelValidationError("failure_checkpoint_interval must equal 5.")


@dataclass(frozen=True)
class AdultTaskSnapshot:
    task_id: str
    task_type: str
    status: str
    created_at: str
    updated_at: str
    state_version: int
    current_step_id: str | None
    attempt_count: int
    failed_attempts_since_approval: int
    provider_calls: int
    approvals_granted: int
    max_total_attempts: int
    max_wall_seconds: int
    max_provider_calls: int
    max_cost_microusd: int
    spent_cost_microusd: int
    deadline_at: str
    selected_adapter: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_tool: str | None = None
    latest_result_artifact_id: str | None = None
    last_error_code: str | None = None
    user_action_required: bool = False
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        _identifier("task_id", self.task_id)
        if self.task_type not in TASK_TYPES or self.status not in TASK_STATUSES:
            raise AdultModelValidationError("Invalid task type or status in snapshot.")
        _timestamp("created_at", self.created_at)
        _timestamp("updated_at", self.updated_at)
        _timestamp("deadline_at", self.deadline_at)
        for name in ("state_version", "attempt_count", "failed_attempts_since_approval",
                     "provider_calls", "approvals_granted", "spent_cost_microusd"):
            _integer(name, getattr(self, name), 0)
        _integer("max_total_attempts", self.max_total_attempts, 1, 20)
        _integer("max_wall_seconds", self.max_wall_seconds, 1, 600)
        _integer("max_provider_calls", self.max_provider_calls, 0, 20)
        _integer("max_cost_microusd", self.max_cost_microusd, 0, 0)
        for name in ("current_step_id", "selected_adapter", "selected_provider", "selected_model",
                     "selected_tool", "latest_result_artifact_id"):
            _identifier(name, getattr(self, name), optional=True)
        if self.last_error_code is not None and not _SAFE_CODE_RE.fullmatch(self.last_error_code):
            raise AdultModelValidationError("last_error_code must be normalized.")
        if not isinstance(self.user_action_required, bool) or not isinstance(self.cancel_requested, bool):
            raise AdultModelValidationError("Snapshot flags must be booleans.")


@dataclass(frozen=True, init=False)
class TaskLease:
    task_id: str
    worker_id: str
    lease_generation: int
    expires_at: str
    state_version_at_grant: int

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("TaskLease values are issued only by AdultTaskStore.")

    @classmethod
    def _from_store(cls, task_id: str, worker_id: str, lease_generation: int,
                    expires_at: str, state_version_at_grant: int) -> "TaskLease":
        instance = object.__new__(cls)
        for name, value in (("task_id", task_id), ("worker_id", worker_id),
                            ("lease_generation", lease_generation), ("expires_at", expires_at),
                            ("state_version_at_grant", state_version_at_grant)):
            object.__setattr__(instance, name, value)
        instance._validate()
        return instance

    def _validate(self) -> None:
        _identifier("task_id", self.task_id)
        _identifier("worker_id", self.worker_id)
        _integer("lease_generation", self.lease_generation, 1)
        _integer("state_version_at_grant", self.state_version_at_grant, 0)
        _timestamp("expires_at", self.expires_at)


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    task_id: str
    sequence_no: int
    operation: str
    status: str
    adapter_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    tool_id: str | None = None
    input_payload_id: str | None = None
    output_artifact_id: str | None = None
    config_fingerprint: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        _identifier("step_id", self.step_id)
        _identifier("task_id", self.task_id)
        _integer("sequence_no", self.sequence_no, 0)
        if self.operation not in STEP_OPERATIONS or self.status not in STEP_STATUSES:
            raise AdultModelValidationError("Invalid step operation or status.")
        for name in ("adapter_id", "provider_id", "model_id", "tool_id",
                     "input_payload_id", "output_artifact_id"):
            _identifier(name, getattr(self, name), optional=True)
        _fingerprint("config_fingerprint", self.config_fingerprint, optional=True)
        if self.started_at is not None:
            _timestamp("started_at", self.started_at)
        if self.completed_at is not None:
            _timestamp("completed_at", self.completed_at)
        if self.status == "pending" and (self.started_at is not None or self.completed_at is not None):
            raise AdultModelValidationError("Pending steps cannot have execution timestamps.")
        if self.status == "running" and self.completed_at is not None:
            raise AdultModelValidationError("Running steps cannot have completed_at.")
        # Step timestamps are output fields owned by AdultTaskStore. Write DTOs
        # may therefore express a target status without supplying timestamps.
        if (
            self.started_at is not None and self.completed_at is not None
            and datetime.fromisoformat(self.completed_at[:-1] + "+00:00")
                < datetime.fromisoformat(self.started_at[:-1] + "+00:00")
        ):
            raise AdultModelValidationError("Step completion cannot precede its start.")


@dataclass(frozen=True)
class CheckpointSnapshot:
    schema_version: int
    task_id: str
    task_type: str
    status: str
    active_step_id: str | None = None
    input_fingerprint: str | None = None
    payload_id: str | None = None
    plan: tuple[str, ...] = ()
    completed_step_ids: tuple[str, ...] = ()
    pending_step_ids: tuple[str, ...] = ()
    latest_result_artifact_id: str | None = None
    selected_adapter: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_tool: str | None = None
    attempted_approach_keys: tuple[str, ...] = ()
    excluded_approach_keys: tuple[str, ...] = ()
    total_attempts: int = 0
    failed_attempts_since_approval: int = 0
    provider_calls: int = 0
    spent_cost_microusd: int = 0
    remaining_attempts: int = 0
    remaining_wall_seconds: int = 0
    remaining_provider_calls: int = 0
    remaining_cost_microusd: int = 0
    last_error_code: str | None = None
    sanitized_summary: str | None = None
    pending_user_action: str | None = None
    allowed_decisions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _integer("schema_version", self.schema_version, 1)
        _identifier("task_id", self.task_id)
        if self.task_type not in TASK_TYPES or self.status not in TASK_STATUSES:
            raise AdultModelValidationError("Invalid checkpoint task type or status.")
        for name in ("active_step_id", "payload_id", "latest_result_artifact_id",
                     "selected_adapter", "selected_provider", "selected_model", "selected_tool"):
            _identifier(name, getattr(self, name), optional=True)
        _fingerprint("input_fingerprint", self.input_fingerprint, optional=True)
        for name in ("plan", "completed_step_ids", "pending_step_ids",
                     "attempted_approach_keys", "excluded_approach_keys"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > 100:
                raise AdultModelValidationError(f"{name} must be a bounded tuple.")
            for value in values:
                _identifier(name, value)
        for name in ("total_attempts", "failed_attempts_since_approval", "provider_calls",
                     "spent_cost_microusd", "remaining_attempts", "remaining_wall_seconds",
                     "remaining_provider_calls", "remaining_cost_microusd"):
            _integer(name, getattr(self, name), 0)
        if self.last_error_code is not None and not _SAFE_CODE_RE.fullmatch(self.last_error_code):
            raise AdultModelValidationError("Checkpoint error code must be normalized.")
        if self.sanitized_summary is not None and (
            not isinstance(self.sanitized_summary, str) or len(self.sanitized_summary) > 1000
            or _FORBIDDEN_VALUE_RE.search(self.sanitized_summary)
        ):
            raise AdultModelValidationError("Checkpoint summary is not sanitized.")
        if self.pending_user_action is not None and (
            not isinstance(self.pending_user_action, str) or len(self.pending_user_action) > 128
            or not _SAFE_CODE_RE.fullmatch(self.pending_user_action)
        ):
            raise AdultModelValidationError("pending_user_action must be a normalized code.")
        if not isinstance(self.allowed_decisions, tuple) or any(
            value not in DECISIONS for value in self.allowed_decisions
        ):
            raise AdultModelValidationError("Unsupported checkpoint decision.")
        self.to_json()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "task_id": self.task_id,
            "task_type": self.task_type, "status": self.status,
            "active_step_id": self.active_step_id, "input_fingerprint": self.input_fingerprint,
            "payload_id": self.payload_id, "plan": list(self.plan),
            "completed_step_ids": list(self.completed_step_ids),
            "pending_step_ids": list(self.pending_step_ids),
            "latest_result_artifact_id": self.latest_result_artifact_id,
            "selected_adapter": self.selected_adapter, "selected_provider": self.selected_provider,
            "selected_model": self.selected_model, "selected_tool": self.selected_tool,
            "attempted_approach_keys": list(self.attempted_approach_keys),
            "excluded_approach_keys": list(self.excluded_approach_keys),
            "total_attempts": self.total_attempts,
            "failed_attempts_since_approval": self.failed_attempts_since_approval,
            "provider_calls": self.provider_calls, "spent_cost_microusd": self.spent_cost_microusd,
            "remaining_attempts": self.remaining_attempts,
            "remaining_wall_seconds": self.remaining_wall_seconds,
            "remaining_provider_calls": self.remaining_provider_calls,
            "remaining_cost_microusd": self.remaining_cost_microusd,
            "last_error_code": self.last_error_code, "sanitized_summary": self.sanitized_summary,
            "pending_user_action": self.pending_user_action,
            "allowed_decisions": list(self.allowed_decisions),
        }

    def to_json(self) -> str:
        encoded = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > 65536:
            raise AdultModelValidationError("Checkpoint snapshot exceeds 64 KiB.")
        return encoded


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    revision: int
    reason: str
    schema_version: int
    failed_attempts_since_approval: int
    user_action_required: bool
    created_at: str
    snapshot: CheckpointSnapshot

    def __post_init__(self) -> None:
        _identifier("checkpoint_id", self.checkpoint_id)
        _identifier("task_id", self.task_id)
        _integer("revision", self.revision, 1)
        if self.reason not in CHECKPOINT_REASONS:
            raise AdultModelValidationError("Invalid checkpoint reason.")
        _integer("schema_version", self.schema_version, 1)
        _integer("failed_attempts_since_approval", self.failed_attempts_since_approval, 0, 5)
        _timestamp("created_at", self.created_at)
        if not isinstance(self.snapshot, CheckpointSnapshot) or self.snapshot.task_id != self.task_id:
            raise AdultModelValidationError("Checkpoint snapshot must match its task.")
        if self.snapshot.schema_version != self.schema_version:
            raise AdultModelValidationError("Checkpoint schema version does not match its snapshot.")
        if not isinstance(self.user_action_required, bool):
            raise AdultModelValidationError("user_action_required must be boolean.")


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    task_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    metadata: dict[str, Any]
    created_at: str

    def __post_init__(self) -> None:
        _identifier("event_id", self.event_id)
        _identifier("task_id", self.task_id)
        _timestamp("created_at", self.created_at)
        if self.event_type not in EVENT_TYPES:
            raise AdultModelValidationError("Unsupported task event.")
        if self.from_status is not None and self.from_status not in TASK_STATUSES:
            raise AdultModelValidationError("Invalid event from_status.")
        if self.to_status is not None and self.to_status not in TASK_STATUSES:
            raise AdultModelValidationError("Invalid event to_status.")
        _safe_metadata(self.metadata, max_bytes=8192)


@dataclass(frozen=True)
class TaskPage:
    items: tuple[AdultTaskSnapshot, ...]
    next_cursor: str | None
    has_more: bool

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(not isinstance(x, AdultTaskSnapshot) for x in self.items):
            raise AdultModelValidationError("TaskPage items must be immutable task snapshots.")
        if self.next_cursor is not None and (not isinstance(self.next_cursor, str) or len(self.next_cursor) > 2048):
            raise AdultModelValidationError("Invalid task cursor.")
        if not isinstance(self.has_more, bool):
            raise AdultModelValidationError("has_more must be boolean.")


_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


@dataclass(frozen=True)
class PolicyDecision:
    """Bounded deterministic policy outcome; never contains source or output text."""

    allowed: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise AdultModelValidationError("PolicyDecision.allowed must be boolean.")
        if (
            not isinstance(self.reason_codes, tuple)
            or len(self.reason_codes) > 8
            or len(self.reason_codes) != len(set(self.reason_codes))
            or any(not isinstance(code, str) or not _REASON_CODE_RE.fullmatch(code)
                   for code in self.reason_codes)
        ):
            raise AdultModelValidationError("PolicyDecision reason codes are invalid.")
        if self.allowed and self.reason_codes:
            raise AdultModelValidationError("An allowed policy decision cannot contain denial reasons.")
        if not self.allowed and not self.reason_codes:
            raise AdultModelValidationError("A denied policy decision requires a reason code.")


@dataclass(frozen=True)
class EvaluationResult:
    """Deterministic output checks; score is a pass fraction, not semantic quality."""

    accepted: bool
    score: float
    checks: tuple[str, ...]
    reason_codes: tuple[str, ...]
    needs_revision: bool

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not isinstance(self.needs_revision, bool):
            raise AdultModelValidationError("Evaluation flags must be booleans.")
        if (
            isinstance(self.score, bool) or not isinstance(self.score, (int, float))
            or not 0.0 <= float(self.score) <= 1.0
        ):
            raise AdultModelValidationError("Evaluation score must be between zero and one.")
        if (
            not isinstance(self.checks, tuple) or len(self.checks) > 16
            or len(self.checks) != len(set(self.checks))
            or any(not isinstance(item, str) or not _CHECK_ID_RE.fullmatch(item)
                   for item in self.checks)
        ):
            raise AdultModelValidationError("Evaluation checks are invalid.")
        if (
            not isinstance(self.reason_codes, tuple) or len(self.reason_codes) > 8
            or len(self.reason_codes) != len(set(self.reason_codes))
            or any(not isinstance(code, str) or not _REASON_CODE_RE.fullmatch(code)
                   for code in self.reason_codes)
        ):
            raise AdultModelValidationError("Evaluation reason codes are invalid.")
        if self.accepted and (self.reason_codes or self.needs_revision):
            raise AdultModelValidationError("Accepted evaluation cannot request revision or contain reasons.")
        if not self.accepted and not self.reason_codes:
            raise AdultModelValidationError("Rejected evaluation requires a reason code.")
        if self.needs_revision and self.accepted:
            raise AdultModelValidationError("Accepted evaluation cannot need revision.")
