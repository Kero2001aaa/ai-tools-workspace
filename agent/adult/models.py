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
        if self.status == "running" and (self.started_at is None or self.completed_at is not None):
            raise AdultModelValidationError("Running steps require started_at and cannot have completed_at.")
        if self.status in {"succeeded", "failed", "skipped"} and self.completed_at is None:
            raise AdultModelValidationError("Finished steps require completed_at.")
        if self.status in {"succeeded", "failed"} and self.started_at is None:
            raise AdultModelValidationError("Executed steps require started_at.")


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
