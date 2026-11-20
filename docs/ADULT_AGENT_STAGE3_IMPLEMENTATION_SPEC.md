# Adult Agent — Stage 3 Implementation Specification
## Operational SQLite Schema and AdultTaskStore

**Status:** Execution contract for Stage 3  
**Branch:** feature/adult-agent-mvp  
**Parent specification:** docs/ADULT_AGENT_IMPLEMENTATION_SPEC.md  
**Precondition:** Stage 2 memory migration/isolation gate is PASS  
**Scope:** Adult operational database, task/step/checkpoint/event persistence, state machine, leases, and recovery  
**Explicit exclusion:** No model/provider calls, encryption implementation, AttemptTracker, Executor, Supervisor, router, or Adult MCP registration

---

## 1. Stage objective and completion boundary

Stage 3 creates the durable operational foundation for Adult Agent. It implements the Adult-specific SQLite database and the transactional TaskStore only.

At completion:

1. Adult task state is stored in .data/adult.sqlite3, separate from the shared memory database.
2. Schema changes are versioned and idempotent.
3. Task creation, first step creation, initial checkpoint, and creation event are atomic.
4. State transitions are validated against one explicit transition graph and protected against stale writers.
5. A single active-task lease is enforced both by SQL constraints and transactional lease operations.
6. Checkpoints are versioned, append-only, size-bounded, and contain non-sensitive execution metadata only.
7. Lease renewal, release, expiration recovery, and cancellation are durable and restart-safe.
8. Task history is not silently deleted; no application delete API exists.
9. Unit tests prove concurrency, rollback, restart, and integrity behavior.
10. Existing Workspace and Stage 2 memory behavior remain unchanged.

Stage 3 does not execute a model or provider request. It does not implement encrypted payload/artifact storage (Stage 4), AttemptTracker (Stage 5), capability registry/router (Stage 6), text/image pipelines (Stages 7–8), or bounded execution supervision (Stage 9).

## 2. Preconditions, dependencies, and protected boundaries

Required precondition: every Stage 2 acceptance test and the existing full unit suite must be green. Stage 3 must use Adult's private operational database and must not bypass AdultMemory's fixed scope.

Configuration:

- ADULT_STATE_DB defaults to .data/adult.sqlite3.
- No connection is made to WORKSPACE_MEMORY_DB for task-state persistence.
- Stage 3 does not require OmniRoute credentials, API keys, encryption keys, model installations, or image backends.
- No provider/network call is permitted from AdultTaskStore.

Protected boundaries:

- Do not inspect, edit, replace, delete, or stage .vscode/.
- Do not read, edit, replace, or stage .env or credentials.
- Do not alter .data/memory.sqlite3 or any existing runtime database.
- Do not change agent/plan_store.py or broaden its allowlist.
- Do not add Adult tools to the Universal MCP Gateway in this stage.
- Do not merge to main.

## 3. Package ownership and public contracts

### 3.1 AdultTaskStore — agent/adult/adult_task_store.py

AdultTaskStore owns only the operational database. It contains no provider-specific code, policy decisions based on model output, encrypted file operations, or inference.

Contract:

    class AdultTaskStore:
        def __init__(self, db_path: str | Path | None = None): ...

        def create_task(
            self, request: AdultTaskRequest, budget: TaskBudget
        ) -> AdultTaskSnapshot: ...

        def get_task(self, task_id: str) -> AdultTaskSnapshot | None: ...

        def get_task_request(self, task_id: str) -> AdultTaskRequest | None: ...

        def list_tasks(
            self, status: str | None = None, limit: int = 20,
            cursor: str | None = None,
        ) -> TaskPage: ...

        def claim_task(
            self, task_id: str, worker_id: str, lease_seconds: int = 30
        ) -> TaskLease: ...

        def renew_lease(
            self, lease: TaskLease, lease_seconds: int = 30
        ) -> TaskLease: ...

        def release_lease(
            self, lease: TaskLease, expected_state_version: int,
            next_status: str = "checkpointed",
            reason: Literal["worker_released","yielded","cancel_observed","budget_exhausted"] = "worker_released",
        ) -> AdultTaskSnapshot: ...

        def save_step(
            self, task_id: str, step: StepRecord, lease: TaskLease,
            expected_state_version: int,
        ) -> StepRecord: ...

        def transition(
            self, task_id: str, expected_status: set[str], new_status: str,
            event_type: str, expected_state_version: int,
            metadata: dict | None = None, lease: TaskLease | None = None,
        ) -> AdultTaskSnapshot: ...

        def save_checkpoint(
            self, task_id: str, snapshot: CheckpointSnapshot,
            reason: CheckpointReason, expected_state_version: int,
            lease: TaskLease | None = None,
        ) -> Checkpoint: ...

        def set_waiting_user(
            self, task_id: str, snapshot: CheckpointSnapshot,
            lease: TaskLease, expected_state_version: int,
        ) -> Checkpoint: ...

        def resume(self, task_id: str, decision: Decision) -> AdultTaskSnapshot: ...

        def request_cancel(self, task_id: str) -> AdultTaskSnapshot: ...

        def recover_expired_leases(self) -> int: ...

        def get_latest_checkpoint(self, task_id: str) -> Checkpoint | None: ...

        def list_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]: ...

The methods above are the only application-level persistence API for task lifecycle. Internal SQL helpers are private. Do not expose a generic SQL, table, path, database, status-force, or namespace operation.

### 3.2 Required DTO contracts — agent/adult/models.py

