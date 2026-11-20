# Adult Agent — Stage 2 Implementation Specification
## Memory Migration, Scoped Access, and Namespace Isolation

**Status:** Execution contract for Stage 2  
**Branch:** feature/adult-agent-mvp  
**Parent specification:** docs/ADULT_AGENT_IMPLEMENTATION_SPEC.md  
**Scope:** Existing shared memory database only; no Adult task runtime or provider execution  
**Data safety:** Tests use temporary databases. The live .data/memory.sqlite3 is not migrated during development.

---

## 1. Stage objective and completion boundary

Stage 2 converts the existing single-namespace SQLite memory store into a versioned, scoped repository while preserving all existing Workspace behavior.

At completion:

1. Existing Workspace memories retain their IDs, content, tags, and timestamps.
2. Every legacy row is explicitly classified as namespace=workspace, visibility=internal, sensitivity=unknown, memory_type=legacy, owner_agent=workspace.
3. Workspace, Adult, and feedback publication use fixed application scopes. No caller can select a namespace or principal.
4. SQL queries enforce the fixed scope policy. Prompt instructions, tags, and post-query Python filtering are not authorization controls.
5. Adult can read its own private records and explicitly shareable Workspace records only.
6. Workspace cannot read Adult records.
7. The feedback publisher cannot read either namespace and can only write schema-validated sanitized feedback into Workspace shareable memory.
8. Access audit records contain operation/principal/namespace/memory ID/time only; never query text or memory content.
9. Existing MemoryStore and Workspace MCP behavior remains backward-compatible.
10. Migration, isolation, rollback, and regression tests pass before Stage 3 begins.

Stage 2 does not implement Adult operational tasks, checkpoints, payload encryption, attempt tracking, model routing, or provider calls.

## 2. Repository baseline and protected boundaries

Current implementation reviewed:

- agent/memory_store.py: SQLite table memories(id, content, tags, created_at), keyword recall, recent listing.
- agent/memory_mcp_server.py: remember, recall, recent_memories tools.
- agent/control_plane.py and agent/task_worker.py: Workspace-only reads through MemoryStore().recent().
- tests/test_memory_store.py: four existing compatibility tests.
- Existing DB observed during prior inspection: .data/memory.sqlite3, schema version not yet tracked, one legacy row, integrity_check=ok, foreign_key_check empty, journal_mode=delete.

Non-negotiable boundaries:

- Do not inspect, edit, replace, delete, or stage .vscode/.
- Do not read, edit, replace, or stage .env or credentials.
- Do not migrate the live memory database during development or unit tests.
- Do not enable WAL in this stage.
- Do not change agent/plan_store.py or its allowlist.
- Do not change the default Workspace MCP tool names or add a namespace argument.
- Do not merge this branch to main.

## 3. Package and class ownership

### 3.1 MemoryStore — agent/memory_store.py

MemoryStore owns the database path, connection creation, schema initialization/migration, and construction of the three fixed storage scopes. It is not a public principal factory.

Contract:

    class MemoryStore:
        def __init__(self, db_path: str | Path | None = None): ...
        def workspace_scope(self) -> ScopedMemoryStore: ...
        def adult_scope(self) -> ScopedMemoryStore: ...
        def feedback_publisher_scope(self) -> FeedbackPublisherStore: ...
        def remember(self, content: str, tags: list[str] | None = None) -> dict: ...
        def recall(self, query: str, limit: int = 5) -> list[dict]: ...
        def recent(self, limit: int = 10) -> list[dict]: ...

Rules:

- Existing direct construction MemoryStore() resolves to Workspace scope.
- The optional db_path remains supported for tests and existing callers.
- The environment default remains WORKSPACE_MEMORY_DB, then .data/memory.sqlite3.
- remember/recall/recent are compatibility wrappers over workspace_scope(); they do not create a privileged bypass.
- The compatibility wrappers preserve existing validation, ordering, and return shapes:
  - remember: id, created_at, tags.
  - recall: id, content, tags, created_at, matched_terms.
  - recent: id, content, tags, created_at.
- No constructor parameter, public method, environment variable, MCP argument, or public factory accepts a principal or namespace string.
- Scope objects are created only by the three fixed factory methods. Their constructors must not be exported as a supported application API.

