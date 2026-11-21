"""Adult Agent operational SQLite schema and migration foundation.

This module deliberately contains no provider, network, payload, or inference code.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Literal

from agent.adult.models import (
    AdultModelValidationError, AdultTaskRequest, AdultTaskSnapshot,
    Checkpoint, CheckpointSnapshot, TaskBudget, TaskEvent, TaskPage, StepRecord,
    TASK_STATUSES, TaskLease, ACTIVE_STATUSES, TERMINAL_STATUSES, EVENT_TYPES,
    CHECKPOINT_REASONS,
)

SCHEMA_VERSION: Final = 1
SCHEMA_NAME: Final = "adult_operational_store_v1"
DEFAULT_DB_PATH: Final = Path(".data") / "adult.sqlite3"
MIN_SQLITE_VERSION: Final = (3, 38, 0)
_BUSY_TIMEOUT_MS: Final = 10_000

class AdultTaskStoreError(Exception):
    """Base class for stable Adult task-store errors."""

class TaskStoreSchemaError(AdultTaskStoreError):
    """The operational database schema is missing or inconsistent."""

class TaskNotFound(AdultTaskStoreError):
    error_code = "task_not_found"

class InvalidTaskBudget(AdultTaskStoreError):
    error_code = "invalid_task_budget"

class PayloadRetentionUnavailable(AdultTaskStoreError):
    error_code = "payload_retention_unavailable"

class InvalidCursor(AdultTaskStoreError):
    error_code = "invalid_cursor"

class InvalidTaskTransition(AdultTaskStoreError):
    error_code = "invalid_task_transition"

class StaleTaskVersion(AdultTaskStoreError):
    error_code = "stale_task_version"

class TaskAlreadyActive(AdultTaskStoreError):
    error_code = "task_already_active"

class LeaseNotAvailable(AdultTaskStoreError):
    error_code = "lease_not_available"

class LeaseLost(AdultTaskStoreError):
    error_code = "lease_lost"

class InvalidCheckpoint(AdultTaskStoreError):
    error_code = "invalid_checkpoint"

class TaskBudgetExhausted(AdultTaskStoreError):
    error_code = "task_budget_exhausted"

class TaskTerminal(AdultTaskStoreError):
    error_code = "task_terminal"

class UserApprovalRequired(AdultTaskStoreError):
    error_code = "user_approval_required"

class TaskCancelled(AdultTaskStoreError):
    error_code = "task_cancelled"

_SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE adult_schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE adult_tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL CHECK(task_type IN (
            'text.analyze','text.generate','text.rewrite',
            'image.analyze','image.generate','image.edit')),
        status TEXT NOT NULL CHECK(status IN (
            'queued','analyzing','running','evaluating','checkpointed',
            'waiting_user','needs_input','paused','completed','failed','cancelled')),
        input_fingerprint TEXT NOT NULL,
        instruction_fingerprint TEXT,
        request_options_json TEXT NOT NULL DEFAULT '{}'
            CHECK(json_valid(request_options_json) AND json_type(request_options_json)='object'
              AND length(CAST(request_options_json AS BLOB)) <= 8192),
        payload_id TEXT,
        retention_policy TEXT NOT NULL CHECK(retention_policy IN ('ephemeral','encrypted')),
        current_step_id TEXT,
        selected_adapter TEXT,
        selected_provider TEXT,
        selected_model TEXT,
        selected_tool TEXT,
        latest_result_artifact_id TEXT,
        failed_attempts_since_approval INTEGER NOT NULL DEFAULT 0 CHECK(failed_attempts_since_approval >= 0),
        total_attempts INTEGER NOT NULL DEFAULT 0 CHECK(total_attempts >= 0),
        provider_calls INTEGER NOT NULL DEFAULT 0 CHECK(provider_calls >= 0),
        approvals_granted INTEGER NOT NULL DEFAULT 0 CHECK(approvals_granted >= 0),
        max_total_attempts INTEGER NOT NULL CHECK(max_total_attempts BETWEEN 1 AND 20),
        max_wall_seconds INTEGER NOT NULL CHECK(max_wall_seconds BETWEEN 1 AND 600),
        max_provider_calls INTEGER NOT NULL CHECK(max_provider_calls BETWEEN 0 AND 20),
        max_cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK(max_cost_microusd >= 0),
        spent_cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK(spent_cost_microusd >= 0),
        deadline_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_expires_at TEXT,
        lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
        state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        last_error_code TEXT,
        user_action_required INTEGER NOT NULL DEFAULT 0 CHECK(user_action_required IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((max_cost_microusd = 0 AND spent_cost_microusd = 0) OR
              (max_cost_microusd > 0 AND spent_cost_microusd <= max_cost_microusd)),
        CHECK((status IN ('analyzing','running','evaluating')
              AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_generation > 0)
           OR (status NOT IN ('analyzing','running','evaluating')
              AND lease_owner IS NULL AND lease_expires_at IS NULL)),
        CHECK((status IN ('waiting_user','needs_input','paused') AND user_action_required = 1)
           OR (status NOT IN ('waiting_user','needs_input','paused') AND user_action_required = 0)),
        CHECK(failed_attempts_since_approval BETWEEN 0 AND 5),
        CHECK(total_attempts <= max_total_attempts),
        CHECK(provider_calls <= max_provider_calls),
        CHECK(status <> 'waiting_user' OR
              (failed_attempts_since_approval = 5 AND total_attempts >= 5)),
        CHECK(status <> 'cancelled' OR cancel_requested = 1),
        CHECK(cancel_requested = 0 OR status IN ('analyzing','running','evaluating','cancelled'))
    )""",
    """CREATE TABLE adult_steps (
        step_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
        sequence_no INTEGER NOT NULL CHECK(sequence_no >= 0),
        operation TEXT NOT NULL CHECK(operation IN (
            'intake','text.analyze','text.generate','text.rewrite',
            'image.analyze','image.generate','image.edit','evaluate')),
        status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','skipped')),
        adapter_id TEXT, provider_id TEXT, model_id TEXT, tool_id TEXT,
        input_payload_id TEXT, output_artifact_id TEXT, config_fingerprint TEXT,
        started_at TEXT, completed_at TEXT,
        UNIQUE(task_id, sequence_no)
    )""",
    """CREATE TABLE adult_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        reason TEXT NOT NULL CHECK(reason IN (
            'initial','step_completed','approval_required','manual_yield',
            'lease_recovered','needs_input','paused','cancelled')),
        schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
        snapshot_json TEXT NOT NULL
            CHECK(json_valid(snapshot_json) AND json_type(snapshot_json)='object'
              AND length(CAST(snapshot_json AS BLOB)) <= 65536),
        failed_attempts_since_approval INTEGER NOT NULL CHECK(failed_attempts_since_approval >= 0),
        user_action_required INTEGER NOT NULL DEFAULT 0 CHECK(user_action_required IN (0,1)),
        created_at TEXT NOT NULL,
        UNIQUE(task_id, revision)
    )""",
    """CREATE TABLE adult_task_events (
        event_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'task_created','task_claimed','lease_renewed','lease_released',
            'step_created','step_transition','state_transition','checkpoint_saved',
            'waiting_user','task_resumed','cancel_requested','task_cancelled',
            'lease_expired_recovered','cancel_recovered','budget_exhausted')),
        from_status TEXT,
        to_status TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
            CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'
              AND length(CAST(metadata_json AS BLOB)) <= 8192),
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_adult_tasks_status_updated ON adult_tasks(status, updated_at DESC)",
    """CREATE UNIQUE INDEX idx_adult_single_active_task ON adult_tasks((1))
       WHERE status IN ('analyzing','running','evaluating')""",
    "CREATE INDEX idx_adult_steps_task_sequence ON adult_steps(task_id, sequence_no)",
    "CREATE INDEX idx_adult_checkpoints_task_revision ON adult_checkpoints(task_id, revision DESC)",
    "CREATE INDEX idx_adult_events_task_created ON adult_task_events(task_id, created_at)",
    """CREATE TRIGGER prevent_adult_tasks_delete BEFORE DELETE ON adult_tasks
       BEGIN SELECT RAISE(ABORT, 'adult task history is immutable'); END""",
    """CREATE TRIGGER prevent_adult_steps_delete BEFORE DELETE ON adult_steps
       BEGIN SELECT RAISE(ABORT, 'adult step history is immutable'); END""",
    """CREATE TRIGGER validate_adult_steps_update BEFORE UPDATE ON adult_steps
       WHEN NEW.step_id IS NOT OLD.step_id OR NEW.task_id IS NOT OLD.task_id
       OR NEW.sequence_no IS NOT OLD.sequence_no OR NEW.operation IS NOT OLD.operation
       OR (OLD.adapter_id IS NOT NULL AND NEW.adapter_id IS NOT OLD.adapter_id)
       OR (OLD.provider_id IS NOT NULL AND NEW.provider_id IS NOT OLD.provider_id)
       OR (OLD.model_id IS NOT NULL AND NEW.model_id IS NOT OLD.model_id)
       OR (OLD.tool_id IS NOT NULL AND NEW.tool_id IS NOT OLD.tool_id)
       OR (OLD.input_payload_id IS NOT NULL AND NEW.input_payload_id IS NOT OLD.input_payload_id)
       OR (OLD.output_artifact_id IS NOT NULL AND NEW.output_artifact_id IS NOT OLD.output_artifact_id)
       OR (OLD.config_fingerprint IS NOT NULL AND NEW.config_fingerprint IS NOT OLD.config_fingerprint)
       OR (OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at)
       OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
       OR NOT (NEW.status = OLD.status
         OR (OLD.status = 'pending' AND NEW.status IN ('running','skipped'))
         OR (OLD.status = 'running' AND NEW.status IN ('succeeded','failed')))
       BEGIN SELECT RAISE(ABORT, 'invalid adult step mutation'); END""",
    """CREATE TRIGGER prevent_adult_checkpoints_update BEFORE UPDATE ON adult_checkpoints
       BEGIN SELECT RAISE(ABORT, 'adult checkpoints are append-only'); END""",
    """CREATE TRIGGER prevent_adult_checkpoints_delete BEFORE DELETE ON adult_checkpoints
       BEGIN SELECT RAISE(ABORT, 'adult checkpoints are append-only'); END""",
    """CREATE TRIGGER prevent_adult_task_events_update BEFORE UPDATE ON adult_task_events
       BEGIN SELECT RAISE(ABORT, 'adult task events are append-only'); END""",
    """CREATE TRIGGER prevent_adult_task_events_delete BEFORE DELETE ON adult_task_events
       BEGIN SELECT RAISE(ABORT, 'adult task events are append-only'); END""",
)

LATEST_SCHEMA_VERSION: Final = 2
ATTEMPT_SCHEMA_VERSION: Final = 2
ATTEMPT_SCHEMA_NAME: Final = "adult_attempt_tracking_v2"

_ATTEMPT_SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE adult_attempts (
        attempt_id TEXT PRIMARY KEY CHECK(length(attempt_id) BETWEEN 16 AND 128),
        task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
        step_id TEXT NOT NULL REFERENCES adult_steps(step_id) ON DELETE RESTRICT,
        attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 20),
        approach_key TEXT NOT NULL CHECK(length(approach_key)=64 AND approach_key NOT GLOB '*[^0-9a-f]*'),
        route_family_key TEXT NOT NULL CHECK(length(route_family_key)=64 AND route_family_key NOT GLOB '*[^0-9a-f]*'),
        task_type TEXT NOT NULL CHECK(task_type IN (
            'text.analyze','text.generate','text.rewrite',
            'image.analyze','image.generate','image.edit')),
        adapter_id TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        provider_id TEXT,
        model_id TEXT,
        model_version TEXT,
        tool_id TEXT,
        tool_version TEXT,
        input_fingerprint TEXT NOT NULL,
        config_fingerprint TEXT NOT NULL,
        environment_fingerprint TEXT,
        lease_generation INTEGER NOT NULL CHECK(lease_generation > 0),
        status TEXT NOT NULL CHECK(status IN (
            'running','succeeded','failed','rejected','timed_out','unknown_outcome')),
        failure_class TEXT CHECK(failure_class IN (
            'transient','capability_mismatch','permanent_request',
            'policy_rejection','budget_exhaustion','unknown')),
        retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
        retry_reason TEXT CHECK(retry_reason IS NULL OR length(retry_reason) <= 512),
        error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
        sanitized_error TEXT CHECK(sanitized_error IS NULL OR length(sanitized_error) <= 512),
        output_fingerprint TEXT,
        output_artifact_id TEXT,
        usage_json TEXT NOT NULL DEFAULT '{}'
            CHECK(json_valid(usage_json) AND json_type(usage_json)='object'
              AND length(CAST(usage_json AS BLOB)) <= 2048),
        cost_microusd INTEGER CHECK(cost_microusd IS NULL OR cost_microusd >= 0),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        elapsed_ms INTEGER CHECK(elapsed_ms IS NULL OR elapsed_ms >= 0),
        UNIQUE(task_id,attempt_no),
        CHECK((status='running' AND finished_at IS NULL)
           OR (status<>'running' AND finished_at IS NOT NULL)),
        CHECK(status NOT IN ('succeeded','running') OR failure_class IS NULL),
        CHECK(status<>'rejected' OR (failure_class='policy_rejection' AND retryable=0))
    )""",
    """CREATE TABLE adult_attempt_events (
        event_id TEXT PRIMARY KEY CHECK(length(event_id) BETWEEN 16 AND 128),
        task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
        attempt_id TEXT REFERENCES adult_attempts(attempt_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'attempt_started','attempt_succeeded','attempt_failed',
            'attempt_rejected','attempt_timed_out','attempt_unknown_outcome',
            'approach_skipped','retry_authorized')),
        approach_key TEXT NOT NULL CHECK(length(approach_key)=64 AND approach_key NOT GLOB '*[^0-9a-f]*'),
        metadata_json TEXT NOT NULL DEFAULT '{}'
            CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'
              AND length(CAST(metadata_json AS BLOB)) <= 2048),
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_adult_attempts_task_no ON adult_attempts(task_id,attempt_no DESC)",
    "CREATE INDEX idx_adult_attempts_approach ON adult_attempts(task_id,approach_key,attempt_no DESC)",
    "CREATE UNIQUE INDEX idx_adult_attempts_one_running ON adult_attempts(task_id) WHERE status='running'",
    "CREATE INDEX idx_adult_attempt_events_task_created ON adult_attempt_events(task_id,created_at)",
    """CREATE TRIGGER validate_adult_attempt_update BEFORE UPDATE ON adult_attempts
       WHEN OLD.status <> 'running'
       OR NEW.attempt_id IS NOT OLD.attempt_id
       OR NEW.task_id IS NOT OLD.task_id
       OR NEW.step_id IS NOT OLD.step_id
       OR NEW.attempt_no IS NOT OLD.attempt_no
       OR NEW.approach_key IS NOT OLD.approach_key
       OR NEW.route_family_key IS NOT OLD.route_family_key
       OR NEW.task_type IS NOT OLD.task_type
       OR NEW.adapter_id IS NOT OLD.adapter_id
       OR NEW.adapter_version IS NOT OLD.adapter_version
       OR NEW.provider_id IS NOT OLD.provider_id
       OR NEW.model_id IS NOT OLD.model_id
       OR NEW.model_version IS NOT OLD.model_version
       OR NEW.tool_id IS NOT OLD.tool_id
       OR NEW.tool_version IS NOT OLD.tool_version
       OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
       OR NEW.config_fingerprint IS NOT OLD.config_fingerprint
       OR NEW.environment_fingerprint IS NOT OLD.environment_fingerprint
       OR NEW.lease_generation IS NOT OLD.lease_generation
       OR NEW.started_at IS NOT OLD.started_at
       OR NEW.retry_reason IS NOT OLD.retry_reason
       OR NEW.status NOT IN ('succeeded','failed','rejected','timed_out','unknown_outcome')
       OR NEW.finished_at IS NULL
       BEGIN SELECT RAISE(ABORT, 'invalid adult attempt mutation'); END""",
    """CREATE TRIGGER prevent_adult_attempts_delete BEFORE DELETE ON adult_attempts
       BEGIN SELECT RAISE(ABORT, 'adult attempt history is immutable'); END""",
    """CREATE TRIGGER prevent_adult_attempt_events_update BEFORE UPDATE ON adult_attempt_events
       BEGIN SELECT RAISE(ABORT, 'adult attempt events are append-only'); END""",
    """CREATE TRIGGER prevent_adult_attempt_events_delete BEFORE DELETE ON adult_attempt_events
       BEGIN SELECT RAISE(ABORT, 'adult attempt events are append-only'); END""",
)

_EXPECTED_TABLES_V1: Final = {
    "adult_schema_migrations", "adult_tasks", "adult_steps",
    "adult_checkpoints", "adult_task_events",
}
_EXPECTED_INDEXES_V1: Final = {
    "idx_adult_tasks_status_updated", "idx_adult_single_active_task",
    "idx_adult_steps_task_sequence", "idx_adult_checkpoints_task_revision",
    "idx_adult_events_task_created",
}
_EXPECTED_TRIGGERS_V1: Final = {
    "prevent_adult_tasks_delete", "prevent_adult_steps_delete",
    "validate_adult_steps_update", "prevent_adult_checkpoints_update",
    "prevent_adult_checkpoints_delete", "prevent_adult_task_events_update",
    "prevent_adult_task_events_delete",
}
_EXPECTED_TABLES_V2: Final = _EXPECTED_TABLES_V1 | {"adult_attempts", "adult_attempt_events"}
_EXPECTED_INDEXES_V2: Final = _EXPECTED_INDEXES_V1 | {
    "idx_adult_attempts_task_no", "idx_adult_attempts_approach",
    "idx_adult_attempts_one_running", "idx_adult_attempt_events_task_created",
}
_EXPECTED_TRIGGERS_V2: Final = _EXPECTED_TRIGGERS_V1 | {
    "validate_adult_attempt_update", "prevent_adult_attempts_delete",
    "prevent_adult_attempt_events_update", "prevent_adult_attempt_events_delete",
}


_ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "queued": frozenset({"analyzing", "cancelled", "failed"}),
    "analyzing": frozenset({"running", "checkpointed", "waiting_user", "needs_input", "paused", "failed", "cancelled"}),
    "running": frozenset({"evaluating", "checkpointed", "waiting_user", "needs_input", "paused", "failed", "cancelled"}),
    "evaluating": frozenset({"completed", "running", "checkpointed", "paused", "failed", "cancelled"}),
    "checkpointed": frozenset({"queued", "analyzing", "cancelled", "paused", "needs_input"}),
    "waiting_user": frozenset({"queued", "cancelled"}),
    "needs_input": frozenset({"queued", "cancelled"}),
    "paused": frozenset({"queued", "cancelled", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

class AdultTaskStore:
    """Owns the isolated Adult operational database; no provider integration."""

    def __init__(self, db_path: str | Path | None = None):
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise TaskStoreSchemaError("SQLite 3.38.0 or later is required.")
        configured = db_path if db_path is not None else os.environ.get("ADULT_STATE_DB")
        self._db_path = Path(configured) if configured is not None else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._db_path.exists()
        self._migrate()
        if not existed and os.name == "posix":
            os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._db_path), timeout=10, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise TaskStoreSchemaError("SQLite foreign-key enforcement is unavailable.")
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "delete":
            connection.close()
            raise TaskStoreSchemaError("Adult task database must use DELETE journal mode.")
        return connection

    def _apply_attempt_migration(self, connection: sqlite3.Connection) -> None:
        for statement in _ATTEMPT_SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO adult_schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (ATTEMPT_SCHEMA_VERSION, ATTEMPT_SCHEMA_NAME, _utc_now()),
        )

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adult_schema_migrations'"
            ).fetchone()
            if not exists:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO adult_schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (SCHEMA_VERSION, SCHEMA_NAME, _utc_now()),
                )
                self._validate_schema(connection, schema_version=SCHEMA_VERSION)
                self._apply_attempt_migration(connection)
                self._validate_schema(connection, schema_version=LATEST_SCHEMA_VERSION)
                connection.commit()
                return

            rows = connection.execute(
                "SELECT version,name FROM adult_schema_migrations ORDER BY version"
            ).fetchall()
            history = [(row["version"], row["name"]) for row in rows]
            expected_v1 = [(SCHEMA_VERSION, SCHEMA_NAME)]
            expected_v2 = expected_v1 + [(ATTEMPT_SCHEMA_VERSION, ATTEMPT_SCHEMA_NAME)]
            if history == expected_v1:
                self._validate_schema(connection, schema_version=SCHEMA_VERSION)
                self._apply_attempt_migration(connection)
                self._validate_schema(connection, schema_version=LATEST_SCHEMA_VERSION)
            elif history == expected_v2:
                self._validate_schema(connection, schema_version=LATEST_SCHEMA_VERSION)
            else:
                raise TaskStoreSchemaError("Unsupported Adult task database migration history.")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection, *, schema_version: int = LATEST_SCHEMA_VERSION
    ) -> None:
        if schema_version == SCHEMA_VERSION:
            expected_tables = _EXPECTED_TABLES_V1
            expected_indexes = _EXPECTED_INDEXES_V1
            expected_triggers = _EXPECTED_TRIGGERS_V1
        elif schema_version == LATEST_SCHEMA_VERSION:
            expected_tables = _EXPECTED_TABLES_V2
            expected_indexes = _EXPECTED_INDEXES_V2
            expected_triggers = _EXPECTED_TRIGGERS_V2
        else:
            raise TaskStoreSchemaError("Unsupported Adult task schema version.")

        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {row["name"] for row in rows}
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
        indexes = {row["name"] for row in rows}
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        triggers = {row["name"] for row in rows}
        if tables != expected_tables:
            raise TaskStoreSchemaError("Adult task database contains an unexpected table layout.")
        if not expected_indexes.issubset(indexes):
            raise TaskStoreSchemaError("Adult task database is missing required indexes.")
        if not expected_triggers.issubset(triggers):
            raise TaskStoreSchemaError("Adult task database is missing required triggers.")



    def claim_task(
        self, task_id: str, worker_id: str, lease_seconds: int = 30
    ) -> TaskLease:
        """Atomically claim one queued/checkpointed task with a fenced lease."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or len(worker_id) > 128
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", worker_id)
        ):
            raise AdultModelValidationError("worker_id must be a bounded opaque identifier.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise AdultModelValidationError("lease_seconds must be an integer from 1 through 300.")

        now = _utc_now()
        expires_at = (
            datetime.fromisoformat(now[:-1] + "+00:00") + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        event_id = str(uuid.uuid4())
        exhausted = False
        lease = None
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound("Task does not exist.")
            if row["status"] in TERMINAL_STATUSES:
                raise TaskTerminal("Terminal tasks cannot be claimed.")
            if row["cancel_requested"]:
                raise TaskCancelled("Cancelled tasks cannot be claimed.")
            if row["status"] not in {"queued", "checkpointed"}:
                raise LeaseNotAvailable("Task is not available for claiming.")

            deadline = datetime.fromisoformat(row["deadline_at"][:-1] + "+00:00")
            if deadline <= datetime.fromisoformat(now[:-1] + "+00:00") or row["total_attempts"] >= row["max_total_attempts"]:
                cursor = db.execute(
                    """UPDATE adult_tasks SET status='failed',lease_owner=NULL,
                       lease_expires_at=NULL,user_action_required=0,state_version=state_version+1,
                       updated_at=? WHERE task_id=? AND state_version=?""",
                    (now, task_id, row["state_version"]),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task changed while its execution budget was checked.")
                db.execute(
                    """INSERT INTO adult_task_events
                       (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                       VALUES (?,?,'budget_exhausted',?,'failed',?,?)""",
                    (event_id, task_id, row["status"],
                     json.dumps({"reason_code": "deadline_or_attempt_limit"}, separators=(",", ":")), now),
                )
                db.commit()
                exhausted = True
            else:
                active = db.execute(
                    "SELECT task_id FROM adult_tasks WHERE status IN ('analyzing','running','evaluating') LIMIT 1"
                ).fetchone()
                if active is not None:
                    raise TaskAlreadyActive("Another Adult task already holds the active lease.")
                generation = row["lease_generation"] + 1
                state_version = row["state_version"] + 1
                try:
                    cursor = db.execute(
                        """UPDATE adult_tasks SET status='analyzing',lease_owner=?,
                           lease_expires_at=?,lease_generation=?,state_version=?,updated_at=?
                           WHERE task_id=? AND state_version=? AND status=? AND cancel_requested=0""",
                        (worker_id, expires_at, generation, state_version, now,
                         task_id, row["state_version"], row["status"]),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TaskAlreadyActive("Another Adult task already holds the active lease.") from exc
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task changed while its lease was being claimed.")
                db.execute(
                    """INSERT INTO adult_task_events
                       (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                       VALUES (?,?,'task_claimed',?,'analyzing',?,?)""",
                    (event_id, task_id, row["status"],
                     json.dumps({"lease_generation": generation, "state_version": state_version},
                                separators=(",", ":")), now),
                )
                db.commit()
                lease = TaskLease._from_store(
                    task_id, worker_id, generation, expires_at, state_version
                )
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        if exhausted:
            raise TaskBudgetExhausted("Task execution budget is exhausted.")
        if lease is None:
            raise TaskStoreSchemaError("Claim completed without issuing a lease.")
        return lease

    def renew_lease(self, lease: TaskLease, lease_seconds: int = 30) -> TaskLease:
        """Extend only the current, unexpired fencing generation."""
        if not isinstance(lease, TaskLease):
            raise LeaseLost("A TaskStore-issued lease is required.")
        lease._validate()
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise AdultModelValidationError("lease_seconds must be an integer from 1 through 300.")
        now = _utc_now()
        expires_at = (
            datetime.fromisoformat(now[:-1] + "+00:00") + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        event_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (lease.task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task does not exist.")
            if (
                row["status"] not in ACTIVE_STATUSES
                or row["lease_owner"] != lease.worker_id
                or row["lease_generation"] != lease.lease_generation
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now
            ):
                raise LeaseLost("The task lease is expired or no longer current.")
            if row["cancel_requested"]:
                raise TaskCancelled("A cancellation request prevents lease renewal.")
            cursor = db.execute(
                """UPDATE adult_tasks SET lease_expires_at=?
                   WHERE task_id=? AND lease_owner=? AND lease_generation=?
                     AND lease_expires_at>? AND cancel_requested=0""",
                (expires_at, lease.task_id, lease.worker_id, lease.lease_generation, now),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("The task lease is expired or no longer current.")
            db.execute(
                """INSERT INTO adult_task_events
                   (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                   VALUES (?,?,'lease_renewed',?,?,?,?)""",
                (event_id, lease.task_id, row["status"], row["status"],
                 json.dumps({"lease_generation": lease.lease_generation}, separators=(",", ":")), now),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        return TaskLease._from_store(
            lease.task_id, lease.worker_id, lease.lease_generation,
            expires_at, lease.state_version_at_grant,
        )

    def release_lease(
        self,
        lease: TaskLease,
        expected_state_version: int,
        next_status: str = "checkpointed",
        reason: Literal["worker_released", "yielded", "cancel_observed", "budget_exhausted"] = "worker_released",
    ) -> AdultTaskSnapshot:
        """Release the current fenced lease and persist the resulting state atomically."""
        if not isinstance(lease, TaskLease):
            raise LeaseLost("A TaskStore-issued lease is required.")
        lease._validate()
        if (
            not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool)
            or expected_state_version < 0
        ):
            raise AdultModelValidationError(
                "expected_state_version must be a non-negative integer."
            )
        if not isinstance(next_status, str) or next_status not in TASK_STATUSES:
            raise AdultModelValidationError("Unsupported lease release destination.")
        allowed_reasons = {
            "worker_released", "yielded", "cancel_observed", "budget_exhausted",
        }
        if not isinstance(reason, str) or reason not in allowed_reasons:
            raise AdultModelValidationError("Unsupported lease release reason.")

        now = _utc_now()
        event_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (lease.task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task does not exist.")
            if row["status"] in TERMINAL_STATUSES:
                raise TaskTerminal("Terminal tasks cannot release a lease.")
            if (
                row["status"] not in ACTIVE_STATUSES
                or row["lease_owner"] != lease.worker_id
                or row["lease_generation"] != lease.lease_generation
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now
            ):
                raise LeaseLost("The task lease is expired or no longer current.")
            if row["state_version"] != expected_state_version:
                raise StaleTaskVersion("Task state version changed.")
            if next_status in ACTIVE_STATUSES or next_status not in _ALLOWED_TRANSITIONS[row["status"]]:
                raise InvalidTaskTransition("Requested lease release destination is not allowed.")
            if row["cancel_requested"] and next_status != "cancelled":
                raise TaskCancelled("A cancellation request permits only cancellation.")
            if next_status == "cancelled" and reason != "cancel_observed":
                raise AdultModelValidationError(
                    "Cancellation release requires the cancel_observed reason."
                )
            if reason == "cancel_observed" and next_status != "cancelled":
                raise AdultModelValidationError(
                    "cancel_observed release must transition to cancelled."
                )
            if next_status == "checkpointed":
                checkpoint = db.execute(
                    "SELECT 1 FROM adult_checkpoints WHERE task_id=? LIMIT 1",
                    (lease.task_id,),
                ).fetchone()
                if checkpoint is None:
                    raise InvalidCheckpoint(
                        "A durable checkpoint is required before releasing to checkpointed."
                    )

            user_action_required = int(next_status in {"needs_input", "paused"})
            cancel_requested = int(next_status == "cancelled" or row["cancel_requested"])
            cursor = db.execute(
                """UPDATE adult_tasks
                   SET status=?,user_action_required=?,cancel_requested=?,
                       lease_owner=NULL,lease_expires_at=NULL,
                       state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND status=? AND state_version=?
                     AND lease_owner=? AND lease_generation=? AND lease_expires_at>?""",
                (
                    next_status, user_action_required, cancel_requested, now,
                    lease.task_id, row["status"], expected_state_version,
                    lease.worker_id, lease.lease_generation, now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("The task lease or state version is no longer current.")
            safe_metadata = {
                "lease_generation": lease.lease_generation,
                "reason_code": reason,
            }
            TaskEvent(
                event_id=event_id,
                task_id=lease.task_id,
                event_type="lease_released",
                from_status=row["status"],
                to_status=next_status,
                metadata=safe_metadata,
                created_at=now,
            )
            db.execute(
                """INSERT INTO adult_task_events
                   (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                   VALUES (?,?,'lease_released',?,?,?,?)""",
                (
                    event_id, lease.task_id, row["status"], next_status,
                    json.dumps(safe_metadata, separators=(",", ":"), sort_keys=True),
                    now,
                ),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        result = self.get_task(lease.task_id)
        if result is None:
            raise TaskStoreSchemaError("Released task could not be reloaded.")
        return result

    def transition(
        self,
        task_id: str,
        expected_status: set[str],
        new_status: str,
        event_type: str,
        expected_state_version: int,
        metadata: dict | None = None,
        lease: TaskLease | None = None,
    ) -> AdultTaskSnapshot:
        """Apply one guarded state transition and append its event atomically.

        Lease acquisition and controlled resume/waiting-user paths are handled
        by their dedicated methods, not by this generic transition primitive.
        """
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if not isinstance(expected_status, (set, frozenset)) or not expected_status:
            raise AdultModelValidationError("expected_status must be a non-empty status set.")
        if any(not isinstance(value, str) or value not in TASK_STATUSES for value in expected_status):
            raise AdultModelValidationError("expected_status contains an unsupported status.")
        if not isinstance(new_status, str) or new_status not in TASK_STATUSES:
            raise AdultModelValidationError("Unsupported destination task status.")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise AdultModelValidationError("Unsupported task event type.")
        if event_type != "state_transition":
            raise AdultModelValidationError("Generic transitions must use the state_transition event.")
        if not isinstance(expected_state_version, int) or isinstance(expected_state_version, bool) or expected_state_version < 0:
            raise AdultModelValidationError("expected_state_version must be a non-negative integer.")
        safe_metadata = {} if metadata is None else metadata
        if not isinstance(safe_metadata, dict):
            raise AdultModelValidationError("metadata must be a JSON object.")
        now = _utc_now()
        event_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFound("Task does not exist.")
            current = row["status"]
            if current in TERMINAL_STATUSES:
                raise TaskTerminal("Terminal tasks cannot transition.")
            if current not in expected_status or row["state_version"] != expected_state_version:
                raise StaleTaskVersion("Task status or state version changed.")
            # Guard dedicated control-flow paths before checking the generic graph,
            # so callers receive the actionable policy error rather than a generic
            # invalid-transition error.
            if current in {"checkpointed", "waiting_user", "needs_input", "paused"} and new_status == "queued":
                raise UserApprovalRequired("Use the dedicated resume operation for this transition.")
            if new_status == "waiting_user":
                raise UserApprovalRequired("Use set_waiting_user to enforce the five-failure approval gate.")
            if new_status not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidTaskTransition("Requested task transition is not allowed.")
            if new_status in ACTIVE_STATUSES and current not in ACTIVE_STATUSES:
                raise LeaseNotAvailable("Active states can only be entered by claim_task.")
            if row["cancel_requested"] and new_status != "cancelled":
                raise TaskCancelled("Cancellation has precedence over other state transitions.")

            if current in ACTIVE_STATUSES:
                if not isinstance(lease, TaskLease):
                    raise LeaseLost("A current task lease is required.")
                lease._validate()
                if (
                    lease.task_id != task_id
                    or lease.worker_id != row["lease_owner"]
                    or lease.lease_generation != row["lease_generation"]
                    or row["lease_expires_at"] is None
                    or row["lease_expires_at"] <= now
                ):
                    raise LeaseLost("The task lease is expired or no longer current.")
            elif lease is not None:
                raise LeaseLost("A lease cannot be supplied for a task without an active lease.")

            user_action_required = int(new_status in {"needs_input", "paused"})
            cancel_requested = 1 if new_status == "cancelled" else int(row["cancel_requested"])
            if new_status in ACTIVE_STATUSES:
                lease_owner = row["lease_owner"]
                lease_expires_at = row["lease_expires_at"]
            else:
                lease_owner = None
                lease_expires_at = None

            # DTO validation rejects unsafe metadata before any durable write.
            TaskEvent(
                event_id=event_id,
                task_id=task_id,
                event_type="state_transition",
                from_status=current,
                to_status=new_status,
                metadata=safe_metadata,
                created_at=now,
            )
            cursor = db.execute(
                """UPDATE adult_tasks
                   SET status=?, user_action_required=?, cancel_requested=?,
                       lease_owner=?, lease_expires_at=?, state_version=state_version+1,
                       updated_at=?
                   WHERE task_id=? AND status=? AND state_version=?""",
                (
                    new_status, user_action_required, cancel_requested,
                    lease_owner, lease_expires_at, now, task_id,
                    current, expected_state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskVersion("Task status or state version changed.")
            db.execute(
                """INSERT INTO adult_task_events
                   (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                   VALUES (?,?,'state_transition', ?, ?, ?, ?)""",
                (
                    event_id, task_id, current, new_status,
                    json.dumps(safe_metadata, separators=(",", ":"), sort_keys=True, allow_nan=False),
                    now,
                ),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        result = self.get_task(task_id)
        if result is None:
            raise TaskStoreSchemaError("Transitioned task could not be reloaded.")
        return result

    def create_task(self, request: AdultTaskRequest, budget: TaskBudget) -> AdultTaskSnapshot:
        if not isinstance(request, AdultTaskRequest):
            raise AdultModelValidationError("request must be an AdultTaskRequest.")
        if not isinstance(budget, TaskBudget):
            raise InvalidTaskBudget("budget must be a validated TaskBudget.")
        if request.retention_policy == "encrypted":
            try:
                from agent.adult.payload_store import PayloadCipher, PayloadStoreError
                PayloadCipher.from_environment()
            except (ImportError, PayloadStoreError, ValueError):
                raise PayloadRetentionUnavailable(
                    "A valid local encryption key is required for encrypted payload retention."
                ) from None
        if request.retention_policy not in {"ephemeral", "encrypted"}:
            raise AdultModelValidationError("Unsupported payload retention policy.")

        task_id = str(uuid.uuid4())
        step_id = str(uuid.uuid4())
        now = _utc_now()
        deadline = (
            datetime.fromisoformat(now[:-1] + "+00:00")
            + timedelta(seconds=budget.max_wall_seconds)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        snapshot = CheckpointSnapshot(
            schema_version=1,
            task_id=task_id,
            task_type=request.task_type,
            status="queued",
            active_step_id=None,
            input_fingerprint=request.input_fingerprint,
            payload_id=request.payload_id,
            plan=(step_id,),
            completed_step_ids=(),
            pending_step_ids=(step_id,),
            total_attempts=0,
            failed_attempts_since_approval=0,
            provider_calls=0,
            spent_cost_microusd=0,
            remaining_attempts=budget.max_total_attempts,
            remaining_wall_seconds=budget.max_wall_seconds,
            remaining_provider_calls=budget.max_provider_calls,
            remaining_cost_microusd=budget.max_cost_microusd,
        )
        checkpoint_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO adult_tasks (
                    task_id, task_type, status, input_fingerprint,
                    instruction_fingerprint, request_options_json, payload_id,
                    retention_policy, current_step_id, failed_attempts_since_approval,
                    total_attempts, provider_calls, approvals_granted,
                    max_total_attempts, max_wall_seconds, max_provider_calls,
                    max_cost_microusd, spent_cost_microusd, deadline_at,
                    state_version, cancel_requested, user_action_required,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,0,?,?,?,?,0,?,0,0,0,?,?)""",
                (
                    task_id, request.task_type, "queued", request.input_fingerprint,
                    request.instruction_fingerprint,
                    json.dumps(request.requested_options, separators=(",", ":"), sort_keys=True, allow_nan=False),
                    request.payload_id, request.retention_policy, step_id,
                    budget.max_total_attempts, budget.max_wall_seconds,
                    budget.max_provider_calls, budget.max_cost_microusd,
                    deadline, now, now,
                ),
            )
            db.execute(
                """INSERT INTO adult_steps
                   (step_id,task_id,sequence_no,operation,status)
                   VALUES (?,?,0,'intake','pending')""",
                (step_id, task_id),
            )
            db.execute(
                """INSERT INTO adult_checkpoints
                   (checkpoint_id,task_id,revision,reason,schema_version,
                    snapshot_json,failed_attempts_since_approval,user_action_required,created_at)
                   VALUES (?,?,1,'initial',1,?,0,0,?)""",
                (checkpoint_id, task_id, snapshot.to_json(), now),
            )
            db.execute(
                """INSERT INTO adult_task_events
                   (event_id,task_id,event_type,from_status,to_status,metadata_json,created_at)
                   VALUES (?,?,'task_created',NULL,'queued','{}',?)""",
                (event_id, task_id, now),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
            raise AdultTaskStoreError("Task creation violated a persistence invariant.") from exc
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        result = self.get_task(task_id)
        if result is None:
            raise TaskStoreSchemaError("Created task could not be reloaded.")
        return result

    def get_task(self, task_id: str) -> AdultTaskSnapshot | None:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        with self._connect() as db:
            row = db.execute("SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._snapshot_from_row(row) if row else None

    def get_task_request(self, task_id: str) -> AdultTaskRequest | None:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        with self._connect() as db:
            row = db.execute(
                """SELECT task_type,input_fingerprint,instruction_fingerprint,
                          payload_id,retention_policy,request_options_json
                   FROM adult_tasks WHERE task_id=?""",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            options = json.loads(row["request_options_json"])
            return AdultTaskRequest(
                task_type=row["task_type"],
                input_fingerprint=row["input_fingerprint"],
                instruction_fingerprint=row["instruction_fingerprint"],
                payload_id=row["payload_id"],
                retention_policy=row["retention_policy"],
                requested_options=options,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TaskStoreSchemaError("Stored task request metadata is invalid.") from exc

    def list_tasks(
        self, status: str | None = None, limit: int = 20,
        cursor: str | None = None,
    ) -> TaskPage:
        if status is not None and (
            not isinstance(status, str) or status not in TASK_STATUSES
        ):
            raise AdultModelValidationError("Unsupported task status filter.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise AdultModelValidationError("limit must be an integer from 1 through 50.")
        cursor_values = self._decode_cursor(cursor) if cursor is not None else None
        clauses = []
        parameters: list[object] = []
        if status is not None:
            clauses.append("status=?")
            parameters.append(status)
        if cursor_values is not None:
            created_at, task_id = cursor_values
            clauses.append("(created_at < ? OR (created_at = ? AND task_id < ?))")
            parameters.extend((created_at, created_at, task_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM adult_tasks" + where
                + " ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = self._encode_cursor(last["created_at"], last["task_id"])
        return TaskPage(
            items=tuple(self._snapshot_from_row(row) for row in page_rows),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM adult_checkpoints
                   WHERE task_id=? ORDER BY revision DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["snapshot_json"])
            tuple_fields = {
                "plan", "completed_step_ids", "pending_step_ids",
                "attempted_approach_keys", "excluded_approach_keys", "allowed_decisions",
            }
            for key in tuple_fields:
                raw[key] = tuple(raw.get(key, ()))
            checkpoint_snapshot = CheckpointSnapshot(**raw)
            return Checkpoint(
                checkpoint_id=row["checkpoint_id"],
                task_id=row["task_id"],
                revision=row["revision"],
                reason=row["reason"],
                schema_version=row["schema_version"],
                failed_attempts_since_approval=row["failed_attempts_since_approval"],
                user_action_required=bool(row["user_action_required"]),
                created_at=row["created_at"],
                snapshot=checkpoint_snapshot,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreSchemaError("Stored checkpoint metadata is invalid.") from exc

    def list_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]:
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise AdultModelValidationError("limit must be an integer from 1 through 200.")
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM adult_task_events WHERE task_id=?
                   ORDER BY created_at,event_id LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        try:
            return [
                TaskEvent(
                    event_id=row["event_id"], task_id=row["task_id"],
                    event_type=row["event_type"], from_status=row["from_status"],
                    to_status=row["to_status"], metadata=json.loads(row["metadata_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreSchemaError("Stored task event metadata is invalid.") from exc

    @staticmethod
    def _require_current_lease(
        row: sqlite3.Row, task_id: str, lease: TaskLease, now: str
    ) -> None:
        if not isinstance(lease, TaskLease):
            raise LeaseLost("A TaskStore-issued lease is required.")
        lease._validate()
        if (
            row["task_id"] != task_id
            or lease.task_id != task_id
            or row["status"] not in ACTIVE_STATUSES
            or row["lease_owner"] != lease.worker_id
            or row["lease_generation"] != lease.lease_generation
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            raise LeaseLost("The task lease is expired or no longer current.")
        if row["cancel_requested"]:
            raise TaskCancelled("A cancellation request prevents this mutation.")

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
        try:
            raw = json.loads(row["snapshot_json"])
            if not isinstance(raw, dict):
                raise ValueError("checkpoint JSON must be an object")
            tuple_fields = {
                "plan", "completed_step_ids", "pending_step_ids",
                "attempted_approach_keys", "excluded_approach_keys", "allowed_decisions",
            }
            for key in tuple_fields:
                raw[key] = tuple(raw.get(key, ()))
            snapshot = CheckpointSnapshot(**raw)
            return Checkpoint(
                checkpoint_id=row["checkpoint_id"], task_id=row["task_id"],
                revision=row["revision"], reason=row["reason"],
                schema_version=row["schema_version"],
                failed_attempts_since_approval=row["failed_attempts_since_approval"],
                user_action_required=bool(row["user_action_required"]),
                created_at=row["created_at"], snapshot=snapshot,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskStoreSchemaError("Stored checkpoint metadata is invalid.") from exc

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> StepRecord:
        try:
            return StepRecord(
                step_id=row["step_id"], task_id=row["task_id"],
                sequence_no=row["sequence_no"], operation=row["operation"],
                status=row["status"], adapter_id=row["adapter_id"],
                provider_id=row["provider_id"], model_id=row["model_id"],
                tool_id=row["tool_id"], input_payload_id=row["input_payload_id"],
                output_artifact_id=row["output_artifact_id"],
                config_fingerprint=row["config_fingerprint"],
                started_at=row["started_at"], completed_at=row["completed_at"],
            )
        except (TypeError, ValueError) as exc:
            raise TaskStoreSchemaError("Stored task step is invalid.") from exc

    @staticmethod
    def _validate_checkpoint_snapshot(
        row: sqlite3.Row, snapshot: CheckpointSnapshot, db: sqlite3.Connection
    ) -> str:
        if not isinstance(snapshot, CheckpointSnapshot):
            raise InvalidCheckpoint("A validated CheckpointSnapshot is required.")
        if (
            snapshot.task_id != row["task_id"]
            or snapshot.task_type != row["task_type"]
            or snapshot.status != row["status"]
            or snapshot.input_fingerprint not in {None, row["input_fingerprint"]}
            or snapshot.payload_id not in {None, row["payload_id"]}
        ):
            raise InvalidCheckpoint("Checkpoint snapshot does not match the current task.")
        if (
            snapshot.total_attempts != row["total_attempts"]
            or snapshot.failed_attempts_since_approval != row["failed_attempts_since_approval"]
            or snapshot.provider_calls != row["provider_calls"]
            or snapshot.spent_cost_microusd != row["spent_cost_microusd"]
        ):
            raise InvalidCheckpoint("Checkpoint counters do not match the current task.")
        if snapshot.status == "waiting_user":
            raise InvalidCheckpoint("Use set_waiting_user for the approval checkpoint.")
        if snapshot.status in TERMINAL_STATUSES and snapshot.status != row["status"]:
            raise InvalidCheckpoint("Checkpoint terminal state does not match the task.")

        referenced = set(snapshot.plan)
        referenced.update(snapshot.completed_step_ids)
        referenced.update(snapshot.pending_step_ids)
        if snapshot.active_step_id is not None:
            referenced.add(snapshot.active_step_id)
        if referenced:
            placeholders = ",".join("?" for _ in referenced)
            rows = db.execute(
                f"SELECT step_id FROM adult_steps WHERE task_id=? AND step_id IN ({placeholders})",
                (row["task_id"], *sorted(referenced)),
            ).fetchall()
            known = {item["step_id"] for item in rows}
            if known != referenced:
                raise InvalidCheckpoint("Checkpoint contains a step outside the task.")
        return snapshot.to_json()

    def save_step(
        self,
        task_id: str,
        step: StepRecord,
        lease: TaskLease,
        expected_state_version: int,
    ) -> StepRecord:
        """Insert or advance one task step under the current fenced lease."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if not isinstance(step, StepRecord) or step.task_id != task_id:
            raise AdultModelValidationError("step must be a validated StepRecord for this task.")
        if step.started_at is not None or step.completed_at is not None:
            raise AdultModelValidationError("Step timestamps are assigned by TaskStore.")
        if (
            not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool)
            or expected_state_version < 0
        ):
            raise AdultModelValidationError("expected_state_version must be a non-negative integer.")

        now = _utc_now()
        event_id = str(uuid.uuid4())
        returned: StepRecord | None = None
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            self._require_current_lease(task, task_id, lease, now)
            if task["state_version"] != expected_state_version:
                raise StaleTaskVersion("Task state version changed.")
            if task["status"] not in {"analyzing", "running", "evaluating"}:
                raise InvalidTaskTransition("Task state does not allow step mutations.")

            existing = db.execute(
                "SELECT * FROM adult_steps WHERE step_id=?", (step.step_id,)
            ).fetchone()
            columns = (
                "adapter_id", "provider_id", "model_id", "tool_id",
                "input_payload_id", "output_artifact_id", "config_fingerprint",
            )
            supplied = {name: getattr(step, name) for name in columns}
            if existing is None:
                if step.status != "pending":
                    raise InvalidTaskTransition("New steps must begin in pending state.")
                maximum = db.execute(
                    "SELECT COALESCE(MAX(sequence_no),-1) FROM adult_steps WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
                if step.sequence_no != maximum + 1:
                    raise AdultModelValidationError("New step sequence_no must be the next task sequence.")
                db.execute(
                    """INSERT INTO adult_steps(
                        step_id,task_id,sequence_no,operation,status,adapter_id,provider_id,
                        model_id,tool_id,input_payload_id,output_artifact_id,config_fingerprint,
                        started_at,completed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                    (
                        step.step_id, task_id, step.sequence_no, step.operation, "pending",
                        *(supplied[name] for name in columns),
                    ),
                )
                event_type = "step_created"
                status = "pending"
                started_at = None
                completed_at = None
            else:
                if (
                    existing["task_id"] != task_id
                    or existing["sequence_no"] != step.sequence_no
                    or existing["operation"] != step.operation
                ):
                    raise AdultModelValidationError("Step identity, task, sequence and operation are immutable.")
                old_status = existing["status"]
                allowed = {
                    "pending": {"pending", "running", "skipped"},
                    "running": {"running", "succeeded", "failed"},
                    "succeeded": set(), "failed": set(), "skipped": set(),
                }
                if step.status not in allowed[old_status]:
                    raise InvalidTaskTransition("Requested step status transition is not allowed.")
                if old_status in {"succeeded", "failed", "skipped"}:
                    raise InvalidTaskTransition("Finished steps cannot be modified.")
                for name, value in supplied.items():
                    old_value = existing[name]
                    if old_value is not None and value is not None and old_value != value:
                        raise AdultModelValidationError(f"{name} is immutable after it is assigned.")
                status = step.status
                started_at = existing["started_at"]
                completed_at = existing["completed_at"]
                if old_status == "pending" and status == "running":
                    started_at = now
                if status in {"succeeded", "failed", "skipped"}:
                    completed_at = now
                db.execute(
                    """UPDATE adult_steps
                       SET status=?, adapter_id=COALESCE(adapter_id,?),
                           provider_id=COALESCE(provider_id,?), model_id=COALESCE(model_id,?),
                           tool_id=COALESCE(tool_id,?), input_payload_id=COALESCE(input_payload_id,?),
                           output_artifact_id=COALESCE(output_artifact_id,?),
                           config_fingerprint=COALESCE(config_fingerprint,?),
                           started_at=?, completed_at=?
                       WHERE step_id=? AND task_id=?""",
                    (
                        status, *(supplied[name] for name in columns),
                        started_at, completed_at, step.step_id, task_id,
                    ),
                )
                event_type = "step_transition"

            actual = db.execute(
                "SELECT * FROM adult_steps WHERE step_id=?", (step.step_id,)
            ).fetchone()
            returned = self._step_from_row(actual)
            current_step_id = task["current_step_id"]
            if returned.status == "running":
                current_step_id = returned.step_id
            elif current_step_id == returned.step_id and returned.status in {"succeeded", "failed", "skipped"}:
                next_step = db.execute(
                    """SELECT step_id FROM adult_steps
                       WHERE task_id=? AND status='pending'
                       ORDER BY sequence_no LIMIT 1""",
                    (task_id,),
                ).fetchone()
                current_step_id = next_step["step_id"] if next_step else None
            elif current_step_id is None:
                current_step_id = returned.step_id

            metadata = {
                "step_id": returned.step_id,
                "sequence_no": returned.sequence_no,
                "operation": returned.operation,
                "status": returned.status,
            }
            TaskEvent(
                event_id=event_id, task_id=task_id, event_type=event_type,
                from_status=task["status"], to_status=task["status"],
                metadata=metadata, created_at=now,
            )
            cursor = db.execute(
                """UPDATE adult_tasks
                   SET current_step_id=?,state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND state_version=? AND lease_owner=?
                     AND lease_generation=? AND lease_expires_at>?""",
                (
                    current_step_id, now, task_id, expected_state_version,
                    lease.worker_id, lease.lease_generation, now,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskVersion("Task state version or lease changed.")
            db.execute(
                """INSERT INTO adult_task_events(
                    event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event_id, task_id, event_type, task["status"], task["status"],
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True), now,
                ),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        if returned is None:
            raise TaskStoreSchemaError("Saved step could not be reloaded.")
        return returned

    def save_checkpoint(
        self,
        task_id: str,
        snapshot: CheckpointSnapshot,
        reason: str,
        expected_state_version: int,
        lease: TaskLease | None = None,
    ) -> Checkpoint:
        """Append a validated checkpoint and advance task state_version atomically."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if not isinstance(reason, str) or reason not in CHECKPOINT_REASONS:
            raise AdultModelValidationError("Unsupported checkpoint reason.")
        if (
            not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool)
            or expected_state_version < 0
        ):
            raise AdultModelValidationError("expected_state_version must be a non-negative integer.")
        if not isinstance(snapshot, CheckpointSnapshot) or snapshot.task_id != task_id:
            raise InvalidCheckpoint("A validated checkpoint snapshot for this task is required.")

        now = _utc_now()
        checkpoint_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        result: Checkpoint | None = None
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            if task["status"] in ACTIVE_STATUSES:
                if not isinstance(lease, TaskLease):
                    raise LeaseLost("An active checkpoint requires a current task lease.")
                self._require_current_lease(task, task_id, lease, now)
            elif lease is not None:
                raise LeaseLost("A lease cannot be supplied for a non-active task.")
            if task["state_version"] != expected_state_version:
                raise StaleTaskVersion("Task state version changed.")
            encoded = self._validate_checkpoint_snapshot(task, snapshot, db)
            latest = db.execute(
                "SELECT COALESCE(MAX(revision),0) FROM adult_checkpoints WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            revision = int(latest) + 1
            action_required = int(snapshot.status in {"waiting_user", "needs_input", "paused"})
            if snapshot.status != task["status"]:
                raise InvalidCheckpoint("Checkpoint status must match the current task status.")
            if action_required != int(task["user_action_required"]):
                raise InvalidCheckpoint("Checkpoint user-action flag does not match the task.")
            db.execute(
                """INSERT INTO adult_checkpoints(
                    checkpoint_id,task_id,revision,reason,schema_version,snapshot_json,
                    failed_attempts_since_approval,user_action_required,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    checkpoint_id, task_id, revision, reason, snapshot.schema_version,
                    encoded, snapshot.failed_attempts_since_approval, action_required, now,
                ),
            )
            cursor = db.execute(
                """UPDATE adult_tasks
                   SET current_step_id=?,selected_adapter=?,selected_provider=?,
                       selected_model=?,selected_tool=?,latest_result_artifact_id=?,
                       state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND state_version=?""",
                (
                    snapshot.active_step_id, snapshot.selected_adapter,
                    snapshot.selected_provider, snapshot.selected_model, snapshot.selected_tool,
                    snapshot.latest_result_artifact_id, now, task_id, expected_state_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskVersion("Task state version changed during checkpoint.")
            metadata = {
                "checkpoint_revision": revision,
                "schema_version": snapshot.schema_version,
                "reason_code": reason,
            }
            TaskEvent(
                event_id=event_id, task_id=task_id, event_type="checkpoint_saved",
                from_status=task["status"], to_status=task["status"],
                metadata=metadata, created_at=now,
            )
            db.execute(
                """INSERT INTO adult_task_events(
                    event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                ) VALUES(?,?,'checkpoint_saved',?,?,?,?)""",
                (
                    event_id, task_id, task["status"], task["status"],
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True), now,
                ),
            )
            db.commit()
            result = Checkpoint(
                checkpoint_id=checkpoint_id, task_id=task_id, revision=revision,
                reason=reason, schema_version=snapshot.schema_version,
                failed_attempts_since_approval=snapshot.failed_attempts_since_approval,
                user_action_required=bool(action_required), created_at=now, snapshot=snapshot,
            )
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        if result is None:
            raise TaskStoreSchemaError("Checkpoint could not be created.")
        return result

    def set_waiting_user(
        self,
        task_id: str,
        snapshot: CheckpointSnapshot,
        lease: TaskLease,
        expected_state_version: int,
    ) -> Checkpoint:
        """Atomically checkpoint the exact five-failure approval boundary."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if not isinstance(snapshot, CheckpointSnapshot) or snapshot.task_id != task_id:
            raise InvalidCheckpoint("A validated checkpoint snapshot for this task is required.")
        if (
            not isinstance(expected_state_version, int)
            or isinstance(expected_state_version, bool)
            or expected_state_version < 0
        ):
            raise AdultModelValidationError("expected_state_version must be a non-negative integer.")
        now = _utc_now()
        checkpoint_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        result: Checkpoint | None = None
        exhausted = False
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            self._require_current_lease(task, task_id, lease, now)
            if task["state_version"] != expected_state_version:
                raise StaleTaskVersion("Task state version changed.")
            if (
                snapshot.task_type != task["task_type"]
                or snapshot.input_fingerprint not in {None, task["input_fingerprint"]}
                or snapshot.payload_id not in {None, task["payload_id"]}
                or snapshot.total_attempts != task["total_attempts"]
                or snapshot.provider_calls != task["provider_calls"]
                or snapshot.spent_cost_microusd != task["spent_cost_microusd"]
            ):
                raise InvalidCheckpoint("Waiting-user snapshot does not match the current task.")
            step_ids = set(snapshot.plan) | set(snapshot.completed_step_ids) | set(snapshot.pending_step_ids)
            if snapshot.active_step_id is not None:
                step_ids.add(snapshot.active_step_id)
            if step_ids:
                placeholders = ",".join("?" for _ in step_ids)
                rows = db.execute(
                    f"SELECT step_id FROM adult_steps WHERE task_id=? AND step_id IN ({placeholders})",
                    (task_id, *sorted(step_ids)),
                ).fetchall()
                if {item["step_id"] for item in rows} != step_ids:
                    raise InvalidCheckpoint("Waiting-user snapshot contains a step outside the task.")
            if task["failed_attempts_since_approval"] != 5:
                raise UserApprovalRequired("Exactly five failed attempts are required.")
            if (
                snapshot.status != "waiting_user"
                or snapshot.failed_attempts_since_approval != 5
                or snapshot.pending_user_action != "approval_required"
                or set(snapshot.allowed_decisions) != {"continue", "cancel"}
                or snapshot.total_attempts != task["total_attempts"]
            ):
                raise InvalidCheckpoint("Waiting-user snapshot does not satisfy the approval contract.")
            deadline_left = max(
                0, int((
                    datetime.fromisoformat(task["deadline_at"][:-1] + "+00:00")
                    - datetime.fromisoformat(now[:-1] + "+00:00")
                ).total_seconds())
            )
            has_budget = (
                task["total_attempts"] < task["max_total_attempts"]
                and deadline_left > 0
                and task["provider_calls"] <= task["max_provider_calls"]
                and task["spent_cost_microusd"] <= task["max_cost_microusd"]
            )
            if not has_budget:
                db.execute(
                    """UPDATE adult_tasks
                       SET status='failed',user_action_required=0,
                           lease_owner=NULL,lease_expires_at=NULL,
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND state_version=? AND lease_generation=?""",
                    (now, task_id, expected_state_version, lease.lease_generation),
                )
                metadata = {"reason_code": "attempt_budget_exhausted"}
                TaskEvent(
                    event_id=event_id, task_id=task_id, event_type="budget_exhausted",
                    from_status=task["status"], to_status="failed",
                    metadata=metadata, created_at=now,
                )
                db.execute(
                    """INSERT INTO adult_task_events(
                        event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                    ) VALUES(?,?,'budget_exhausted',?,'failed',?,?)""",
                    (
                        event_id, task_id, task["status"],
                        json.dumps(metadata, separators=(",", ":")), now,
                    ),
                )
                exhausted = True
            else:
                encoded = snapshot.to_json()
                latest = db.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM adult_checkpoints WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
                revision = int(latest) + 1
                db.execute(
                    """INSERT INTO adult_checkpoints(
                        checkpoint_id,task_id,revision,reason,schema_version,snapshot_json,
                        failed_attempts_since_approval,user_action_required,created_at
                    ) VALUES(?,?,?,'approval_required',?, ?,5,1,?)""",
                    (checkpoint_id, task_id, revision, snapshot.schema_version, encoded, now),
                )
                cursor = db.execute(
                    """UPDATE adult_tasks
                       SET status='waiting_user',user_action_required=1,
                           current_step_id=?,selected_adapter=?,selected_provider=?,
                           selected_model=?,selected_tool=?,latest_result_artifact_id=?,
                           lease_owner=NULL,lease_expires_at=NULL,
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND state_version=? AND lease_generation=?
                         AND lease_owner=? AND lease_expires_at>? AND cancel_requested=0""",
                    (
                        snapshot.active_step_id, snapshot.selected_adapter,
                        snapshot.selected_provider, snapshot.selected_model,
                        snapshot.selected_tool, snapshot.latest_result_artifact_id,
                        now, task_id, expected_state_version, lease.lease_generation,
                        lease.worker_id, now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task state or lease changed during approval checkpoint.")
                metadata = {
                    "checkpoint_revision": revision,
                    "failed_attempts_since_approval": 5,
                }
                TaskEvent(
                    event_id=event_id, task_id=task_id, event_type="waiting_user",
                    from_status=task["status"], to_status="waiting_user",
                    metadata=metadata, created_at=now,
                )
                db.execute(
                    """INSERT INTO adult_task_events(
                        event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                    ) VALUES(?,?,'waiting_user',?,'waiting_user',?,?)""",
                    (
                        event_id, task_id,
                        task["status"],
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True), now,
                    ),
                )
                result = Checkpoint(
                    checkpoint_id=checkpoint_id, task_id=task_id, revision=revision,
                    reason="approval_required", schema_version=1,
                    failed_attempts_since_approval=5, user_action_required=True,
                    created_at=now, snapshot=snapshot,
                )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        if exhausted:
            raise TaskBudgetExhausted("No execution budget remains for user approval.")
        if result is None:
            raise TaskStoreSchemaError("Waiting-user checkpoint could not be created.")
        return result

    def resume(self, task_id: str, decision: str) -> AdultTaskSnapshot:
        """Persist an explicit continue/cancel decision; never dispatch work."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        if decision not in {"continue", "cancel"}:
            raise AdultModelValidationError("decision must be continue or cancel.")
        if decision == "cancel":
            return self.request_cancel(task_id)

        now = _utc_now()
        event_id = str(uuid.uuid4())
        result: AdultTaskSnapshot | None = None
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            if task["status"] in TERMINAL_STATUSES:
                raise TaskTerminal("Terminal tasks cannot be resumed.")
            if task["cancel_requested"]:
                raise TaskCancelled("A cancellation request prevents task resume.")
            if task["status"] == "needs_input":
                raise UserApprovalRequired("A validated replacement payload is required before resume.")
            if task["status"] == "paused":
                raise InvalidTaskTransition("Paused tasks require a separately defined corrective action.")
            if task["status"] not in {"waiting_user", "checkpointed"}:
                raise InvalidTaskTransition("Only waiting_user or checkpointed tasks can be resumed.")

            if task["status"] == "waiting_user" and (
                not task["user_action_required"] or task["failed_attempts_since_approval"] != 5
            ):
                raise UserApprovalRequired("Waiting-user task does not satisfy the approval gate.")
            deadline_left = max(
                0, int((
                    datetime.fromisoformat(task["deadline_at"][:-1] + "+00:00")
                    - datetime.fromisoformat(now[:-1] + "+00:00")
                ).total_seconds())
            )
            budget_available = (
                task["total_attempts"] < task["max_total_attempts"]
                and deadline_left > 0
                and task["provider_calls"] <= task["max_provider_calls"]
                and task["spent_cost_microusd"] <= task["max_cost_microusd"]
            )
            if not budget_available:
                cursor = db.execute(
                    """UPDATE adult_tasks
                       SET status='failed',user_action_required=0,
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND status=? AND state_version=?""",
                    (now, task_id, task["status"], task["state_version"]),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task state changed while checking resume budget.")
                metadata = {"reason_code": "resume_budget_exhausted"}
                TaskEvent(
                    event_id=event_id, task_id=task_id, event_type="budget_exhausted",
                    from_status=task["status"], to_status="failed",
                    metadata=metadata, created_at=now,
                )
                db.execute(
                    """INSERT INTO adult_task_events(
                        event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                    ) VALUES(?,?,'budget_exhausted',?,'failed',?,?)""",
                    (
                        event_id, task_id, task["status"],
                        json.dumps(metadata, separators=(",", ":")), now,
                    ),
                )
            else:
                waiting_for_user = task["status"] == "waiting_user"
                cursor = db.execute(
                    """UPDATE adult_tasks
                       SET status='queued',
                           approvals_granted=approvals_granted+?,
                           failed_attempts_since_approval=CASE WHEN ?=1 THEN 0
                                                               ELSE failed_attempts_since_approval END,
                           user_action_required=0,state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND status=? AND state_version=? AND cancel_requested=0""",
                    (
                        int(waiting_for_user), int(waiting_for_user), now,
                        task_id, task["status"], task["state_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task state changed while resuming.")
                metadata = {"decision": "continue", "approval_granted": waiting_for_user}
                TaskEvent(
                    event_id=event_id, task_id=task_id, event_type="task_resumed",
                    from_status=task["status"], to_status="queued",
                    metadata=metadata, created_at=now,
                )
                db.execute(
                    """INSERT INTO adult_task_events(
                        event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                    ) VALUES(?,?,'task_resumed',?,'queued',?,?)""",
                    (
                        event_id, task_id, task["status"],
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True), now,
                    ),
                )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        result = self.get_task(task_id)
        if result is None:
            raise TaskStoreSchemaError("Resumed task could not be reloaded.")
        return result

    def request_cancel(self, task_id: str) -> AdultTaskSnapshot:
        """Persist a cancellation request with active-worker acknowledgement semantics."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise AdultModelValidationError("task_id must be a bounded identifier.")
        now = _utc_now()
        event_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM adult_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise TaskNotFound("Task does not exist.")
            if task["status"] in {"completed", "failed"}:
                raise TaskTerminal("Completed or failed tasks cannot be cancelled.")
            if task["status"] == "cancelled" or (
                task["status"] in ACTIVE_STATUSES and task["cancel_requested"]
            ):
                db.commit()
                return self._snapshot_from_row(task)

            active = task["status"] in ACTIVE_STATUSES
            new_status = task["status"] if active else "cancelled"
            event_type = "cancel_requested" if active else "task_cancelled"
            cursor = db.execute(
                """UPDATE adult_tasks
                   SET status=?,cancel_requested=1,user_action_required=0,
                       lease_owner=CASE WHEN ?=1 THEN lease_owner ELSE NULL END,
                       lease_expires_at=CASE WHEN ?=1 THEN lease_expires_at ELSE NULL END,
                       state_version=state_version+1,updated_at=?
                   WHERE task_id=? AND state_version=?""",
                (
                    new_status, int(active), int(active), now,
                    task_id, task["state_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTaskVersion("Task state changed while requesting cancellation.")
            metadata = {"reason_code": "user_cancelled"}
            TaskEvent(
                event_id=event_id, task_id=task_id, event_type=event_type,
                from_status=task["status"], to_status=new_status,
                metadata=metadata, created_at=now,
            )
            db.execute(
                """INSERT INTO adult_task_events(
                    event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    event_id, task_id, event_type, task["status"], new_status,
                    json.dumps(metadata, separators=(",", ":")), now,
                ),
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        result = self.get_task(task_id)
        if result is None:
            raise TaskStoreSchemaError("Cancelled task could not be reloaded.")
        return result

    def _build_recovery_snapshot(
        self, db: sqlite3.Connection, task: sqlite3.Row,
        new_status: str, now: str,
    ) -> CheckpointSnapshot:
        steps = db.execute(
            "SELECT * FROM adult_steps WHERE task_id=? ORDER BY sequence_no",
            (task["task_id"],),
        ).fetchall()
        plan = tuple(row["step_id"] for row in steps)
        completed = tuple(
            row["step_id"] for row in steps if row["status"] in {"succeeded", "skipped"}
        )
        pending = tuple(
            row["step_id"] for row in steps if row["status"] in {"pending", "running"}
        )
        attempted: tuple[str, ...] = ()
        excluded: tuple[str, ...] = ()
        has_attempts = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adult_attempts'"
        ).fetchone()
        if has_attempts:
            attempts = db.execute(
                """SELECT approach_key,status FROM adult_attempts
                   WHERE task_id=? ORDER BY attempt_no""",
                (task["task_id"],),
            ).fetchall()
            attempted = tuple(dict.fromkeys(row["approach_key"] for row in attempts))
            excluded = tuple(dict.fromkeys(
                row["approach_key"] for row in attempts
                if row["status"] in {"failed", "rejected", "timed_out", "unknown_outcome"}
            ))
        remaining_wall = max(
            0, int((
                datetime.fromisoformat(task["deadline_at"][:-1] + "+00:00")
                - datetime.fromisoformat(now[:-1] + "+00:00")
            ).total_seconds())
        )
        return CheckpointSnapshot(
            schema_version=1, task_id=task["task_id"], task_type=task["task_type"],
            status=new_status, active_step_id=task["current_step_id"],
            input_fingerprint=task["input_fingerprint"], payload_id=task["payload_id"],
            plan=plan, completed_step_ids=completed, pending_step_ids=pending,
            latest_result_artifact_id=task["latest_result_artifact_id"],
            selected_adapter=task["selected_adapter"], selected_provider=task["selected_provider"],
            selected_model=task["selected_model"], selected_tool=task["selected_tool"],
            attempted_approach_keys=attempted, excluded_approach_keys=excluded,
            total_attempts=task["total_attempts"],
            failed_attempts_since_approval=task["failed_attempts_since_approval"],
            provider_calls=task["provider_calls"], spent_cost_microusd=task["spent_cost_microusd"],
            remaining_attempts=max(0, task["max_total_attempts"] - task["total_attempts"]),
            remaining_wall_seconds=remaining_wall,
            remaining_provider_calls=max(0, task["max_provider_calls"] - task["provider_calls"]),
            remaining_cost_microusd=max(0, task["max_cost_microusd"] - task["spent_cost_microusd"]),
            last_error_code=task["last_error_code"],
        )

    def recover_expired_leases(self) -> int:
        """Checkpoint and release expired task leases without re-dispatching work."""
        now = _utc_now()
        recovered = 0
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM adult_tasks
                   WHERE status IN ('analyzing','running','evaluating')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                   ORDER BY created_at,task_id""",
                (now,),
            ).fetchall()
            for task in rows:
                current = db.execute(
                    "SELECT * FROM adult_tasks WHERE task_id=?", (task["task_id"],)
                ).fetchone()
                if (
                    current is None or current["status"] not in ACTIVE_STATUSES
                    or current["lease_generation"] != task["lease_generation"]
                    or current["lease_expires_at"] is None
                    or current["lease_expires_at"] > now
                ):
                    continue
                cancelled = bool(current["cancel_requested"])
                new_status = "cancelled" if cancelled else "checkpointed"
                reason = "cancelled" if cancelled else "lease_recovered"
                snapshot = self._build_recovery_snapshot(db, current, new_status, now)
                latest = db.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM adult_checkpoints WHERE task_id=?",
                    (current["task_id"],),
                ).fetchone()[0]
                revision = int(latest) + 1
                checkpoint_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO adult_checkpoints(
                        checkpoint_id,task_id,revision,reason,schema_version,snapshot_json,
                        failed_attempts_since_approval,user_action_required,created_at
                    ) VALUES(?,?,?,?,?,?,?,0,?)""",
                    (
                        checkpoint_id, current["task_id"], revision, reason,
                        snapshot.schema_version, snapshot.to_json(),
                        snapshot.failed_attempts_since_approval, now,
                    ),
                )
                cursor = db.execute(
                    """UPDATE adult_tasks
                       SET status=?,user_action_required=0,lease_owner=NULL,
                           lease_expires_at=NULL,lease_generation=lease_generation+1,
                           state_version=state_version+1,updated_at=?
                       WHERE task_id=? AND status=? AND state_version=?
                         AND lease_generation=? AND lease_expires_at<=?""",
                    (
                        new_status, now, current["task_id"], current["status"],
                        current["state_version"], current["lease_generation"], now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleTaskVersion("Task changed during lease recovery.")
                event_type = "cancel_recovered" if cancelled else "lease_expired_recovered"
                metadata = {
                    "checkpoint_revision": revision,
                    "lease_generation": current["lease_generation"],
                }
                recovery_event_id = str(uuid.uuid4())
                TaskEvent(
                    event_id=recovery_event_id, task_id=current["task_id"],
                    event_type=event_type, from_status=current["status"],
                    to_status=new_status, metadata=metadata, created_at=now,
                )
                db.execute(
                    """INSERT INTO adult_task_events(
                        event_id,task_id,event_type,from_status,to_status,metadata_json,created_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        recovery_event_id, current["task_id"], event_type,
                        current["status"], new_status,
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True), now,
                    ),
                )
                recovered += 1
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        return recovered

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> AdultTaskSnapshot:
        return AdultTaskSnapshot(
            task_id=row["task_id"], task_type=row["task_type"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            state_version=row["state_version"], current_step_id=row["current_step_id"],
            attempt_count=row["total_attempts"],
            failed_attempts_since_approval=row["failed_attempts_since_approval"],
            provider_calls=row["provider_calls"], approvals_granted=row["approvals_granted"],
            max_total_attempts=row["max_total_attempts"],
            max_wall_seconds=row["max_wall_seconds"],
            max_provider_calls=row["max_provider_calls"],
            max_cost_microusd=row["max_cost_microusd"],
            spent_cost_microusd=row["spent_cost_microusd"], deadline_at=row["deadline_at"],
            selected_adapter=row["selected_adapter"], selected_provider=row["selected_provider"],
            selected_model=row["selected_model"], selected_tool=row["selected_tool"],
            latest_result_artifact_id=row["latest_result_artifact_id"],
            last_error_code=row["last_error_code"],
            user_action_required=bool(row["user_action_required"]),
            cancel_requested=bool(row["cancel_requested"]),
        )

    @staticmethod
    def _encode_cursor(created_at: str, task_id: str) -> str:
        payload = json.dumps(
            {"v": 1, "created_at": created_at, "task_id": task_id},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
            raise InvalidCursor("Task cursor is invalid.")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
            if len(decoded) > 1024:
                raise InvalidCursor("Task cursor is invalid.")
            value = json.loads(decoded.decode("utf-8"))
            if (
                not isinstance(value, dict)
                or set(value) != {"v", "created_at", "task_id"}
                or type(value["v"]) is not int or value["v"] != 1
                or not isinstance(value["created_at"], str)
                or not isinstance(value["task_id"], str)
                or not value["task_id"] or len(value["task_id"]) > 128
            ):
                raise InvalidCursor("Task cursor is invalid.")
            stamp = value["created_at"]
            if not stamp.endswith("Z"):
                raise InvalidCursor("Task cursor is invalid.")
            datetime.fromisoformat(stamp[:-1] + "+00:00")
            return stamp, value["task_id"]
        except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
            if isinstance(exc, InvalidCursor):
                raise
            raise InvalidCursor("Task cursor is invalid.") from exc

    def _utc_now(self) -> str:
        return _utc_now()


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