All DTOs are immutable or treated as immutable snapshots. Validate all external values before constructing them.

AdultTaskRequest:
- task_type: one of text.analyze, text.generate, text.rewrite, image.analyze, image.generate, image.edit.
- input_fingerprint: non-empty keyed fingerprint; never the original text or image bytes.
- instruction_fingerprint: optional keyed fingerprint; never the original instruction.
- payload_id: optional opaque payload reference; Stage 3 stores no payload bytes.
- retention_policy: ephemeral or encrypted.
- requested_options: operation-specific JSON-safe object, maximum 8 KiB encoded; unknown keys fail closed. Stage 3's accepted key set is empty until a later operation stage defines typed keys; no secrets, arbitrary endpoint/path values, raw content, or unbounded nested objects.
- task_id is generated by the store; callers do not choose a database key.
- get_task_request() reconstructs safe request metadata from the task row for the internal application service; it never returns original text, image bytes, or raw instructions.

TaskBudget:
- max_total_attempts: integer 1–20.
- max_wall_seconds: integer 1–600.
- max_provider_calls: integer 0–20.
- max_cost_microusd: non-negative integer within the configured application ceiling; Stage 3 default is 0.
- failure_checkpoint_interval: exactly 5. Any other value is rejected.

AdultTaskSnapshot:
- task_id, task_type, status, created_at, updated_at.
- state_version, current_step_id.
- attempt_count (mapped from adult_tasks.total_attempts), failed_attempts_since_approval, provider_calls, approvals_granted.
- max_total_attempts, max_wall_seconds, max_provider_calls, max_cost_microusd, spent_cost_microusd, deadline_at.
- selected_adapter/provider/model/tool identifiers when present.
- latest_result_artifact_id when present.
- last_error_code, user_action_required, cancel_requested.
- Never include input text, original instruction, image bytes/base64, API keys, payload bytes, database paths, or local absolute paths.

StepRecord:
- step_id, task_id, sequence_no, operation, status.
- Optional adapter/provider/model/tool identifiers and payload/artifact opaque IDs.
- Optional non-secret config fingerprint and opaque payload/artifact references.
- started_at/completed_at are output fields assigned by TaskStore; callers must omit them on writes and any caller-supplied timestamp is rejected.
- No raw input/output body.

CheckpointSnapshot:
- schema_version: positive integer.
- task_id, task_type, status, active_step_id.
- input fingerprint and opaque payload reference only.
- current plan represented as bounded identifiers and step references.
- completed/pending step IDs.
- last successful artifact reference and selected route/tool identifiers when known.
- attempted approach keys and exclusions when later stages supply them.
- attempt/failure/provider-call/cost counters and remaining budget values.
- last normalized error code and sanitized summary.
- pending user action and allowed decisions.
- No original prompt, image bytes, unredacted provider response, credentials, absolute path, or arbitrary object graph.

TaskLease:
- task_id, worker_id, lease_generation, expires_at, state_version_at_grant.
- lease_generation is a fencing counter. A stale generation can never renew, release, or mutate the task after a newer claim.
- state_version_at_grant records the task version returned by claim_task; it is a convenience for the first guarded mutation, not a substitute for lease_generation.
- Every state-changing method requires the latest expected_state_version. After a mutation increments task state_version, callers must use the returned AdultTaskSnapshot or call get_task() before the next guarded mutation.
- Do not treat a worker ID alone as proof of lease ownership.

TaskLease is an immutable value object with the fields above. Its constructor is internal to AdultTaskStore; callers cannot choose lease_generation or manufacture a valid lease.

CheckpointReason is one of initial, step_completed, approval_required, manual_yield, lease_recovered, needs_input, paused, or cancelled. Checkpoint reasons are fixed enum values, never caller-provided free text.
Lease release reason is a separate fixed Literal: worker_released, yielded, cancel_observed, or budget_exhausted. Arbitrary free-form release reasons are rejected.

Checkpoint:
- checkpoint_id, task_id, revision, reason, schema_version, failed_attempts_since_approval, user_action_required, created_at.
- It may expose the validated snapshot only to internal application code; MCP output never returns snapshot_json.

TaskEvent:
- event_id, task_id, event_type, from_status, to_status, metadata, created_at.
- metadata is parsed from the validated bounded JSON object; no raw payload or arbitrary exception strings.

TaskPage:
- items: list of AdultTaskSnapshot.
- next_cursor: opaque cursor or null.
- has_more: boolean.

### 3.3 Error types

Use stable typed errors (or a common base carrying stable error_code):

- TaskNotFound
- InvalidTaskTransition
- StaleTaskVersion
- TaskAlreadyActive
- LeaseNotAvailable
- LeaseLost
- InvalidCheckpoint
- InvalidTaskBudget
- TaskBudgetExhausted
- InvalidCursor
- TaskTerminal
- UserApprovalRequired
- TaskCancelled

Do not leak SQL text, stack traces, local paths, raw values, or serialized database rows in external error messages.

## 4. State machine contract

### 4.1 Status definitions

| Status | Meaning |
|---|---|
| queued | Accepted and awaiting an explicit bounded run call |
| analyzing | A worker has claimed the task and is preparing its execution plan |
| running | A bounded execution step is active |
| evaluating | A produced result is being validated |
| checkpointed | Durable progress exists; no worker lease is active |
| waiting_user | Five eligible failed attempts since last approval; explicit user decision required |
| needs_input | Required ephemeral input is unavailable and must be supplied again |
| paused | Non-terminal stop requiring an explicit corrective action |
| completed | Accepted output was committed by later pipeline stages |
| failed | Terminal failure or exhausted hard budget |
| cancelled | Cancellation completed; no future provider dispatch is permitted |

