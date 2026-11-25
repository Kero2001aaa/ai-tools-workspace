"""Durable attempt tracking and retry de-duplication for Adult Agent Stage 5."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator

from agent.adult.adult_task_store import ACTIVE_STATUSES, AdultTaskStore
from agent.adult.adult_task_store import LeaseLost, TaskNotFound
from agent.adult.models import AdultModelValidationError, RouteCandidate, TaskLease


_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{15,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,;:_()'!-]{0,511}$")
_UNSAFE_TEXT_RE = re.compile(
    r"(?i)(https?://|api[_-]?key|password|authorization|bearer|token|secret|"
    r"credential|prompt|instruction|base64|payload|provider request body|"
    r"provider response body|request body|response body|[\\/](?:home|root|users|tmp)[\\/])"
)
_USAGE_KEYS = frozenset({
    "input_tokens", "output_tokens", "total_tokens",
    "image_tokens", "provider_units",
})
_MAX_USAGE_VALUE = 10**12

ATTEMPT_STATUSES = frozenset({
    "running", "succeeded", "failed", "rejected", "timed_out", "unknown_outcome",
})
FAILURE_CLASSES = frozenset({
    "transient", "capability_mismatch", "permanent_request",
    "policy_rejection", "budget_exhaustion", "unknown",
})
_TERMINAL_STATUSES = ATTEMPT_STATUSES - {"running"}

_TRANSIENT_CODES = frozenset({
    "PROVIDER_TIMEOUT", "PROVIDER_RATE_LIMIT", "PROVIDER_ERROR",
    "CONNECTION_RESET", "BACKEND_UNAVAILABLE", "LOCAL_BACKEND_UNAVAILABLE",
})
_TIMEOUT_CODES = frozenset({"PROVIDER_TIMEOUT"})
_CAPABILITY_CODES = frozenset({"CAPABILITY_UNAVAILABLE", "ROUTE_NOT_VERIFIED"})
_PERMANENT_CODES = frozenset({
    "INVALID_INPUT", "INVALID_IMAGE", "INVALID_SCHEMA", "INVALID_CONFIGURATION",
})
_POLICY_CODES = frozenset({"POLICY_REJECTED"})
_BUDGET_CODES = frozenset({
    "ATTEMPT_BUDGET_EXHAUSTED", "WALL_TIME_BUDGET_EXHAUSTED",
    "PROVIDER_CALL_BUDGET_EXHAUSTED", "COST_POLICY_BLOCKED",
})


class AttemptTrackerError(Exception):
    """Base class for stable AttemptTracker errors."""


class AttemptValidationError(AttemptTrackerError):
    error_code = "invalid_attempt"


class AttemptNotAvailable(AttemptTrackerError):
    error_code = "attempt_not_available"


class AttemptBudgetExhausted(AttemptTrackerError):
    error_code = "attempt_budget_exhausted"


class AttemptApprovalRequired(AttemptTrackerError):
    error_code = "user_approval_required"


class DuplicateApproach(AttemptTrackerError):
    error_code = "duplicate_approach"

    def __init__(self, prior: "AttemptSummary") -> None:
        super().__init__("An identical approach has already been attempted.")
        self.prior = prior


class RetryAuthorizationRequired(AttemptTrackerError):
    error_code = "retry_reason_required"


class AttemptInProgress(AttemptTrackerError):
    error_code = "attempt_in_progress"


class PolicyRejectionStopsTask(AttemptTrackerError):
    error_code = "policy_rejected"


class AttemptNotFound(AttemptTrackerError):
    error_code = "attempt_not_found"


class AttemptAlreadyFinalized(AttemptTrackerError):
    error_code = "attempt_already_finalized"


class StaleAttemptHandle(AttemptTrackerError):
    error_code = "stale_attempt_handle"


@dataclass(frozen=True, init=False)
class AttemptHandle:
    attempt_id: str
    task_id: str
    attempt_no: int
    approach_key: str
    lease_generation: int
    started_at: str

    def __init__(self, *args, **kwargs) -> None:
        raise TypeError("AttemptHandle values are issued only by AdultAttemptTracker.")

    @classmethod
    def _issue(
        cls, attempt_id: str, task_id: str, attempt_no: int,
        approach_key: str, lease_generation: int, started_at: str,
    ) -> "AttemptHandle":
        instance = object.__new__(cls)
        for name, value in (
            ("attempt_id", attempt_id), ("task_id", task_id),
            ("attempt_no", attempt_no), ("approach_key", approach_key),
            ("lease_generation", lease_generation), ("started_at", started_at),
        ):
            object.__setattr__(instance, name, value)
        return instance


@dataclass(frozen=True)
class AttemptSummary:
    attempt_id: str
    task_id: str
    step_id: str
    attempt_no: int
    approach_key: str
    route_family_key: str
    task_type: str
    adapter_id: str
    adapter_version: str
    provider_id: str | None
    model_id: str | None
    model_version: str | None
    tool_id: str | None
    tool_version: str | None
    status: str
    failure_class: str | None
    retryable: bool
    retry_reason: str | None
    error_code: str | None
    sanitized_error: str | None
    output_artifact_id: str | None
    usage: dict[str, int]
    cost_microusd: int | None
    started_at: str
    finished_at: str | None
    elapsed_ms: int | None


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    task_id: str
    attempt_no: int
    approach_key: str
    status: str
    failure_class: str | None
    retryable: bool
    output_ref: str | None
    error_code: str | None
    sanitized_error: str | None
    usage: dict[str, int]
    cost_microusd: int | None
    started_at: str
    finished_at: str
    elapsed_ms: int


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AttemptValidationError("Attempt clock must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise AttemptTrackerError("Stored attempt timestamp is invalid.") from exc
    if parsed.utcoffset() is None:
        raise AttemptTrackerError("Stored attempt timestamp is invalid.")
    return parsed.astimezone(timezone.utc)


def _validate_identifier(name: str, value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not _ATTEMPT_ID_RE.fullmatch(value):
        raise AttemptValidationError(f"{name} must be a bounded opaque identifier.")


def _validate_fingerprint(name: str, value: object) -> None:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise AttemptValidationError(f"{name} must be a normalized keyed fingerprint.")


def _safe_text(name: str, value: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise AttemptValidationError(f"{name} must be text.")
    value = value.strip()
    if not value and not required:
        return None
    if not value or not _SAFE_TEXT_RE.fullmatch(value) or _UNSAFE_TEXT_RE.search(value):
        raise AttemptValidationError(f"{name} contains unsupported or sensitive text.")
    return value


def _validate_usage(usage: dict | None) -> dict[str, int]:
    if usage is None:
        return {}
    if not isinstance(usage, dict) or len(usage) > len(_USAGE_KEYS):
        raise AttemptValidationError("usage must be a bounded object.")
    result: dict[str, int] = {}
    for key, value in usage.items():
        if key not in _USAGE_KEYS:
            raise AttemptValidationError("usage contains an unsupported field.")
        if (
            not isinstance(value, int) or isinstance(value, bool)
            or value < 0 or value > _MAX_USAGE_VALUE
        ):
            raise AttemptValidationError("usage values must be bounded non-negative integers.")
        result[key] = value
    return result


def _validate_cost(cost_microusd: int | None) -> int | None:
    if cost_microusd is None:
        return None
    if (
        not isinstance(cost_microusd, int) or isinstance(cost_microusd, bool)
        or cost_microusd < 0 or cost_microusd > 10**12
    ):
        raise AttemptValidationError("cost_microusd must be a bounded non-negative integer.")
    return cost_microusd


def _classify_error(error_code: str) -> tuple[str, str]:
    if error_code in _TRANSIENT_CODES:
        return "transient", "timed_out" if error_code in _TIMEOUT_CODES else "failed"
    if error_code in _CAPABILITY_CODES:
        return "capability_mismatch", "failed"
    if error_code in _PERMANENT_CODES:
        return "permanent_request", "failed"
    if error_code in _POLICY_CODES:
        return "policy_rejection", "rejected"
    if error_code in _BUDGET_CODES:
        return "budget_exhaustion", "failed"
    return "unknown", "failed"


def _canonical_hash(value: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            value, separators=(",", ":"), sort_keys=True, ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AttemptValidationError("Approach metadata is not canonical JSON.") from exc
    return hashlib.sha256(encoded).hexdigest()


class AdultAttemptTracker:
    """Persists attempt state and retry audit in the TaskStore database."""

    def __init__(
        self,
        store: AdultTaskStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        route_validator: Callable | None = None,
    ) -> None:
        if not isinstance(store, AdultTaskStore):
            raise TypeError("store must be an AdultTaskStore.")
        if not callable(clock):
            raise TypeError("clock must be callable.")
        if route_validator is not None and not callable(route_validator):
            raise TypeError("route_validator must be callable.")
        self._store = store
        self._clock = clock
        self._route_validator = route_validator

    def _now(self) -> str:
        return _utc_text(self._clock())

    @staticmethod
    def approach_keys(
        task_type: str, route: RouteCandidate,
        input_fingerprint: str, config_fingerprint: str,
    ) -> tuple[str, str]:
        if not isinstance(route, RouteCandidate):
            raise AttemptValidationError("route must be a validated RouteCandidate.")
        if task_type != route.task_type:
            raise AttemptValidationError("Route task type does not match the task.")
        _validate_fingerprint("input_fingerprint", input_fingerprint)
        _validate_fingerprint("config_fingerprint", config_fingerprint)
        material = {
            "task_type": task_type,
            "adapter_id": route.adapter_id,
            "adapter_version": route.adapter_version,
            "provider_id": route.provider_id or "",
            "model_id": route.model_id or "",
            "model_version": route.model_version or "",
            "tool_id": route.tool_id or "",
            "tool_version": route.tool_version or "",
            "config_fingerprint": config_fingerprint,
            "input_fingerprint": input_fingerprint,
            "environment_fingerprint": route.capability_version,
        }
        family = {
            "task_type": task_type,
            "adapter_id": route.adapter_id,
            "provider_id": route.provider_id or "",
            "model_id": route.model_id or "",
            "tool_id": route.tool_id or "",
        }
        return _canonical_hash(material), _canonical_hash(family)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(
        db: sqlite3.Connection, *, task_id: str, attempt_id: str | None,
        event_type: str, approach_key: str, metadata: dict, created_at: str,
    ) -> None:
        allowed_keys = {
            "attempt_no", "status", "failure_class", "error_code",
            "retryable", "retry_reason", "lease_generation",
        }
        if set(metadata) - allowed_keys:
            raise AttemptValidationError("Attempt audit metadata contains an unsupported field.")
        encoded = json.dumps(
            metadata, separators=(",", ":"), sort_keys=True, allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 2048:
            raise AttemptValidationError("Attempt audit metadata is too large.")
        db.execute(
            """INSERT INTO adult_attempt_events(
                event_id,task_id,attempt_id,event_type,approach_key,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, task_id, attempt_id, event_type, approach_key, encoded, created_at),
        )

    @staticmethod
    def _summary(row: sqlite3.Row) -> AttemptSummary:
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AttemptTrackerError("Stored attempt usage metadata is invalid.") from exc
        if not isinstance(usage, dict):
            raise AttemptTrackerError("Stored attempt usage metadata is invalid.")
        return AttemptSummary(
            attempt_id=row["attempt_id"], task_id=row["task_id"],
            step_id=row["step_id"], attempt_no=row["attempt_no"],
            approach_key=row["approach_key"], route_family_key=row["route_family_key"],
            task_type=row["task_type"], adapter_id=row["adapter_id"],
            adapter_version=row["adapter_version"], provider_id=row["provider_id"],
            model_id=row["model_id"], model_version=row["model_version"],
            tool_id=row["tool_id"], tool_version=row["tool_version"],
            status=row["status"], failure_class=row["failure_class"],
            retryable=bool(row["retryable"]), retry_reason=row["retry_reason"],
            error_code=row["error_code"], sanitized_error=row["sanitized_error"],
            output_artifact_id=row["output_artifact_id"], usage=usage,
            cost_microusd=row["cost_microusd"], started_at=row["started_at"],
            finished_at=row["finished_at"], elapsed_ms=row["elapsed_ms"],
        )

    def begin(
        self, task_id: str, step_id: str, route: RouteCandidate,
        input_fingerprint: str, config_fingerprint: str,
        *, retry_reason: str | None = None, lease: TaskLease | None = None,
    ) -> AttemptHandle:
        _validate_identifier("task_id", task_id)
        _validate_identifier("step_id", step_id)
        if not isinstance(route, RouteCandidate):
            raise AttemptValidationError("route must be a validated RouteCandidate.")
        if route.cost_class not in {"local", "free_verified"}:
            raise AttemptNotAvailable("Only local or explicitly verified free routes may be attempted.")
        if route.cost_class == "free_verified" and route.provider_id is None:
            raise AttemptValidationError("A free_verified route requires a provider identifier.")
        safe_retry_reason = _safe_text("retry_reason", retry_reason, required=True) if retry_reason is not None else None

        now = self._now()
        attempt_id = uuid.uuid4().hex
        duplicate: AttemptSummary | None = None
        issued: AttemptHandle | None = None

        with self._transaction() as db:
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            if lease is not None:
                # Bind a caller-owned execution to its fenced lease, atomically
                # with attempt creation. Legacy direct callers remain unchanged.
                self._store._require_current_lease(task, task_id, lease, now)
            if task["status"] in {"completed", "failed", "cancelled"}:
                raise AttemptNotAvailable("Terminal tasks cannot start new attempts.")
            if task["status"] not in ACTIVE_STATUSES:
                raise AttemptNotAvailable("An active task lease is required before an attempt.")
            if task["cancel_requested"]:
                raise AttemptNotAvailable("A cancellation request prevents new attempts.")
            if (
                task["lease_owner"] is None or task["lease_expires_at"] is None
                or task["lease_expires_at"] <= now or task["lease_generation"] <= 0
            ):
                raise LeaseLost("A current unexpired task lease is required.")
            if self._route_validator is not None and self._route_validator(db, route, now) is not True:
                raise AttemptNotAvailable("Selected route is no longer verified and enabled.")
            if task["deadline_at"] <= now:
                raise AttemptBudgetExhausted("Task wall-time budget is exhausted.")
            if task["failed_attempts_since_approval"] >= 5:
                raise AttemptApprovalRequired("Five failed attempts require explicit user approval.")
            policy_rejection = db.execute(
                """SELECT 1 FROM adult_attempts
                   WHERE task_id=? AND status='rejected'
                     AND failure_class='policy_rejection' LIMIT 1""",
                (task_id,),
            ).fetchone()
            if policy_rejection is not None:
                raise PolicyRejectionStopsTask(
                    "An upstream policy rejection is terminal for this task and cannot be routed around."
                )
            if task["total_attempts"] >= task["max_total_attempts"]:
                raise AttemptBudgetExhausted("Task attempt budget is exhausted.")

            step = db.execute(
                "SELECT task_id,status FROM adult_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            if step is None or step["task_id"] != task_id:
                raise AttemptValidationError("step_id must belong to this task.")
            if step["status"] not in {"pending", "running"}:
                raise AttemptNotAvailable("Attempts require a pending or running task step.")
            if task["current_step_id"] != step_id:
                raise AttemptValidationError("step_id must be the current task step.")

            approach_key, route_family_key = self.approach_keys(
                task["task_type"], route, input_fingerprint, config_fingerprint,
            )
            prior = db.execute(
                """SELECT * FROM adult_attempts
                   WHERE task_id=? AND approach_key=? ORDER BY attempt_no DESC LIMIT 1""",
                (task_id, approach_key),
            ).fetchone()
            if prior is not None:
                duplicate = self._summary(prior)
                self._event(
                    db, task_id=task_id, attempt_id=None, event_type="approach_skipped",
                    approach_key=approach_key,
                    metadata={"status": prior["status"], "attempt_no": prior["attempt_no"]},
                    created_at=now,
                )
            else:
                family_prior = db.execute(
                    """SELECT attempt_no FROM adult_attempts
                       WHERE task_id=? AND route_family_key=? ORDER BY attempt_no DESC LIMIT 1""",
                    (task_id, route_family_key),
                ).fetchone()
                if family_prior is not None and safe_retry_reason is None:
                    raise RetryAuthorizationRequired(
                        "A material-change retry reason is required for this route family."
                    )
                running = db.execute(
                    "SELECT 1 FROM adult_attempts WHERE task_id=? AND status='running' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if running is not None:
                    raise AttemptInProgress("Another attempt is already running for this task.")

                is_provider_call = route.cost_class == "free_verified"
                if is_provider_call and task["provider_calls"] >= task["max_provider_calls"]:
                    raise AttemptBudgetExhausted("Task provider-call budget is exhausted.")

                attempt_no = task["total_attempts"] + 1
                # The task counters and the attempt start are one durable transaction.
                cursor = db.execute(
                    """UPDATE adult_tasks
                       SET total_attempts=total_attempts+1,
                           provider_calls=provider_calls+?,
                           selected_adapter=?,selected_provider=?,selected_model=?,selected_tool=?,
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND total_attempts=? AND provider_calls=?
                         AND state_version=? AND lease_generation=? AND lease_owner=?
                         AND lease_expires_at>? AND status IN ('analyzing','running','evaluating')
                         AND cancel_requested=0""",
                    (
                        int(is_provider_call), route.adapter_id, route.provider_id,
                        route.model_id, route.tool_id, now, task_id,
                        task["total_attempts"], task["provider_calls"], task["state_version"],
                        task["lease_generation"], task["lease_owner"], now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AttemptNotAvailable("Task changed before attempt dispatch.")

                db.execute(
                    """INSERT INTO adult_attempts(
                        attempt_id,task_id,step_id,attempt_no,approach_key,route_family_key,
                        task_type,adapter_id,adapter_version,provider_id,model_id,model_version,
                        tool_id,tool_version,input_fingerprint,config_fingerprint,
                        environment_fingerprint,lease_generation,status,retryable,retry_reason,
                        usage_json,started_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'running',0,?,'{}',?)""",
                    (
                        attempt_id, task_id, step_id, attempt_no, approach_key, route_family_key,
                        task["task_type"], route.adapter_id, route.adapter_version,
                        route.provider_id, route.model_id, route.model_version,
                        route.tool_id, route.tool_version, input_fingerprint,
                        config_fingerprint, route.capability_version, task["lease_generation"],
                        safe_retry_reason, now,
                    ),
                )
                if family_prior is not None and safe_retry_reason is not None:
                    self._event(
                        db, task_id=task_id, attempt_id=attempt_id,
                        event_type="retry_authorized", approach_key=approach_key,
                        metadata={
                            "attempt_no": attempt_no,
                            "retry_reason": safe_retry_reason,
                        },
                        created_at=now,
                    )
                self._event(
                    db, task_id=task_id, attempt_id=attempt_id,
                    event_type="attempt_started", approach_key=approach_key,
                    metadata={
                        "attempt_no": attempt_no,
                        "lease_generation": task["lease_generation"],
                        "status": "running",
                    },
                    created_at=now,
                )
                issued = AttemptHandle._issue(
                    attempt_id, task_id, attempt_no, approach_key,
                    task["lease_generation"], now,
                )

        if duplicate is not None:
            raise DuplicateApproach(duplicate)
        if issued is None:
            raise AttemptTrackerError("Attempt start did not produce a handle.")
        return issued

    def _load_handle_row(
        self, db: sqlite3.Connection, handle: AttemptHandle, now: str
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if not isinstance(handle, AttemptHandle):
            raise StaleAttemptHandle("A tracker-issued AttemptHandle is required.")
        _validate_identifier("attempt_id", handle.attempt_id)
        _validate_identifier("task_id", handle.task_id)
        attempt = db.execute(
            "SELECT * FROM adult_attempts WHERE attempt_id=? AND task_id=?",
            (handle.attempt_id, handle.task_id),
        ).fetchone()
        if attempt is None:
            raise AttemptNotFound("Attempt does not exist.")
        if attempt["status"] != "running":
            raise AttemptAlreadyFinalized("Attempt already has a terminal outcome.")
        if (
            not isinstance(handle.attempt_no, int) or isinstance(handle.attempt_no, bool)
            or not 1 <= handle.attempt_no <= 20
            or not isinstance(handle.lease_generation, int)
            or isinstance(handle.lease_generation, bool) or handle.lease_generation < 1
            or not isinstance(handle.approach_key, str)
            or not _HASH_RE.fullmatch(handle.approach_key)
            or not isinstance(handle.started_at, str)
            or handle.attempt_no != attempt["attempt_no"]
            or handle.approach_key != attempt["approach_key"]
            or handle.started_at != attempt["started_at"]
            or handle.lease_generation != attempt["lease_generation"]
        ):
            raise StaleAttemptHandle("Attempt handle fields do not match the stored attempt.")
        try:
            _parse_utc(handle.started_at)
        except AttemptTrackerError as exc:
            raise StaleAttemptHandle("Attempt handle timestamp is invalid.") from exc
        task = db.execute(
            "SELECT * FROM adult_tasks WHERE task_id=?", (handle.task_id,)
        ).fetchone()
        if task is None:
            raise TaskNotFound("Task does not exist.")
        if (
            attempt["lease_generation"] != handle.lease_generation
            or task["lease_generation"] != handle.lease_generation
            or task["status"] not in ACTIVE_STATUSES
            or task["lease_owner"] is None
            or task["lease_expires_at"] is None
            or task["lease_expires_at"] <= now
        ):
            raise StaleAttemptHandle("The worker lease changed or expired before attempt completion.")
        return attempt, task

    @staticmethod
    def _result(
        attempt: sqlite3.Row, *, status: str, failure_class: str | None,
        retryable: bool, output_ref: str | None, error_code: str | None,
        sanitized_error: str | None, usage: dict[str, int],
        cost_microusd: int | None, finished_at: str, elapsed_ms: int,
    ) -> AttemptResult:
        return AttemptResult(
            attempt_id=attempt["attempt_id"], task_id=attempt["task_id"],
            attempt_no=attempt["attempt_no"], approach_key=attempt["approach_key"],
            status=status, failure_class=failure_class, retryable=retryable,
            output_ref=output_ref, error_code=error_code,
            sanitized_error=sanitized_error, usage=usage,
            cost_microusd=cost_microusd, started_at=attempt["started_at"],
            finished_at=finished_at, elapsed_ms=elapsed_ms,
        )

    def succeed(
        self, handle: AttemptHandle, output_ref: str | None,
        usage: dict | None, cost_microusd: int | None,
    ) -> AttemptResult:
        if output_ref is not None:
            _validate_identifier("output_ref", output_ref)
        safe_usage = _validate_usage(usage)
        safe_cost = _validate_cost(cost_microusd)
        now = self._now()
        result: AttemptResult | None = None
        with self._transaction() as db:
            attempt, task = self._load_handle_row(db, handle, now)
            additional_cost = safe_cost or 0
            new_spent_cost = task["spent_cost_microusd"] + additional_cost
            if new_spent_cost > task["max_cost_microusd"]:
                raise AttemptBudgetExhausted("Task cost budget is exhausted.")
            started = _parse_utc(attempt["started_at"])
            elapsed_ms = max(0, int((_parse_utc(now) - started).total_seconds() * 1000))
            encoded_usage = json.dumps(
                safe_usage, separators=(",", ":"), sort_keys=True, allow_nan=False,
            )
            cursor = db.execute(
                """UPDATE adult_attempts SET status='succeeded',failure_class=NULL,
                          retryable=0,output_artifact_id=?,usage_json=?,cost_microusd=?,
                          finished_at=?,elapsed_ms=?
                   WHERE attempt_id=? AND status='running'""",
                (
                    output_ref, encoded_usage, safe_cost, now, elapsed_ms,
                    handle.attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AttemptAlreadyFinalized("Attempt could not be finalized.")
            task_cursor = db.execute(
                """UPDATE adult_tasks
                   SET spent_cost_microusd=?,last_error_code=NULL,
                       state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND state_version=? AND lease_generation=?
                     AND lease_expires_at>? AND status IN ('analyzing','running','evaluating')
                     AND cancel_requested=0""",
                (
                    new_spent_cost, now, handle.task_id, task["state_version"],
                    handle.lease_generation, now,
                ),
            )
            if task_cursor.rowcount != 1:
                raise StaleAttemptHandle("Task changed before success was committed.")
            self._event(
                db, task_id=handle.task_id, attempt_id=handle.attempt_id,
                event_type="attempt_succeeded", approach_key=handle.approach_key,
                metadata={"attempt_no": handle.attempt_no, "status": "succeeded"},
                created_at=now,
            )
            result = self._result(
                attempt, status="succeeded", failure_class=None, retryable=False,
                output_ref=output_ref, error_code=None, sanitized_error=None,
                usage=safe_usage, cost_microusd=safe_cost,
                finished_at=now, elapsed_ms=elapsed_ms,
            )
        if result is None:
            raise AttemptTrackerError("Attempt success did not produce a result.")
        return result

    def fail(
        self, handle: AttemptHandle, error_code: str,
        sanitized_error: str, retryable: bool,
    ) -> AttemptResult:
        if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
            raise AttemptValidationError("error_code must be a normalized uppercase code.")
        if not isinstance(retryable, bool):
            raise AttemptValidationError("retryable must be boolean.")
        safe_error = _safe_text("sanitized_error", sanitized_error, required=True)
        failure_class, status = _classify_error(error_code)
        if failure_class in {"capability_mismatch", "permanent_request", "policy_rejection", "budget_exhaustion"} and retryable:
            raise AttemptValidationError("This failure class cannot be marked retryable.")
        now = self._now()
        result: AttemptResult | None = None
        with self._transaction() as db:
            attempt, task = self._load_handle_row(db, handle, now)
            started = _parse_utc(attempt["started_at"])
            elapsed_ms = max(0, int((_parse_utc(now) - started).total_seconds() * 1000))
            encoded_usage = attempt["usage_json"]
            cursor = db.execute(
                """UPDATE adult_attempts SET status=?,failure_class=?,retryable=?,
                          error_code=?,sanitized_error=?,finished_at=?,elapsed_ms=?
                   WHERE attempt_id=? AND status='running'""",
                (
                    status, failure_class, int(retryable), error_code,
                    safe_error, now, elapsed_ms, handle.attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise AttemptAlreadyFinalized("Attempt could not be finalized.")
            counts_as_failure = status in {"failed", "timed_out"}
            task_cursor = db.execute(
                """UPDATE adult_tasks
                   SET failed_attempts_since_approval=CASE
                           WHEN ?=1 AND failed_attempts_since_approval < 5
                           THEN failed_attempts_since_approval+1
                           ELSE failed_attempts_since_approval END,
                       last_error_code=?,state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND state_version=? AND lease_generation=?
                     AND lease_expires_at>? AND status IN ('analyzing','running','evaluating')""",
                (
                    int(counts_as_failure), error_code.lower(), now, handle.task_id,
                    task["state_version"], handle.lease_generation, now,
                ),
            )
            if task_cursor.rowcount != 1:
                raise StaleAttemptHandle("Task changed before failure was committed.")
            event_type = {
                "rejected": "attempt_rejected",
                "timed_out": "attempt_timed_out",
                "failed": "attempt_failed",
            }[status]
            self._event(
                db, task_id=handle.task_id, attempt_id=handle.attempt_id,
                event_type=event_type, approach_key=handle.approach_key,
                metadata={
                    "attempt_no": handle.attempt_no, "status": status,
                    "failure_class": failure_class, "error_code": error_code,
                    "retryable": retryable,
                },
                created_at=now,
            )
            usage = json.loads(encoded_usage)
            result = self._result(
                attempt, status=status, failure_class=failure_class,
                retryable=retryable, output_ref=None, error_code=error_code,
                sanitized_error=safe_error, usage=usage,
                cost_microusd=attempt["cost_microusd"],
                finished_at=now, elapsed_ms=elapsed_ms,
            )
        if result is None:
            raise AttemptTrackerError("Attempt failure did not produce a result.")
        return result

    def list_summaries(self, task_id: str) -> tuple[AttemptSummary, ...]:
        """Read this task's bounded attempt metadata, never input/output bytes."""
        _validate_identifier("task_id", task_id)
        with self._store._connect() as db:
            rows = db.execute("SELECT * FROM adult_attempts WHERE task_id=? ORDER BY attempt_no LIMIT 20",
                              (task_id,)).fetchall()
            return tuple(self._summary(row) for row in rows)

    def list_skipped(self, task_id: str, limit: int = 20) -> tuple[dict, ...]:
        """Latest bounded duplicate audit, separate from actual provider attempts.

        Do not return audit metadata, approach keys, error summaries or content.
        Join to the original attempt to avoid trusting duplicate-event metadata.
        """
        _validate_identifier("task_id", task_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise AttemptValidationError("Invalid skipped history limit.")
        db = self._store._connect()
        try:
            rows = db.execute(
                """SELECT e.event_id,e.created_at,a.attempt_id,a.attempt_no,
                          a.adapter_id,a.adapter_version,a.provider_id,a.model_id,
                          a.tool_id,a.status
                   FROM adult_attempt_events e
                   JOIN adult_attempts a ON a.task_id=e.task_id
                       AND a.approach_key=e.approach_key
                   WHERE e.task_id=? AND e.event_type='approach_skipped'
                   ORDER BY e.created_at DESC,e.event_id DESC LIMIT ?""",
                (task_id, limit),
            ).fetchall()
            return tuple({**dict(row), "skipped_as_duplicate": True} for row in reversed(rows))
        finally:
            db.close()

    def find_prior_approach(
        self, task_id: str, approach_key: str
    ) -> AttemptSummary | None:
        _validate_identifier("task_id", task_id)
        if not isinstance(approach_key, str) or not _HASH_RE.fullmatch(approach_key):
            raise AttemptValidationError("approach_key must be a SHA-256 digest.")
        db = self._store._connect()
        try:
            row = db.execute(
                """SELECT * FROM adult_attempts
                   WHERE task_id=? AND approach_key=? ORDER BY attempt_no DESC LIMIT 1""",
                (task_id, approach_key),
            ).fetchone()
            return self._summary(row) if row is not None else None
        finally:
            db.close()

    def failures_since_approval(self, task_id: str) -> int:
        _validate_identifier("task_id", task_id)
        db = self._store._connect()
        try:
            row = db.execute(
                "SELECT failed_attempts_since_approval FROM adult_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            raise TaskNotFound("Task does not exist.")
        return int(row["failed_attempts_since_approval"])

    def total_attempts(self, task_id: str) -> int:
        _validate_identifier("task_id", task_id)
        db = self._store._connect()
        try:
            row = db.execute(
                "SELECT total_attempts FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            db.close()
        if row is None:
            raise TaskNotFound("Task does not exist.")
        return int(row["total_attempts"])

    def recover_uncertain_attempts(self) -> int:
        """Finalize attempts whose task lease was already recovered or released."""
        now = self._now()
        recovered = 0
        with self._transaction() as db:
            rows = db.execute(
                """SELECT a.*,t.status AS task_status,t.state_version AS task_state_version,
                          t.lease_generation AS current_lease_generation,
                          t.lease_owner AS current_lease_owner,t.lease_expires_at AS current_lease_expires_at
                   FROM adult_attempts a
                   JOIN adult_tasks t ON t.task_id=a.task_id
                   WHERE a.status='running'
                     AND (t.lease_generation>a.lease_generation
                          OR t.lease_owner IS NULL
                          OR t.status NOT IN ('analyzing','running','evaluating'))"""
            ).fetchall()
            for attempt in rows:
                started = _parse_utc(attempt["started_at"])
                elapsed_ms = max(0, int((_parse_utc(now) - started).total_seconds() * 1000))
                cursor = db.execute(
                    """UPDATE adult_attempts SET status='unknown_outcome',
                              failure_class='unknown',retryable=1,
                              error_code='PROVIDER_OUTCOME_UNKNOWN',
                              sanitized_error='Provider outcome could not be established after worker lease recovery.',
                              finished_at=?,elapsed_ms=?
                       WHERE attempt_id=? AND status='running'""",
                    (now, elapsed_ms, attempt["attempt_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                task_cursor = db.execute(
                    """UPDATE adult_tasks
                       SET failed_attempts_since_approval=CASE
                               WHEN failed_attempts_since_approval < 5
                               THEN failed_attempts_since_approval+1 ELSE 5 END,
                           last_error_code='provider_outcome_unknown',
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND state_version=?""",
                    (now, attempt["task_id"], attempt["task_state_version"]),
                )
                if task_cursor.rowcount != 1:
                    raise AttemptTrackerError("Task changed during uncertain-outcome recovery.")
                self._event(
                    db, task_id=attempt["task_id"], attempt_id=attempt["attempt_id"],
                    event_type="attempt_unknown_outcome",
                    approach_key=attempt["approach_key"],
                    metadata={
                        "attempt_no": attempt["attempt_no"], "status": "unknown_outcome",
                        "failure_class": "unknown",
                        "error_code": "PROVIDER_OUTCOME_UNKNOWN",
                        "retryable": True,
                    },
                    created_at=now,
                )
                recovered += 1
        return recovered
