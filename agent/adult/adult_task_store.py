"""Adult Agent operational SQLite schema and migration foundation.

This module deliberately contains no provider, network, payload, or inference code.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final = 1
SCHEMA_NAME: Final = "adult_operational_store_v1"
DEFAULT_DB_PATH: Final = Path(".data") / "adult.sqlite3"
MIN_SQLITE_VERSION: Final = (3, 38, 0)
_BUSY_TIMEOUT_MS: Final = 10_000

class AdultTaskStoreError(Exception):
    """Base class for stable Adult task-store errors."""

class TaskStoreSchemaError(AdultTaskStoreError):
    """The operational database schema is missing or inconsistent."""

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

_EXPECTED_TABLES: Final = {
    "adult_schema_migrations", "adult_tasks", "adult_steps",
    "adult_checkpoints", "adult_task_events",
}
_EXPECTED_INDEXES: Final = {
    "idx_adult_tasks_status_updated", "idx_adult_single_active_task",
    "idx_adult_steps_task_sequence", "idx_adult_checkpoints_task_revision",
    "idx_adult_events_task_created",
}
_EXPECTED_TRIGGERS: Final = {
    "prevent_adult_tasks_delete", "prevent_adult_steps_delete",
    "validate_adult_steps_update", "prevent_adult_checkpoints_update",
    "prevent_adult_checkpoints_delete", "prevent_adult_task_events_update",
    "prevent_adult_task_events_delete",
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

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adult_schema_migrations'"
            ).fetchone()
            if exists:
                rows = connection.execute(
                    "SELECT version, name FROM adult_schema_migrations ORDER BY version"
                ).fetchall()
                if len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION or rows[0]["name"] != SCHEMA_NAME:
                    raise TaskStoreSchemaError("Unsupported Adult task database migration history.")
                self._validate_schema(connection)
                connection.commit()
                return
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO adult_schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (SCHEMA_VERSION, SCHEMA_NAME, _utc_now()),
            )
            self._validate_schema(connection)
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
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
        if tables != _EXPECTED_TABLES:
            raise TaskStoreSchemaError("Adult task database is missing required tables.")
        if not _EXPECTED_INDEXES.issubset(indexes):
            raise TaskStoreSchemaError("Adult task database is missing required indexes.")
        if not _EXPECTED_TRIGGERS.issubset(triggers):
            raise TaskStoreSchemaError("Adult task database is missing required triggers.")

    def _utc_now(self) -> str:
        return _utc_now()


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