Terminal states are completed, failed, and cancelled. No transition out of a terminal state is permitted.

### 4.2 Allowed transitions

| From | Allowed destinations | Notes |
|---|---|---|
| queued | analyzing, cancelled, failed | Claim or cancel |
| analyzing | running, checkpointed, waiting_user, needs_input, paused, failed, cancelled | Plan and execution lifecycle |
| running | evaluating, checkpointed, waiting_user, needs_input, paused, failed, cancelled | Bounded work |
| evaluating | completed, running, checkpointed, paused, failed, cancelled | Later evaluator result |
| checkpointed | queued, analyzing, cancelled, paused, needs_input | Explicit re-entry only |
| waiting_user | queued, cancelled | Continue requires explicit approval |
| needs_input | queued, cancelled | Only after replacement payload has been validated by the payload layer |
| paused | queued, cancelled, failed | Resume only after a defined correction |
| completed | none | Terminal |
| failed | none | Terminal |
| cancelled | none | Terminal |

TaskStore validates this graph on every state mutation. No public method accepts a force flag or bypasses transition validation. An empty expected_status set is invalid.

Stage 3 may exercise transitions using tests and internal service calls. It does not initiate model or provider execution.

### 4.3 State invariants

- queued, checkpointed, waiting_user, needs_input, and paused tasks must not retain a live lease.
- analyzing, running, and evaluating tasks require a non-null lease owner, expiry, and positive lease_generation.
- Only one task may be in analyzing/running/evaluating at a time in this single-user MVP.
- waiting_user requires user_action_required=1 and failed_attempts_since_approval=5.
- waiting_user is invalid when no total attempts/provider-call/time budget remains; the task must instead be failed.
- completed, failed, and cancelled tasks have no live lease.
- completed/failed/cancelled cannot be resumed.
- cancel_requested=1 permits only the transition to cancelled; it prevents transitions to queued/analyzing/running/evaluating/checkpointed/waiting_user and prevents a new provider dispatch. Later Executor/Supervisor stages must check it immediately before dispatch.
- A new resume call never dispatches work by itself. It only updates durable state; later Supervisor code performs a bounded run after the caller explicitly invokes the run/resume application operation.

## 5. SQLite database and exact Stage 3 schema

### 5.1 Database settings

- Path: ADULT_STATE_DB, default .data/adult.sqlite3.
- Keep separate from .data/memory.sqlite3.
- Create the parent directory if absent.
- Use a 10-second connection/busy timeout.
- Set sqlite3 isolation_level=None so the repository controls transaction boundaries explicitly.
- Enable PRAGMA foreign_keys=ON and verify it on every connection.
- Set a bounded busy timeout.
- Require SQLite 3.38.0 or later for built-in JSON validation functions and expression/partial index behavior.
- Keep journal_mode=DELETE. Do not enable WAL in Stage 3.
- Use parameterized SQL only.
- Use explicit BEGIN IMMEDIATE transactions for every multi-row state mutation.
- No transaction remains open during model/provider/network operations.
- On POSIX, newly created database files should be mode 0600. Do not recursively chmod the existing .data directory or other runtime files.

### 5.2 Migration ledger

    CREATE TABLE adult_schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      applied_at TEXT NOT NULL
    );

Stage 3 migration version 1 creates only the four operational tables below plus indexes and immutability triggers. The migration ledger is authoritative; do not also use an independently writable user_version counter.

Migration process:

1. Open connection; set row factory, foreign_keys=ON, busy_timeout.
2. BEGIN IMMEDIATE.
3. Read adult_schema_migrations.
4. If version 1 exists, validate every expected table/index/trigger and return without mutation.
5. If no version exists, create schema objects with individual execute calls. Do not use executescript inside the transaction.
6. Insert version 1 only after every schema object has been created successfully.
7. Commit. Roll back on any exception.
8. Re-open and run integrity_check and foreign_key_check in tests.

### 5.3 adult_tasks

    CREATE TABLE adult_tasks (
      task_id TEXT PRIMARY KEY,
      task_type TEXT NOT NULL CHECK(task_type IN (
        'text.analyze','text.generate','text.rewrite',
        'image.analyze','image.generate','image.edit'
      )),
      status TEXT NOT NULL CHECK(status IN (
        'queued','analyzing','running','evaluating','checkpointed',
        'waiting_user','needs_input','paused','completed','failed','cancelled'
      )),
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
      CHECK(
        (status IN ('analyzing','running','evaluating')
          AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
          AND lease_generation > 0)
        OR
        (status NOT IN ('analyzing','running','evaluating')
          AND lease_owner IS NULL AND lease_expires_at IS NULL)
      ),
      CHECK(
        (status IN ('waiting_user','needs_input','paused') AND user_action_required = 1)
        OR (status NOT IN ('waiting_user','needs_input','paused') AND user_action_required = 0)
      ),
      CHECK(failed_attempts_since_approval BETWEEN 0 AND 5),
      CHECK(total_attempts <= max_total_attempts),
      CHECK(provider_calls <= max_provider_calls),
      CHECK(
        status <> 'waiting_user'
        OR (failed_attempts_since_approval = 5 AND total_attempts >= 5)
      ),
      CHECK(status <> 'cancelled' OR cancel_requested = 1),
      CHECK(cancel_requested = 0 OR status IN ('analyzing','running','evaluating','cancelled'))
    );