### 3.2 ScopedMemoryStore — internal fixed-scope facade

Contract:

    class ScopedMemoryStore:
        def remember(
            self,
            content: str,
            tags: list[str] | None = None,
            memory_type: str = "fact",
            task_id: str | None = None,
            metadata: dict | None = None,
        ) -> MemoryRef: ...

        def recall(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent(self, limit: int = 10) -> list[MemoryRecord]: ...
        def recall_owned(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent_owned(self, limit: int = 10) -> list[MemoryRecord]: ...
        def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent_shareable_workspace(self, limit: int = 10) -> list[MemoryRecord]: ...

Rules:

- The object holds an immutable internally assigned principal and owned namespace. Callers cannot mutate either value.
- remember always writes to the scope's owned namespace, with visibility=internal and owner_agent fixed by the factory.
- Callers cannot set namespace, principal, visibility, owner_agent, or sensitivity.
- memory_type is a bounded enum: fact, strategy, outcome, lesson, preference, note, or legacy. The legacy value is reserved for migrated rows and cannot be assigned by new writes.
- task_id is optional, at most 128 characters, and validated as an opaque identifier; it is not a filesystem path.
- Existing content/tag validation remains: content is 1–10,000 characters; at most 20 tags, each at most 64 characters. New memory writes use sensitivity=unknown unless a trusted internal classifier assigns a more restrictive value. content_hash is optional and, when populated, must be an HMAC made with a dedicated derived key; do not store a plain SHA hash of private content.
- metadata must be a JSON object no larger than 4 KiB UTF-8 encoded, at most 3 levels deep, with no more than 32 keys per object and arrays of at most 50 scalar values. Keys must come from an application allowlist; reject raw prompt/content/image keys, arbitrary nested blobs, secrets, absolute paths, NaN/Infinity, and strings longer than 256 characters.
- recall/recent return only records permitted by the fixed scope policy.
- recall_owned/recent_owned return records from the scope's own namespace only.
- recall_shareable_workspace/recent_shareable_workspace are permitted only on the Adult scope and return Workspace rows with visibility=shareable. Calls from Workspace scope fail closed.
- No method accepts a namespace selector, SQL fragment, filter expression, principal, or caller-provided access policy.

### 3.3 FeedbackPublisherStore — constrained write-only interface

Do not implement feedback publication by handing the publisher a generic ScopedMemoryStore.remember() method. That would allow arbitrary text to be marked shareable.

Contract:

    class FeedbackPublisherStore:
        def publish_sanitized_feedback(self, feedback: SanitizedFeedback) -> MemoryRef: ...

Requirements:

- The instance is created only by MemoryStore.feedback_publisher_scope().
- It has no recall(), recent(), get(), raw SQL, or generic remember() method.
- SanitizedFeedback is a validated typed record with the fields defined in the parent spec's Feedback schema.
- The publisher revalidates allowed keys, lengths, category, verification_status, and content policy at the write boundary.
- It writes only to namespace=workspace, visibility=shareable, memory_type=lesson, owner_agent=feedback_publisher.
- It never accepts raw task text, image bytes, full provider bodies, secrets, local paths, or arbitrary metadata.
- Its SQL write is authorized by the fixed feedback_publisher ACL row and validated feedback schema.

### 3.4 AdultMemory — agent/adult/adult_memory.py

Contract:

    class AdultMemory:
        def __init__(self, store: ScopedMemoryStore): ...
        def remember_private(
            self, content: str, memory_type: str,
            task_id: str | None = None, metadata: dict | None = None,
        ) -> MemoryRef: ...
        def recall_private(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...

Construction is internal composition: the Adult application receives MemoryStore().adult_scope(). AdultMemory must reject a Workspace scope or feedback publisher object. It never accepts namespace or principal values and never stores raw task prompts or image bytes by default.

### 3.5 Workspace memory MCP contract

The existing Workspace MCP server keeps exactly these tools and argument contracts:

    remember(content: str, tags: list[str] | None = None) -> dict
    recall(query: str, limit: int = 5) -> list[dict]
    recent_memories(limit: int = 10) -> list[dict]

The server binds only the Workspace scope. It must not add namespace, principal, visibility, owner_agent, sensitivity, SQL, path, or ACL arguments. Existing tool names, descriptions, validation, and result projections remain compatible. No Adult MCP tool is registered in Stage 2.

## 4. SQLite schema and migration contract

### 4.1 Existing table: additive-only change

The existing memories table must not be dropped, renamed, copied into a replacement table, or recreated. Add these columns:

| Column | SQLite definition | Default / invariant |
|---|---|---|
| namespace | TEXT NOT NULL | 'workspace' |
| memory_type | TEXT NOT NULL | 'legacy' |
| visibility | TEXT NOT NULL | 'internal' |
| sensitivity | TEXT NOT NULL | 'unknown' |
| task_id | TEXT | NULL |
| content_hash | TEXT | NULL |
| metadata_json | TEXT NOT NULL | '{}' |
| owner_agent | TEXT NOT NULL | 'workspace' |

Add indexes:

    CREATE INDEX IF NOT EXISTS idx_memories_namespace_created
      ON memories(namespace, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_memories_namespace_type_created
      ON memories(namespace, memory_type, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_memories_task_id
      ON memories(task_id);

Allowed namespace values are workspace and adult. Allowed visibility values are internal and shareable. Allowed sensitivity values are normal, sensitive, and unknown. Validate these values at the repository boundary; add CHECK constraints only through a table-rebuild migration if a future revision explicitly approves that migration. Stage 2 must not rebuild the existing table.

### 4.2 Migration ledger

Create:

    CREATE TABLE IF NOT EXISTS memory_schema_migrations (
      version INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      applied_at TEXT NOT NULL
    );

Version 1 is the namespace/ACL/audit migration. The ledger is the single source of truth; do not maintain a second independently writable migration counter. Migration names and versions are constants in code, not caller inputs.

Migration trigger and live-data gate:

- MemoryStore construction must never silently migrate an existing version-0 legacy database.
- A new database path that did not exist before opening may initialize directly to schema version 1 because there is no prior user data to transform. An already-existing version-0 file containing the legacy memories table is always treated as legacy, even when its row count is zero, and requires the explicit command.
- If an existing legacy database has no migration ledger, normal MemoryStore construction raises a typed MemoryMigrationRequired error without changing its schema, rows, journal mode, or timestamps.
- Existing Workspace services remain unavailable against that legacy file until the operator explicitly runs the migration command.
- The only supported live-database migration entry point is: .venv/bin/python -m agent.memory_migrations backup-and-migrate
- The command uses WORKSPACE_MEMORY_DB (or the normal default) from the process environment. It accepts no arbitrary database path, SQL, principal, or namespace argument.
- The command prints a dry-run summary (safe basename only, row count, ID range, schema version, journal mode) and requires an explicit --confirm flag before creating a backup or modifying the source. Without --confirm it performs read-only inspection only.
- The confirmed command creates a collision-resistant backup under .data/backups/ using SQLite Connection.backup(), creates the backup directory with mode 0700 and the backup file with mode 0600 on POSIX, validates the backup read-only, compares row count and ID range, and runs integrity_check and foreign_key_check. It refuses to migrate if backup verification fails.
- Only after successful backup verification does it apply the additive migration to the source DB. It then verifies schema, preserved IDs/content/tags/timestamps by row-level comparison or pre-migration hashes, row count, integrity_check, foreign_key_check, and Workspace recall.
- The backup is retained; the command never overwrites an earlier backup and never deletes the old DB or backup. Restore is a separate operation requiring explicit operator action.
- Unit tests use temporary DBs and may call the internal migration function directly; they never invoke the live-data command against .data/memory.sqlite3.

Migration algorithm:

1. Open a connection with foreign_keys=ON and a bounded busy timeout.
2. Start BEGIN IMMEDIATE before reading the current migration ledger.
3. If version 1 is already applied, validate the required columns, tables, indexes, and fixed ACL rows; do not reapply ALTER TABLE.
4. For a version-0 database, inspect the known legacy schema and fail closed if required legacy columns are missing or unexpected incompatible columns are present.
5. Add each new column with an additive ALTER TABLE ... ADD COLUMN statement and a constant SQL default.
6. Create ACL, audit, indexes, and ledger rows using individual execute calls. Do not use executescript inside the migration transaction because its implicit transaction behavior can invalidate the atomic migration contract.
7. Insert version 1 into memory_schema_migrations only after all schema/data initialization succeeds.
8. Commit. On any exception, roll back all DDL/data changes supported by SQLite and re-raise a sanitized migration error.
9. After commit, re-open and verify schema, row count, legacy row mapping, database integrity, and foreign keys.

The migration must be safe on a new empty database and on the existing legacy schema. Concurrent initializers serialize through BEGIN IMMEDIATE. A second initializer sees version 1 and performs validation only.

The explicit backup-and-migrate command above is the only live-data migration path. It must not run automatically from import, application startup, or MemoryStore construction. Its --confirm gate is mandatory, the backup uses Connection.backup(), and the backup remains until the source passes post-migration checks and a restore drill has been validated. The command is not executed during Stage 2 development or CI.

### 4.3 ACL table

    CREATE TABLE memory_namespace_acl (
      principal TEXT NOT NULL,
      namespace TEXT NOT NULL CHECK(namespace IN ('workspace','adult')),
      can_read INTEGER NOT NULL CHECK(can_read IN (0,1)),
      can_write INTEGER NOT NULL CHECK(can_write IN (0,1)),
      can_read_shareable INTEGER NOT NULL CHECK(can_read_shareable IN (0,1)),
      PRIMARY KEY(principal, namespace)
    );

Seed exactly these six rows:

| Principal | Namespace | Read | Write | Shareable read |
|---|---|---:|---:|---:|
| workspace_agent | workspace | 1 | 1 | 0 |
| workspace_agent | adult | 0 | 0 | 0 |
| adult_agent | adult | 1 | 1 | 0 |
| adult_agent | workspace | 0 | 0 | 1 |
| feedback_publisher | workspace | 0 | 1 | 0 |
| feedback_publisher | adult | 0 | 0 | 0 |

Factory-created scopes use fixed principal constants only. ACL table access is internal to repository code. No MCP tool or application-facing configuration can insert or update ACL entries.

At initialization, compare the stored ACL rows with the required fixed matrix. If a required row is missing or differs, fail closed with an authorization-configuration error; do not silently grant access or repair an unknown policy state at runtime.

### 4.4 Access audit table

    CREATE TABLE memory_access_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      principal TEXT NOT NULL,
      operation TEXT NOT NULL,
      namespace TEXT NOT NULL,
      memory_id INTEGER,
      created_at TEXT NOT NULL,
      FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE RESTRICT
    );

Audit only operation metadata. operation is one of remember, recall, recent, publish_feedback, or denied. Do not store search terms, query text, content, tags, metadata, prompt fragments, or image information. A read operation records one audit row per returned memory; a zero-result read records one row with memory_id=NULL. An authorization denial records operation=denied, the fixed principal, and the attempted fixed scope namespace; it does not record the attempted content/query.

### 4.5 SQL authorization predicates

Every SELECT and INSERT must be authorized in SQL, not by filtering returned Python rows.

Owned namespace read predicate:

    EXISTS (
      SELECT 1 FROM memory_namespace_acl acl
      WHERE acl.principal = :principal
        AND acl.namespace = memories.namespace
        AND acl.can_read = 1
    )

Shareable read predicate:

    memories.visibility = 'shareable'
    AND EXISTS (
      SELECT 1 FROM memory_namespace_acl acl
      WHERE acl.principal = :principal
        AND acl.namespace = memories.namespace
        AND acl.can_read_shareable = 1
    )

For normal recall/recent, combine the permitted owned-namespace predicate with the shareable predicate. recall_owned/recent_owned use only the owned-namespace predicate. recall_shareable_workspace/recent_shareable_workspace additionally bind namespace='workspace' and are callable only on Adult scope.

Every write must be an INSERT ... SELECT ... WHERE EXISTS(ACL can_write=1) with bound values, or an equivalent transaction that validates the ACL row inside the same transaction. A failed authorization must insert no memory. The scope-assigned namespace, principal, visibility, and owner_agent are constants held by the scoped object; they are never taken from method arguments.

Because SQLite has no native row-level security, this contract protects access through the supported repository interfaces and fixed application identities. It does not claim isolation from hostile arbitrary Python code running with the same OS/database permissions.

## 5. Connection, transaction, and JSON rules

Every connection must:

- Set row_factory to sqlite3.Row.
- Execute PRAGMA foreign_keys=ON and verify it is enabled.
- Set PRAGMA busy_timeout=10000 (or the repository's equivalent bounded 10-second timeout).
- Use parameterized SQL for all values.
- Keep the existing DELETE journal mode; do not enable WAL in Stage 2.
- Use explicit BEGIN IMMEDIATE for migration and each write-plus-audit transaction.
- Never keep a transaction open while performing model/network work.

JSON rules:

- tags must remain valid JSON arrays of strings and retain their original serialized meaning during migration.
- metadata_json must be a valid JSON object and must be size-bounded before serialization.
- Reject NaN/Infinity, unsupported Python objects, over-deep objects, and oversized encoded JSON.
- Do not silently replace malformed legacy tags with empty lists. Migration should preserve raw tags; reading a malformed legacy value should raise a controlled data-integrity error and identify only the memory ID, not expose its content in logs.

## 6. Data flow contracts

### Workspace write/read

1. Existing callers create MemoryStore() or explicitly receive workspace_scope().
2. remember writes a row with namespace=workspace, visibility=internal, owner_agent=workspace.
3. recall/recent SQL is constrained to workspace namespace and its ACL row.
4. Returned values preserve existing Workspace API shape for legacy wrappers.
5. The transaction writes audit metadata but never query/content data.

### Adult private memory

1. Adult application construction receives MemoryStore().adult_scope().
2. remember writes only namespace=adult, visibility=internal, owner_agent=adult_agent.
3. recall_private reads only namespace=adult.
4. recall_shareable_workspace reads only namespace=workspace with visibility=shareable and can_read_shareable=1.
5. Adult cannot retrieve legacy Workspace rows because migration marks all legacy rows internal.

### Feedback publication

1. Feedback code creates a typed SanitizedFeedback proposal.
2. Sanitizer removes raw task content, image bytes, identity details, secrets, local paths, and unneeded unique identifiers.
3. Publisher schema and policy validator run immediately before persistence.
4. FeedbackPublisherStore executes the fixed write-only publication path.
5. Resulting Workspace row is namespace=workspace, visibility=shareable, memory_type=lesson, owner_agent=feedback_publisher.
6. Workspace can read it under its workspace read ACL; Adult can read it only under its shareable-read ACL.
7. FeedbackPublisherStore cannot read the source Adult memory or query either namespace.

## 7. Test specification and acceptance IDs

All migration/isolation tests use independent temporary SQLite files. Tests must never point to .data/memory.sqlite3.

### Migration tests

- MEM-MIG-01: legacy database with known schema migrates successfully.
- MEM-MIG-02: IDs, content, tags, and created_at are byte-for-byte/value-for-value unchanged.
- MEM-MIG-03: every legacy row maps to workspace/internal/unknown/legacy/workspace.
- MEM-MIG-04: new empty database initializes and receives migration version 1.
- MEM-MIG-05: migration is idempotent; running initializer twice creates no duplicate columns/indexes/ledger rows.
- MEM-MIG-06: concurrent initializers serialize and leave one valid version-1 schema.
- MEM-MIG-07: injected failure mid-migration rolls back schema and ledger to the prior state.
- MEM-MIG-08: unsupported legacy schema fails without dropping/replacing memories.
- MEM-MIG-09: malformed legacy tags are preserved during migration and reported as a controlled read error.
- MEM-MIG-10: integrity_check returns ok and foreign_key_check returns no rows.
- MEM-MIG-11: normal MemoryStore construction against an existing v0 legacy database raises MemoryMigrationRequired and makes no schema/data/journal changes.
- MEM-MIG-12: the explicit --confirm backup-and-migrate command creates and verifies a non-overwriting online backup before changing the source; missing confirmation or failed backup verification leaves the source unchanged.

### Isolation and authorization tests

- MEM-ACL-01: Workspace write lands only in workspace namespace.
- MEM-ACL-02: Adult write lands only in adult namespace.
- MEM-ACL-03: Workspace recall/recent never returns Adult rows.
- MEM-ACL-04: Adult private recall returns Adult rows only.
- MEM-ACL-05: Adult shareable recall returns only explicitly shareable Workspace rows.
- MEM-ACL-06: Legacy Workspace rows are not visible to Adult.
- MEM-ACL-07: missing or modified ACL rows cause fail-closed initialization/access.
- MEM-ACL-08: caller-supplied namespace/principal/visibility arguments are rejected or absent from the public signatures.
- MEM-ACL-09: feedback publisher has no read API and cannot read Adult or Workspace rows.
- MEM-ACL-10: publisher rejects extra fields, invalid categories, unbounded strings, raw-content fields, and invalid verification status.
- MEM-ACL-11: publisher can write only schema-valid shareable Workspace lesson rows.
- MEM-ACL-12: direct SQL query assertions prove namespace predicates are applied before data is returned.

### Audit, connection, and compatibility tests

- MEM-AUD-01: every successful remember/recent/recall/publish operation creates expected audit metadata.
- MEM-AUD-02: zero-result reads are audited with memory_id=NULL.
- MEM-AUD-03: query text, content, tags, secrets, and image fields never occur in audit rows.
- MEM-DB-01: foreign_keys is enabled on every opened connection.
- MEM-DB-02: busy timeout is bounded and journal mode remains DELETE.
- MEM-COMPAT-01: all four pre-existing tests in tests/test_memory_store.py pass unchanged.
- MEM-COMPAT-02: agent/memory_mcp_server.py keeps remember, recall, recent_memories names and does not expose namespace/principal arguments.
- MEM-COMPAT-03: control_plane.memory_snapshot and task_worker.memory_recent remain Workspace-scoped.
- MEM-REG-01: full existing unittest suite passes with no regressions.

## 8. Required implementation files

Existing files to update:

- agent/memory_store.py — migration/version checks, fixed scope factories, ACL-enforced SQL, compatibility wrappers, audit writes.
- agent/memory_mcp_server.py — bind only workspace_scope(); preserve all three existing MCP tool names and argument/result contracts.
- agent/adult/adult_memory.py — new Adult-only private/shareable memory facade.

New files:

- agent/memory_migrations.py — explicit inspect / backup-and-migrate CLI. No migration-on-import or migration-on-startup.
- tests/test_memory_migrations.py — migration ledger, additive DDL, backup, dry-run/confirmation, rollback and compatibility fixtures.
- tests/test_memory_access_control.py — ACL matrix, scope isolation, SQL predicate, feedback write-only capability, and audit privacy tests.

The migration CLI must not be imported by normal server startup as a side effect. The CLI is invoked explicitly by the operator after reviewing the dry-run summary and confirming the operation.

## 8. Stage 2 implementation sequence and gate

Implement in this exact order:

1. Add typed MemoryRecord/MemoryRef and SanitizedFeedback validation helpers.
2. Add migration ledger and additive migration against temporary test databases.
3. Add ACL and audit schema, fixed ACL validation, and SQL permission predicates.
4. Add ScopedMemoryStore and the three fixed factories; add write-only FeedbackPublisherStore.
5. Preserve MemoryStore legacy wrappers and update Workspace MCP to consume only the Workspace scope without changing tool names or response contracts.
6. Add AdultMemory using only adult_scope().
7. Add migration, ACL, audit, compatibility, and rollback tests.
8. Run focused memory tests, then the full unit suite from a fresh process.
9. Inspect diff and protected-path scan; verify no .vscode/, .env, credentials, or runtime DB files changed.
10. Run repository CI and inspect all jobs.

Stage 2 PASS requires every MEM-* acceptance test and the existing full test suite to pass, migration validation to be repeatable on temporary databases, and code review to find no namespace or principal selection path exposed outside fixed factories.

Stage 3 may not begin until this gate is recorded as PASS. The live memory database remains untouched until a separately reviewed migration operation is authorized and a consistent backup/restore plan is executed.