payload_id and latest_result_artifact_id are opaque references only in Stage 3. They have no foreign key until the Stage 4 payload/artifact schema is implemented. They must never contain paths or file contents.

### 5.4 adult_steps

    CREATE TABLE adult_steps (
      step_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      sequence_no INTEGER NOT NULL CHECK(sequence_no >= 0),
      operation TEXT NOT NULL CHECK(operation IN (
        'intake','text.analyze','text.generate','text.rewrite',
        'image.analyze','image.generate','image.edit','evaluate'
      )),
      status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','skipped')),
      adapter_id TEXT,
      provider_id TEXT,
      model_id TEXT,
      tool_id TEXT,
      input_payload_id TEXT,
      output_artifact_id TEXT,
      config_fingerprint TEXT,
      started_at TEXT,
      completed_at TEXT,
      UNIQUE(task_id, sequence_no)
    );

A step identifier is generated by trusted application code. Operation and status are bounded values; no caller-supplied SQL or executable command is accepted. Steps may be updated through validated state transitions but never deleted through application APIs.

### 5.5 adult_checkpoints

    CREATE TABLE adult_checkpoints (
      checkpoint_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      revision INTEGER NOT NULL CHECK(revision >= 1),
      reason TEXT NOT NULL CHECK(reason IN (
        'initial','step_completed','approval_required','manual_yield',
        'lease_recovered','needs_input','paused','cancelled'
      )),
      schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
      snapshot_json TEXT NOT NULL
        CHECK(json_valid(snapshot_json) AND json_type(snapshot_json)='object'
          AND length(CAST(snapshot_json AS BLOB)) <= 65536),
      failed_attempts_since_approval INTEGER NOT NULL CHECK(failed_attempts_since_approval >= 0),
      user_action_required INTEGER NOT NULL DEFAULT 0 CHECK(user_action_required IN (0,1)),
      created_at TEXT NOT NULL,
      UNIQUE(task_id, revision)
    );

Checkpoint requirements:

- Maximum encoded snapshot size: 64 KiB.
- Validate schema_version and all allowed keys before serialization.
- Persist only non-sensitive state described in CheckpointSnapshot.
- Reject raw input, raw instruction, image base64/bytes, provider request/response bodies, API keys, authorization headers, and absolute filesystem paths.
- Each checkpoint is immutable. New checkpoints increment revision; never update an earlier snapshot.
- Revision allocation and insertion occur within BEGIN IMMEDIATE.
- A checkpoint must never overwrite a checkpoint with a higher revision.

### 5.6 adult_task_events

    CREATE TABLE adult_task_events (
      event_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      event_type TEXT NOT NULL CHECK(event_type IN (
        'task_created','task_claimed','lease_renewed','lease_released',
        'step_created','step_transition','state_transition','checkpoint_saved',
        'waiting_user','task_resumed','cancel_requested','task_cancelled',
        'lease_expired_recovered','cancel_recovered','budget_exhausted'
      )),
      from_status TEXT,
      to_status TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'
          AND length(CAST(metadata_json AS BLOB)) <= 8192),
      created_at TEXT NOT NULL
    );

Event rules:

- event_type is one of task_created, task_claimed, lease_renewed, lease_released, step_created, step_transition, state_transition, checkpoint_saved, waiting_user, task_resumed, cancel_requested, task_cancelled, lease_expired_recovered, cancel_recovered, or budget_exhausted.
- metadata_json must be a JSON object no larger than 8 KiB.
- Only bounded identifiers, normalized error codes, counters, lease generation, and state-version metadata are allowed.
- No raw prompt, image, content, provider body, credentials, secret, or absolute path.
- Events are append-only. No application update/delete method exists.
- Task events are not a debug log and must not contain free-form untrusted exception strings.

### 5.7 Indexes and global invariants

    CREATE INDEX idx_adult_tasks_status_updated
      ON adult_tasks(status, updated_at DESC);

    CREATE UNIQUE INDEX idx_adult_single_active_task
      ON adult_tasks((1))
      WHERE status IN ('analyzing','running','evaluating');

    CREATE INDEX idx_adult_steps_task_sequence
      ON adult_steps(task_id, sequence_no);

    CREATE INDEX idx_adult_checkpoints_task_revision
      ON adult_checkpoints(task_id, revision DESC);

    CREATE INDEX idx_adult_events_task_created
      ON adult_task_events(task_id, created_at);

The partial unique index is the final database-level guard for the one-active-task rule. TaskStore must still return a typed TaskAlreadyActive error rather than expose a raw SQLite IntegrityError.

Required immutability triggers:

    CREATE TRIGGER prevent_adult_tasks_delete
    BEFORE DELETE ON adult_tasks
    BEGIN SELECT RAISE(ABORT, 'adult task history is immutable'); END;

    CREATE TRIGGER prevent_adult_steps_delete
    BEFORE DELETE ON adult_steps
    BEGIN SELECT RAISE(ABORT, 'adult step history is immutable'); END;

    CREATE TRIGGER validate_adult_steps_update
    BEFORE UPDATE ON adult_steps
    WHEN
      NEW.step_id IS NOT OLD.step_id
      OR NEW.task_id IS NOT OLD.task_id
      OR NEW.sequence_no IS NOT OLD.sequence_no
      OR NEW.operation IS NOT OLD.operation
      OR (OLD.adapter_id IS NOT NULL AND NEW.adapter_id IS NOT OLD.adapter_id)
      OR (OLD.provider_id IS NOT NULL AND NEW.provider_id IS NOT OLD.provider_id)
      OR (OLD.model_id IS NOT NULL AND NEW.model_id IS NOT OLD.model_id)
      OR (OLD.tool_id IS NOT NULL AND NEW.tool_id IS NOT OLD.tool_id)
      OR (OLD.input_payload_id IS NOT NULL AND NEW.input_payload_id IS NOT OLD.input_payload_id)
      OR (OLD.output_artifact_id IS NOT NULL AND NEW.output_artifact_id IS NOT OLD.output_artifact_id)
      OR (OLD.config_fingerprint IS NOT NULL AND NEW.config_fingerprint IS NOT OLD.config_fingerprint)
      OR (OLD.started_at IS NOT NULL AND NEW.started_at IS NOT OLD.started_at)
      OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
      OR NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'pending' AND NEW.status IN ('running','skipped'))
        OR (OLD.status = 'running' AND NEW.status IN ('succeeded','failed'))
      )
    BEGIN SELECT RAISE(ABORT, 'invalid adult step mutation'); END;

    CREATE TRIGGER prevent_adult_checkpoints_update
    BEFORE UPDATE ON adult_checkpoints
    BEGIN SELECT RAISE(ABORT, 'adult checkpoints are append-only'); END;

    CREATE TRIGGER prevent_adult_checkpoints_delete
    BEFORE DELETE ON adult_checkpoints
    BEGIN SELECT RAISE(ABORT, 'adult checkpoints are append-only'); END;

    CREATE TRIGGER prevent_adult_task_events_update
    BEFORE UPDATE ON adult_task_events
    BEGIN SELECT RAISE(ABORT, 'adult task events are append-only'); END;

    CREATE TRIGGER prevent_adult_task_events_delete
    BEFORE DELETE ON adult_task_events
    BEGIN SELECT RAISE(ABORT, 'adult task events are append-only'); END;

The trigger DDL is part of Stage 3 migration version 1 and must be included in the idempotent schema validation.

### 5.8 History preservation

The four Stage 3 tables are operational history. No TaskStore method deletes tasks, steps, checkpoints, or events.

Use ON DELETE RESTRICT, never ON DELETE CASCADE. Add database triggers that reject DELETE against adult_tasks, adult_checkpoints, and adult_task_events. Do not create a task purge operation in this stage. A future retention/purge feature requires a separate specification defining user confirmation, export/backup, audit, and dependency cleanup.

## 6. Transactional behavior

All methods that mutate more than one row use BEGIN IMMEDIATE and commit or roll back as one unit. All writes update updated_at and state_version where applicable and append an event in the same transaction.

State-version return contract:
- create_task, get_task, list_tasks, transition, release_lease, resume, request_cancel return snapshots containing the authoritative current state_version.
- claim_task returns TaskLease.state_version_at_grant. The caller may use that value for its first guarded mutation.
- renew_lease updates only lease expiry and event history; it does not increment task state_version.
- save_step, save_checkpoint, and set_waiting_user return their resource records and increment task state_version. The caller must call get_task() before the next state-changing operation to obtain the new expected version.
- recover_expired_leases increments state_version for every recovered task; callers must reload snapshots before further mutation.

### 6.1 create_task

Input validation:

- Validate task_type, fingerprints, retention policy, budget integers, max limits, and requested_options.
- Serialize requested_options into adult_tasks.request_options_json after schema validation; this is the only persisted form of user-selected non-sensitive operation options.
- failure_checkpoint_interval must equal five.
- input_fingerprint must not contain raw input; it is a keyed fingerprint generated by the application layer.
- Reject raw text/image/prompt fields if accidentally passed to the persistence DTO.

Transaction:

1. Generate UUID task_id and UUID step_id.
2. Set status=queued, counters=0, user_action_required=0, cancel_requested=0.
3. Set deadline_at from created_at + max_wall_seconds in UTC.
4. Insert adult_tasks row, including validated request_options_json.
5. Insert initial sequence_no=0 step with operation=intake and status=pending.
6. Set current_step_id to the intake step.
7. Insert initial checkpoint revision=1 with a sanitized schema-versioned snapshot.
8. Insert task_created event.
9. Commit and return the safe AdultTaskSnapshot.

Any failure in steps 4–8 rolls back the entire creation; no partial task/step/checkpoint remains.

### 6.2 claim_task and lease fencing

claim_task accepts only queued or checkpointed tasks with no live lease and no cancellation request.

Within BEGIN IMMEDIATE:

1. Read current task and state_version.
2. Reject missing, terminal, or cancelled task. If deadline_at has passed or total_attempts has reached max_total_attempts, atomically transition the task to failed, clear lease fields, append a budget_exhausted event, commit, and then raise the typed TaskBudgetExhausted error; do not return a lease or claim the task. A provider-call limit of zero may still permit a later verified local route; provider-call and cost limits are enforced by the Supervisor immediately before a provider dispatch, not guessed by TaskStore.
3. Confirm no other active task exists.
4. Increment lease_generation and state_version.
5. Set status=analyzing, lease_owner, and lease_expires_at.
6. Append task_claimed event.
7. Commit and return TaskLease with the new lease_generation. The current state_version is returned in AdultTaskSnapshot and is required by the next mutating call.

lease_seconds must be an integer from 1 through 300. Reject bool values even though bool is a Python int subclass.

renew_lease requires matching task_id, worker_id, lease_generation and a non-expired lease. It may extend expiry only for the current owner/generation and must reject cancel_requested=1. A stale lease raises LeaseLost. Renewing does not alter the lease_generation or task state_version; it changes only lease_expires_at and appends a lease_renewed event in one transaction.

release_lease requires matching current owner/generation and a valid next status from the transition graph. It clears lease_owner/lease_expires_at and appends an event atomically. Default next_status is checkpointed. The task must have a durable checkpoint before it is released to checkpointed. If cancel_requested=1, the only permitted release destination is cancelled; it must never return to checkpointed/queued.

A lease_generation is a fencing token: after lease recovery or a new claim, any older TaskLease is permanently invalid even if worker_id is reused.

### 6.3 save_step

- Requires a valid active lease for the task.
- Requires the current task state to allow step work.
- sequence_no must be non-negative and unique per task.
- operation is one of intake, text.analyze, text.generate, text.rewrite, image.analyze, image.generate, image.edit, or evaluate.
- A new step is inserted once; an existing step may only advance through pending -> running -> succeeded/failed or pending -> skipped.
- step_id, task_id, sequence_no, and operation are immutable after insertion. Route/config/payload/artifact identifiers may be set once when known and cannot be overwritten with a different value.
- Terminal step statuses cannot be changed or reopened.
- started_at/completed_at are server-generated UTC timestamps, not caller timestamps.
- Increment the parent task state_version and updated_at, and append a step_created/step_transition event atomically.
- The operation and route identifiers are metadata only; they are not executed by TaskStore.

### 6.4 transition

- Validate new_status against the explicit state graph.
- Validate current state is in expected_status.
- Require expected_state_version to be an integer and exactly equal to the current state_version; there is no unguarded state-transition mode.
- Active-state transitions require the current valid lease unless the method is an explicitly defined cancellation/recovery path.
- Update status, user_action_required, cancel_requested and lease fields consistently with state invariants. If cancel_requested=1, only cancelled is an allowed destination.
- Append an event in the same transaction.
- Raise StaleTaskVersion or InvalidTaskTransition rather than applying a stale write.
- No caller can force an arbitrary status by passing a set containing all states.

### 6.5 save_checkpoint

- Validate task ownership, schema version, allowed JSON shape, maximum size, and non-sensitive content.
- If the task is in analyzing/running/evaluating, a matching non-expired TaskLease is mandatory. For non-active states, lease must be omitted and the expected state_version is still mandatory.
- Start BEGIN IMMEDIATE and read latest checkpoint revision.
- Allocate revision = previous revision + 1.
- Insert the immutable checkpoint.
- Update the task's state_version/current_step_id/updated_at only if expected_state_version still matches.
- Append checkpoint_saved event.
- Commit all three operations together.

A stale writer cannot insert a later checkpoint based on an old task snapshot. If optimistic concurrency validation fails, roll back the checkpoint insert.

### 6.6 set_waiting_user

This operation must atomically:

1. Verify the supplied lease is current and valid.
2. Require expected_state_version to match the current task row exactly.
3. Verify failed_attempts_since_approval is exactly five.
3. Verify some total execution budget remains; otherwise transition to failed instead of waiting.
4. Write a new checkpoint with user_action_required=1.
5. Set status=waiting_user and user_action_required=1.
6. Clear lease owner/expiry.
7. Append waiting_user event.
8. Commit.

No sixth attempt can be dispatched after this state is committed. Stage 3 persists the invariant; Stage 5/9 will provide the actual attempt counting and Supervisor dispatch enforcement.

### 6.7 resume

- Execute under BEGIN IMMEDIATE and re-read the current status inside the transaction; do not rely on a pre-transaction snapshot.
- decision is exactly continue or cancel.
- cancel delegates to request_cancel and never dispatches a provider call.
- continue from waiting_user requires user_action_required=1 and exactly five failures; verify that deadline, attempt, provider-call, and cost budgets still permit work. If no budget remains, transition to failed without dispatch. Otherwise transition to queued, increment approvals_granted, reset failed_attempts_since_approval=0, clear user_action_required, and write a resume event.
- continue from checkpointed transitions to queued without incrementing approvals_granted.
- If cancel_requested=1, resume is rejected; cancellation has precedence and cannot be reversed by a stale continue request.
- paused is rejected by TaskStore.resume in Stage 3. A later application layer may add a specific corrective-resume contract only after defining and validating the corrective action; do not expose a generic public bypass.
- needs_input cannot be resumed by TaskStore alone. Stage 4 must first validate and attach a replacement payload; until then, reject continue.
- completed, failed, and cancelled are terminal.
- resume changes durable state only. It does not claim a task lease or execute a step.

### 6.8 request_cancel

- For queued/checkpointed/waiting_user/paused/needs_input tasks, atomically set status=cancelled, cancel_requested=1, user_action_required=0, clear any stale lease fields, and append task_cancelled.
- For analyzing/running/evaluating tasks, set cancel_requested=1 and append cancel_requested, but leave the lease/state intact until the active worker observes cancellation and terminates safely. Repeating the same request is idempotent and must not append duplicate cancellation events.
- Subsequent claim/renew/dispatch is rejected when cancel_requested=1.
- The later Executor must discard/unpublish any provider output returned after cancellation was requested.

### 6.9 recover_expired_leases

- Find tasks in analyzing/running/evaluating with lease_expires_at <= current UTC time.
- For each task, in a serialized transaction, verify the lease is still expired and the generation has not changed.
- Create a recovery checkpoint from the latest known safe snapshot.
- If cancel_requested=0, transition the task to checkpointed. If cancel_requested=1, transition it to cancelled and retain cancel_requested=1; never make a cancelled request resumable.
- Clear lease owner/expiry, increment state_version and lease_generation, and append lease_expired_recovered or cancel_recovered event as appropriate.
- Return the number of recovered tasks.
- Do not retry or re-dispatch any provider request. Later AttemptTracker integration must inspect an in-progress attempt and classify its outcome as timed_out or unknown_outcome before any retry.
- Re-running recovery is idempotent; already recovered tasks are not counted again.

## 7. Cursor, time, JSON, and storage rules

### Cursor

- list_tasks order is created_at DESC, task_id DESC.
- Cursor is an opaque versioned base64url JSON tuple containing only the last created_at and task_id values.
- Reject malformed, oversized (>1024 decoded bytes), unsupported-version, or inconsistent cursors with InvalidCursor.
- A cursor is not an authorization token and grants no access.
- limit defaults to 20; accepted range is 1–50. Reject bool and non-integer values.

### Timestamps

- Generate timestamps in the application using timezone-aware UTC.
- Serialize using one normalized ISO-8601 representation with UTC offset Z and fixed precision.
- Never accept caller-provided timestamps for state transitions, leases, checkpoints, or events.
- Compare parsed UTC datetimes, not naive local times.

### JSON validation

- Use JSON objects only; reject NaN/Infinity and unsupported types.
- Check maximum UTF-8 encoded size before database write.
- Validate keys against DTO schemas; do not serialize arbitrary dataclasses or objects.
- The event/checkpoint schema is versioned independently of SQLite schema migration version.

### Database integrity

- Enable foreign keys on every connection and test the PRAGMA value.
- Keep journal_mode=DELETE; no WAL changes in this stage.
- Run integrity_check and foreign_key_check in tests after migrations and injected failures.
- Do not perform an online migration on the live user database as part of unit tests.
- Database backup/restore behavior must be documented before first persistent user task state is created.

## 8. MCP boundary and Stage 3 exposure decision

Stage 3 is an internal persistence stage. It does not register an Adult MCP server, alter the Universal Gateway allowlist, or expose TaskStore methods as public tools.

The existing Workspace memory MCP contract remains unchanged by Stage 3.

The eventual Adult MCP contracts remain those in docs/ADULT_AGENT_IMPLEMENTATION_SPEC.md, but their implementation is staged:

| Future MCP tool | Stage 3 persistence mapping | Exposed in Stage 3? |
|---|---|---|
| adult_submit_text_task | AdultAgent validation -> AdultTaskStore.create_task | No |
| adult_submit_image_task | AdultAgent validation -> AdultTaskStore.create_task | No |
| adult_get_task | AdultTaskStore.get_task | No |
| adult_list_tasks | AdultTaskStore.list_tasks | No |
| adult_cancel_task | AdultTaskStore.request_cancel | No |
| adult_run_task | Requires Supervisor/Executor, Stage 9 | No |
| adult_get_attempts | Requires AttemptTracker/attempt schema, Stage 5 | No |
| adult_resume_task | Requires full resume orchestration; TaskStore only performs durable state update | No |
| adult_get_artifact_chunk | Requires encrypted artifact store, Stage 4 | No |

When Stage 11 later registers the server:

- Tool arguments remain strict objects with additionalProperties=false where supported.
- Caller cannot supply database path, SQL, lease owner, lease generation, state_version, namespace, principal, provider endpoint, or key.
- MCP output is a redacted snapshot; no prompt, image, payload bytes, checkpoint JSON, local absolute path, or credentials are returned.
- Tool calls use stable task_id values and are independent of MCP transport session lifetime.
- Adult tools remain absent when ENABLE_ADULT_AGENT is unset/false.

## 9. Stage 3 acceptance test matrix

All tests use temporary Adult database files. No test may use .data/adult.sqlite3.

### Migration and schema

- TASK-DB-01: new database creates migration ledger version 1 and all four required tables.
- TASK-DB-02: migration is idempotent and verifies schema objects on re-initialization.
- TASK-DB-03: injected migration failure rolls back tables/indexes/ledger.
- TASK-DB-04: foreign_keys is enabled on every connection.
- TASK-DB-05: integrity_check is ok and foreign_key_check is empty.
- TASK-DB-06: journal_mode remains DELETE.
- TASK-DB-07: CHECK constraints reject invalid task_type/status/retention/budget/boolean/counter values.
- TASK-DB-08: no extra tables for attempts, payloads, artifacts, or model capabilities are introduced by Stage 3.
- TASK-DB-09: required task lease/user-action/budget/counter CHECK constraints and all history immutability triggers exist and reject invalid writes/deletes.
- TASK-DB-10: event types, step operations, checkpoint reasons, malformed JSON, and JSON over-size limits are rejected by SQLite constraints as well as application validation.
- TASK-DB-11: cancel_requested is accepted only on active tasks or already-cancelled tasks; cancellation cannot leave a queued/checkpointed task in a resumable state.
- TASK-DB-14: waiting_user cannot be stored unless both the failure counter and lifetime attempt counter prove at least five failed attempts.
- TASK-DB-12: TaskStore rejects caller-supplied StepRecord timestamps and prevents identity/operation mutation or terminal step reopening.
- TASK-DB-13: the adult_steps update trigger rejects changes to immutable IDs/operation/assigned references and invalid status regressions.

### Task creation and persistence

- TASK-STORE-01: create_task creates a queued task, intake step, initial checkpoint, and creation event in one transaction.
- TASK-STORE-02: injected failure after each insert rolls back the entire task creation.
- TASK-STORE-03: task snapshot contains only safe metadata and fingerprints.
- TASK-STORE-04: reopening the store in a new process can retrieve the same task and checkpoint.
- TASK-STORE-05: invalid task DTO, unsupported task type, or invalid budget is rejected before writes.
- TASK-STORE-06: task IDs and step IDs are unique opaque UUIDs.
- TASK-STORE-07: no delete API exists; SQL delete triggers/foreign keys preserve history.
- TASK-STORE-08: requested_options round-trips through request_options_json using only the Stage 3 empty allowlist; unknown option keys are rejected.
- TASK-STORE-09: get_task_request recovers safe fingerprints/payload reference/options after restart and never returns original input or instruction bytes.

### State machine and concurrency

- TASK-STATE-01: every allowed transition is accepted.
- TASK-STATE-02: every disallowed transition and every transition out of a terminal state is rejected.
- TASK-STATE-03: stale expected status or state_version is rejected without partial writes.
- TASK-STATE-04: state transition and event append commit or roll back together.
- TASK-LEASE-01: exactly one task can hold the active status/lease at a time.
- TASK-LEASE-02: simultaneous claims from independent connections produce one winner.
- TASK-LEASE-03: a second worker cannot claim a task with an unexpired lease.
- TASK-LEASE-04: expired lease recovery checkpoints and releases the task exactly once.
- TASK-LEASE-05: a stale lease_generation cannot renew, release, or mutate after recovery/reclaim.
- TASK-LEASE-06: renewal after expiry fails.
- TASK-LEASE-07: cancellation prevents new claims and dispatch authorization.

### Checkpoints and event history

- TASK-CP-01: checkpoint revisions increase monotonically per task.
- TASK-CP-02: concurrent/stale checkpoint writes cannot overwrite or supersede newer state.
- TASK-CP-03: task state update, checkpoint insert, and event append are atomic.
- TASK-CP-04: oversized, malformed, wrong-version, or sensitive checkpoint data is rejected.
- TASK-CP-05: checkpoint data contains identifiers/fingerprints/references only, never prompt/image bytes or credentials.
- TASK-EVENT-01: event rows are append-only and bounded.
- TASK-EVENT-02: event metadata never stores raw text, images, provider request/response bodies, secrets, or local paths.
- TASK-EVENT-03: task history cannot be deleted through the API or database constraints.

### Resume, cancellation, and recovery

- TASK-RESUME-01: waiting_user + continue returns task to queued, increments approvals, resets failure counter, and does not dispatch work.
- TASK-RESUME-02: waiting_user + cancel transitions to cancelled and does not dispatch work.
- TASK-RESUME-03: checkpointed + continue returns to queued without incrementing approval count.
- TASK-RESUME-04: needs_input cannot resume without a validated replacement payload from the later payload layer.
- TASK-RESUME-05: completed/failed/cancelled tasks cannot resume.
- TASK-CANCEL-01: queued/non-running cancel becomes cancelled atomically.
- TASK-CANCEL-02: active cancel sets cancel_requested and is visible to the lease owner; a repeated cancel request is idempotent.
- TASK-RECOVERY-01: restart and expired-lease recovery preserve the latest checkpoint and history.
- TASK-RECOVERY-02: recovery never re-dispatches or assumes a provider call succeeded.
- TASK-RECOVERY-03: an expired lease with cancel_requested set becomes cancelled, never checkpointed/queued.

### Scope and no-provider-call guard

- TASK-SCOPE-01: AdultTaskStore writes only to ADULT_STATE_DB, never WORKSPACE_MEMORY_DB.
- TASK-SCOPE-02: Stage 3 imports no provider SDK/adapter as a side effect of store construction.
- TASK-SCOPE-03: unit tests prove no HTTP/network/provider call occurs during create/get/list/claim/checkpoint/recover.
- TASK-SCOPE-04: Gateway tool inventory and Workspace MCP tool inventory are unchanged.

- TASK-REG-01: every Stage 2 focused test remains green.
- TASK-REG-02: the complete existing unittest suite passes.

## 10. Stage 3 implementation sequence and gate

Implement in this order:

1. Add immutable DTOs and validation for task requests, budgets, snapshots, leases, steps, events, and checkpoints.
2. Add Adult database connection configuration and migration ledger.
3. Add the four Stage 3 tables, indexes, CHECK constraints, append-only triggers, and integrity validation.
4. Implement create_task/get_task/list_tasks and compatibility-safe snapshot mapping.
5. Implement the state transition graph and optimistic state_version checks.
6. Implement claim/renew/release with lease_generation fencing and the one-active-task SQL guard.
7. Implement checkpoint persistence, waiting_user transition, resume state updates, cancellation, and expired-lease recovery.
8. Add failure injection, concurrency, restart, and privacy tests.
9. Run Stage 3 focused tests, all Stage 2 memory tests, and the full suite from a fresh process.
10. Review diff, SQL invariants, protected-path scan, and GitHub CI jobs.

Stage 3 PASS requires every TASK-* acceptance test, all Stage 2 MEM-* tests, and the complete repository test suite to pass. The implementation must contain no provider call, no MCP registration, no payload plaintext persistence, no automatic retries, and no destructive history cleanup.

Stage 4 may begin only after Stage 3 PASS is recorded and reviewed.
