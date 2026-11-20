"""Scoped SQLite memory storage with additive migration and SQL-enforced ACLs.

The default MemoryStore API is permanently bound to Workspace. Adult and
feedback callers must receive one of the fixed scope factories.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(".data") / "memory.sqlite3"
BUSY_TIMEOUT_MS = 10_000
MAX_CONTENT_LENGTH = 10_000
MAX_TAGS = 20
MAX_TAG_LENGTH = 64
MAX_METADATA_BYTES = 4 * 1024
MAX_METADATA_DEPTH = 3
MAX_METADATA_KEYS = 32
MAX_METADATA_ARRAY_ITEMS = 50
MAX_METADATA_STRING_LENGTH = 256

MEMORY_TYPES = frozenset({"fact", "strategy", "outcome", "lesson", "preference", "note"})
SENSITIVITIES = frozenset({"normal", "sensitive", "unknown"})
VISIBILITIES = frozenset({"internal", "shareable"})
NAMESPACES = frozenset({"workspace", "adult"})
FEEDBACK_CATEGORIES = frozenset({
    "capability_gap", "transient_failure", "quality_issue",
    "compatibility_issue", "strategy",
})
VERIFICATION_STATUSES = frozenset({"observed", "reproduced", "unverified"})
_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METADATA_KEYS = frozenset({
    "category", "component_id", "version", "operation", "failure_code",
    "recommended_action", "verification_status", "source_fingerprint",
    "source", "confidence", "language", "project", "tags",
})
_ACL_POLICY = {
    ("workspace_agent", "workspace"): (1, 1, 0),
    ("workspace_agent", "adult"): (0, 0, 0),
    ("adult_agent", "adult"): (1, 1, 0),
    ("adult_agent", "workspace"): (0, 0, 1),
    ("feedback_publisher", "workspace"): (0, 1, 0),
    ("feedback_publisher", "adult"): (0, 0, 0),
}
_SCOPE_TOKEN = object()


class MemoryErrorBase(Exception):
    """Base class for sanitized memory repository errors."""


class MemoryMigrationRequired(MemoryErrorBase):
    """An existing legacy database needs an explicit backed-up migration."""


class MemoryAuthorizationError(MemoryErrorBase):
    """A fixed repository scope is not authorized by the configured ACL."""


class MemoryDataIntegrityError(MemoryErrorBase):
    """A stored row is malformed; only a stable code and row ID are exposed."""

    def __init__(self, memory_id: int, code: str):
        self.memory_id = int(memory_id)
        self.code = code
        super().__init__(f"Memory row {self.memory_id} failed validation ({code}).")


class UnsupportedLegacySchema(MemoryErrorBase):
    """The database layout is not a supported legacy or current schema."""


class MemoryBackupVerificationFailed(MemoryErrorBase):
    """The online backup failed validation; source data was not migrated."""


@dataclass(frozen=True)
class MemoryRef:
    id: int
    created_at: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    content: str
    tags: list[str]
    created_at: str
    memory_type: str
    namespace: str
    visibility: str
    sensitivity: str
    task_id: str | None
    metadata: dict[str, Any]
    owner_agent: str
    matched_terms: int | None = None

    def to_dict(self, *, include_scope_fields: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }
        if self.matched_terms is not None:
            result["matched_terms"] = self.matched_terms
        if include_scope_fields:
            result.update({
                "memory_type": self.memory_type,
                "namespace": self.namespace,
                "visibility": self.visibility,
                "sensitivity": self.sensitivity,
                "task_id": self.task_id,
                "metadata": dict(self.metadata),
                "owner_agent": self.owner_agent,
            })
        return result


@dataclass(frozen=True)
class SanitizedFeedback:
    category: str
    component_id: str
    version: str | None
    operation: str
    failure_code: str
    lesson: str
    recommended_action: str
    verification_status: str
    source_fingerprint: str
    created_at: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_database(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve()
    if readonly:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    else:
        connection = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if enabled != 1:
        connection.close()
        raise MemoryErrorBase("SQLite foreign-key enforcement could not be enabled.")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _required_columns(connection: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in connection.execute("PRAGMA table_info(memories)")}


def _validate_acl(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT principal, namespace, can_read, can_write, can_read_shareable "
        "FROM memory_namespace_acl"
    ).fetchall()
    actual = {
        (row["principal"], row["namespace"]): (
            row["can_read"], row["can_write"], row["can_read_shareable"]
        )
        for row in rows
    }
    if actual != _ACL_POLICY:
        raise MemoryAuthorizationError("The fixed memory ACL policy is missing or has been modified.")


def _create_acl_and_audit(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE memory_namespace_acl (
            principal TEXT NOT NULL,
            namespace TEXT NOT NULL CHECK(namespace IN ('workspace','adult')),
            can_read INTEGER NOT NULL CHECK(can_read IN (0,1)),
            can_write INTEGER NOT NULL CHECK(can_write IN (0,1)),
            can_read_shareable INTEGER NOT NULL CHECK(can_read_shareable IN (0,1)),
            PRIMARY KEY(principal, namespace)
        )"""
    )
    connection.execute(
        """CREATE TABLE memory_access_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN
                ('remember','recall','recent','publish_feedback','denied')),
            namespace TEXT NOT NULL CHECK(namespace IN ('workspace','adult')),
            memory_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE RESTRICT
        )"""
    )
    connection.executemany(
        "INSERT INTO memory_namespace_acl"
        "(principal, namespace, can_read, can_write, can_read_shareable) "
        "VALUES (?, ?, ?, ?, ?)",
        [(p, n, *permissions) for (p, n), permissions in _ACL_POLICY.items()],
    )


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace_created "
        "ON memories(namespace, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_namespace_type_created "
        "ON memories(namespace, memory_type, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_task_id ON memories(task_id)"
    )


def _create_current_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            namespace TEXT NOT NULL DEFAULT 'workspace',
            memory_type TEXT NOT NULL DEFAULT 'legacy',
            visibility TEXT NOT NULL DEFAULT 'internal',
            sensitivity TEXT NOT NULL DEFAULT 'unknown',
            task_id TEXT,
            content_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            owner_agent TEXT NOT NULL DEFAULT 'workspace'
        )"""
    )
    connection.execute(
        """CREATE TABLE memory_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )"""
    )
    _create_acl_and_audit(connection)
    _create_indexes(connection)
    connection.execute(
        "INSERT INTO memory_schema_migrations(version, name, applied_at) VALUES (1, ?, ?)",
        ("namespace_acl_audit_v1", _utc_now()),
    )


def _validate_current_schema(connection: sqlite3.Connection) -> None:
    tables = _table_names(connection)
    required_tables = {
        "memories", "memory_schema_migrations",
        "memory_namespace_acl", "memory_access_audit",
    }
    if not required_tables.issubset(tables):
        raise UnsupportedLegacySchema("The memory database is missing required version-1 tables.")
    required = {
        "id", "content", "tags", "created_at", "namespace", "memory_type",
        "visibility", "sensitivity", "task_id", "content_hash",
        "metadata_json", "owner_agent",
    }
    if not required.issubset(_required_columns(connection)):
        raise UnsupportedLegacySchema("The memory database has an incomplete version-1 schema.")
    migration = connection.execute(
        "SELECT name FROM memory_schema_migrations WHERE version=1"
    ).fetchone()
    if migration is None or migration["name"] != "namespace_acl_audit_v1":
        raise UnsupportedLegacySchema("The memory migration ledger is invalid.")
    audit_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(memory_access_audit)")
    }
    if not {"id", "principal", "operation", "namespace", "memory_id", "created_at"}.issubset(audit_columns):
        raise UnsupportedLegacySchema("The memory audit table is incomplete.")
    indexes = {
        row["name"] for row in connection.execute("PRAGMA index_list(memories)")
    }
    if not {
        "idx_memories_created_at", "idx_memories_namespace_created",
        "idx_memories_namespace_type_created", "idx_memories_task_id",
    }.issubset(indexes):
        raise UnsupportedLegacySchema("The memory database is missing required indexes.")
    _validate_acl(connection)


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise ValueError("Memory content must be text.")
    cleaned = content.strip()
    if not cleaned:
        raise ValueError("Memory content must not be empty.")
    if len(cleaned) > MAX_CONTENT_LENGTH:
        raise ValueError("Memory content must be 10,000 characters or fewer.")
    return cleaned


def _validate_tags(tags: list[str] | None) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("Memory tags must be a list of strings.")
    clean_tags = sorted({tag.strip() for tag in tags if tag.strip()})
    if len(clean_tags) > MAX_TAGS or any(len(tag) > MAX_TAG_LENGTH for tag in clean_tags):
        raise ValueError("Use at most 20 tags, each 64 characters or fewer.")
    return clean_tags


def _validate_task_id(task_id: str | None) -> str | None:
    if task_id is None:
        return None
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id must be a bounded opaque identifier.")
    return task_id


def _validate_json_value(value: Any, depth: int = 1) -> None:
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("Metadata nesting exceeds three levels.")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Metadata numbers must be finite.")
        return
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING_LENGTH:
            raise ValueError("Metadata strings must be 256 characters or fewer.")
        if re.search(r"(?i)(?:^|[\s=:])(?:/home/|/root/|[a-z]:\\|\\\\)", value):
            raise ValueError("Metadata must not contain absolute filesystem paths.")
        return
    if isinstance(value, list):
        if len(value) > MAX_METADATA_ARRAY_ITEMS:
            raise ValueError("Metadata arrays may contain at most 50 values.")
        for item in value:
            if isinstance(item, (dict, list)):
                raise ValueError("Metadata arrays may contain scalar values only.")
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_METADATA_KEYS:
            raise ValueError("Metadata objects may contain at most 32 keys.")
        for key, item in value.items():
            if not isinstance(key, str) or key not in _METADATA_KEYS:
                raise ValueError("Metadata contains a key outside the application allowlist.")
            if re.search(r"(?i)(prompt|image|secret|token|password|payload|content|raw)", key):
                raise ValueError("Metadata contains a prohibited raw-data key.")
            _validate_json_value(item, depth + 1)
        return
    raise ValueError("Metadata contains an unsupported value.")


def _validate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("Memory metadata must be a JSON object.")
    _validate_json_value(metadata)
    encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("Memory metadata must be at most 4 KiB.")
    return metadata


def _decode_row(row: sqlite3.Row, *, matched_terms: int | None = None) -> MemoryRecord:
    memory_id = int(row["id"])
    try:
        if not isinstance(row["content"], str) or not 1 <= len(row["content"]) <= MAX_CONTENT_LENGTH:
            raise ValueError("content")
        if not isinstance(row["created_at"], str) or not row["created_at"]:
            raise ValueError("created_at")
        tags = json.loads(row["tags"])
        metadata = json.loads(row["metadata_json"])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("tags")
        if not isinstance(metadata, dict):
            raise ValueError("metadata")
        _validate_tags(tags)
        _validate_metadata(metadata)
        if row["namespace"] not in NAMESPACES:
            raise ValueError("namespace")
        if row["visibility"] not in VISIBILITIES:
            raise ValueError("visibility")
        if row["sensitivity"] not in SENSITIVITIES:
            raise ValueError("sensitivity")
        if row["memory_type"] not in MEMORY_TYPES | {"legacy"}:
            raise ValueError("memory_type")
        _validate_task_id(row["task_id"])
        if row["owner_agent"] not in {"workspace", "adult_agent", "feedback_publisher"}:
            raise ValueError("owner_agent")
        if row["content_hash"] is not None and not re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]):
            raise ValueError("content_hash")
        if row["namespace"] == "adult" and row["owner_agent"] != "adult_agent":
            raise ValueError("owner_namespace")
        if row["namespace"] == "workspace" and row["owner_agent"] == "adult_agent":
            raise ValueError("owner_namespace")
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise MemoryDataIntegrityError(memory_id, "invalid_stored_record") from None
    return MemoryRecord(
        id=memory_id,
        content=row["content"],
        tags=tags,
        created_at=row["created_at"],
        memory_type=row["memory_type"],
        namespace=row["namespace"],
        visibility=row["visibility"],
        sensitivity=row["sensitivity"],
        task_id=row["task_id"],
        metadata=metadata,
        owner_agent=row["owner_agent"],
        matched_terms=matched_terms,
    )


class MemoryStore:
    """Database owner and fixed-scope factory; direct methods are Workspace-only."""

    def __init__(self, db_path: str | Path | None = None):
        configured = db_path or os.getenv("WORKSPACE_MEMORY_DB") or str(DEFAULT_DB)
        self.db_path = Path(configured).expanduser()
        existed_before_open = self.db_path.exists()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(existed_before_open=existed_before_open)

    def _connect(self) -> sqlite3.Connection:
        return _connect_database(self.db_path)

    def _initialize(self, *, existed_before_open: bool) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = _table_names(connection)
            if not tables:
                if existed_before_open:
                    raise UnsupportedLegacySchema(
                        "An existing empty database file is not a supported new database."
                    )
                _create_current_schema(connection)
                connection.commit()
            else:
                if "memory_schema_migrations" not in tables:
                    if "memories" in tables:
                        raise MemoryMigrationRequired(
                            "Existing version-0 memory database requires explicit backup-and-migrate."
                        )
                    raise UnsupportedLegacySchema("Unrecognized memory database schema.")
                _validate_current_schema(connection)
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def workspace_scope(self) -> "ScopedMemoryStore":
        return ScopedMemoryStore(_SCOPE_TOKEN, self, "workspace_agent", "workspace", "workspace")

    def adult_scope(self) -> "ScopedMemoryStore":
        return ScopedMemoryStore(_SCOPE_TOKEN, self, "adult_agent", "adult", "adult_agent")

    def feedback_publisher_scope(self) -> "FeedbackPublisherStore":
        return FeedbackPublisherStore(_SCOPE_TOKEN, self)

    def remember(self, content: str, tags: list[str] | None = None) -> dict:
        return self.workspace_scope().remember(content, tags).to_dict()

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        return [
            record.to_dict()
            for record in self.workspace_scope().recall(query, limit)
        ]

    def recent(self, limit: int = 10) -> list[dict]:
        return [
            record.to_dict()
            for record in self.workspace_scope().recent(limit)
        ]


class ScopedMemoryStore:
    """Internal repository facade bound to one immutable principal and namespace."""

    def __init__(
        self,
        token: object,
        store: MemoryStore,
        principal: str,
        namespace: str,
        owner_agent: str,
    ):
        if token is not _SCOPE_TOKEN:
            raise TypeError("ScopedMemoryStore instances must come from fixed MemoryStore factories.")
        if (principal, namespace, owner_agent) not in {
            ("workspace_agent", "workspace", "workspace"),
            ("adult_agent", "adult", "adult_agent"),
        }:
            raise TypeError("Unsupported fixed memory scope.")
        self.__store = store
        self.__principal = principal
        self.__namespace = namespace
        self.__owner_agent = owner_agent

    @property
    def _scope_kind(self) -> str:
        return self.__namespace

    def _connection(self) -> sqlite3.Connection:
        return self.__store._connect()

    def _audit(
        self, connection: sqlite3.Connection, operation: str,
        namespace: str, memory_id: int | None,
    ) -> None:
        connection.execute(
            "INSERT INTO memory_access_audit"
            "(principal, operation, namespace, memory_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (self.__principal, operation, namespace, memory_id, _utc_now()),
        )

    def _audit_denied(self, operation: str, namespace: str) -> None:
        try:
            connection = self.__store._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                if "memory_access_audit" in _table_names(connection):
                    connection.execute(
                        "INSERT INTO memory_access_audit"
                        "(principal, operation, namespace, memory_id, created_at) "
                        "VALUES (?, 'denied', ?, NULL, ?)",
                        (self.__principal, namespace, _utc_now()),
                    )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error:
            return

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        memory_type: str = "fact",
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRef:
        cleaned = _validate_content(content)
        clean_tags = _validate_tags(tags)
        if memory_type not in MEMORY_TYPES:
            raise ValueError("Unsupported memory_type.")
        clean_task_id = _validate_task_id(task_id)
        clean_metadata = _validate_metadata(metadata)
        created_at = _utc_now()
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_acl(connection)
            cursor = connection.execute(
                """INSERT INTO memories
                   (content, tags, created_at, namespace, memory_type, visibility,
                    sensitivity, task_id, content_hash, metadata_json, owner_agent)
                   SELECT ?, ?, ?, ?, ?, 'internal', 'unknown', ?, NULL, ?, ?
                   WHERE EXISTS (
                     SELECT 1 FROM memory_namespace_acl
                     WHERE principal=? AND namespace=? AND can_write=1
                   )""",
                (
                    cleaned, json.dumps(clean_tags, ensure_ascii=False), created_at,
                    self.__namespace, memory_type, clean_task_id,
                    json.dumps(clean_metadata, ensure_ascii=False, allow_nan=False),
                    self.__owner_agent, self.__principal, self.__namespace,
                ),
            )
            if cursor.rowcount != 1:
                self._audit(connection, "denied", self.__namespace, None)
                raise MemoryAuthorizationError("The fixed memory scope is not authorized to write.")
            memory_id = int(cursor.lastrowid)
            self._audit(connection, "remember", self.__namespace, memory_id)
            connection.commit()
            return MemoryRef(memory_id, created_at, clean_tags)
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, MemoryAuthorizationError):
                self._audit_denied("remember", self.__namespace)
            raise
        finally:
            connection.close()

    def _read(
        self,
        query: str | None,
        limit: int,
        *,
        operation: str,
        owned_only: bool = False,
        shareable_workspace_only: bool = False,
    ) -> list[MemoryRecord]:
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("Memory limit must be between 1 and 20.")
        terms: list[str] = []
        if query is not None:
            if not isinstance(query, str) or not query.strip():
                raise ValueError("Recall query must not be empty.")
            terms = list(dict.fromkeys(re.findall(r"[\w-]{2,}", query.strip().casefold())))
        no_search_terms = query is not None and not terms
        if shareable_workspace_only and self.__namespace != "adult":
            self._audit_denied(operation, "workspace")
            raise MemoryAuthorizationError("Only the Adult scope may read shareable Workspace memories.")

        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_acl(connection)
            if no_search_terms:
                self._audit(connection, operation, "workspace" if shareable_workspace_only else self.__namespace, None)
                connection.commit()
                return []
            if shareable_workspace_only:
                predicates = [
                    "m.namespace='workspace'",
                    "m.visibility='shareable'",
                    """EXISTS (SELECT 1 FROM memory_namespace_acl acl
                               WHERE acl.principal=? AND acl.namespace=m.namespace
                                 AND acl.can_read_shareable=1)""",
                ]
                parameters: list[Any] = [self.__principal]
            elif owned_only:
                predicates = [
                    "m.namespace=?",
                    """EXISTS (SELECT 1 FROM memory_namespace_acl acl
                               WHERE acl.principal=? AND acl.namespace=m.namespace
                                 AND acl.can_read=1)""",
                ]
                parameters = [self.__namespace, self.__principal]
            else:
                predicates = [
                    """(
                      (m.namespace=? AND EXISTS (
                        SELECT 1 FROM memory_namespace_acl own_acl
                        WHERE own_acl.principal=? AND own_acl.namespace=m.namespace
                          AND own_acl.can_read=1
                      ))
                      OR
                      (m.namespace='workspace' AND m.visibility='shareable' AND EXISTS (
                        SELECT 1 FROM memory_namespace_acl share_acl
                        WHERE share_acl.principal=? AND share_acl.namespace=m.namespace
                          AND share_acl.can_read_shareable=1
                      ))
                    )"""
                ]
                parameters = [self.__namespace, self.__principal, self.__principal]
            if terms:
                like_clauses = []
                for term in terms:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    like_clauses.append("LOWER(m.content) LIKE ? ESCAPE '\\'")
                    parameters.append(f"%{escaped}%")
                predicates.append("(" + " OR ".join(like_clauses) + ")")
            where_sql = " AND ".join(predicates)
            selected = (
                "m.id, m.content, m.tags, m.created_at, m.memory_type, m.namespace, "
                "m.visibility, m.sensitivity, m.task_id, m.metadata_json, m.owner_agent"
            )
            order = "m.created_at DESC, m.id DESC"
            rows = connection.execute(
                f"SELECT {selected} FROM memories m WHERE {where_sql} "
                f"ORDER BY {order} LIMIT ?",
                [*parameters, limit],
            ).fetchall()
            ranked: list[MemoryRecord] = []
            for row in rows:
                score = sum(term in row["content"].casefold() for term in terms) if terms else None
                ranked.append(_decode_row(row, matched_terms=score))
            if terms:
                ranked.sort(key=lambda item: (-(item.matched_terms or 0), -item.id))
            audit_namespace = "workspace" if shareable_workspace_only else self.__namespace
            if rows:
                for record in ranked:
                    self._audit(connection, operation, record.namespace, record.id)
            else:
                self._audit(connection, operation, audit_namespace, None)
            connection.commit()
            return ranked
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, MemoryAuthorizationError):
                self._audit_denied(
                    operation, "workspace" if shareable_workspace_only else self.__namespace
                )
            raise
        finally:
            connection.close()

    def recall(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        return self._read(query, limit, operation="recall")

    def recent(self, limit: int = 10) -> list[MemoryRecord]:
        return self._read(None, limit, operation="recent")

    def recall_owned(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        return self._read(query, limit, operation="recall", owned_only=True)

    def recent_owned(self, limit: int = 10) -> list[MemoryRecord]:
        return self._read(None, limit, operation="recent", owned_only=True)

    def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        return self._read(
            query, limit, operation="recall", shareable_workspace_only=True
        )

    def recent_shareable_workspace(self, limit: int = 10) -> list[MemoryRecord]:
        return self._read(
            None, limit, operation="recent", shareable_workspace_only=True
        )


class FeedbackPublisherStore:
    """Write-only facade for schema-validated sanitized lessons."""

    def __init__(self, token: object, store: MemoryStore):
        if token is not _SCOPE_TOKEN:
            raise TypeError("FeedbackPublisherStore instances must come from MemoryStore.")
        self.__store = store

    @staticmethod
    def _validate_feedback(feedback: SanitizedFeedback) -> SanitizedFeedback:
        if not isinstance(feedback, SanitizedFeedback):
            raise TypeError("feedback must be a SanitizedFeedback record.")
        if not isinstance(feedback.category, str) or feedback.category not in FEEDBACK_CATEGORIES:
            raise ValueError("Unsupported feedback category.")
        if not isinstance(feedback.component_id, str) or not _COMPONENT_RE.fullmatch(feedback.component_id):
            raise ValueError("component_id must be a normalized non-secret identifier.")
        if feedback.version is not None and (
            not isinstance(feedback.version, str) or len(feedback.version) > 64
        ):
            raise ValueError("version must be at most 64 characters.")
        if not isinstance(feedback.operation, str) or not re.fullmatch(
            r"^(text|image)\.(analyze|generate|rewrite|edit)$", feedback.operation
        ):
            raise ValueError("Unsupported feedback operation.")
        if not isinstance(feedback.failure_code, str) or not re.fullmatch(
            r"^[a-z0-9][a-z0-9._-]{0,63}$", feedback.failure_code
        ):
            raise ValueError("failure_code must be normalized.")
        lesson = _validate_content(feedback.lesson)
        action = _validate_content(feedback.recommended_action)
        if len(lesson) > 1000 or len(action) > MAX_METADATA_STRING_LENGTH:
            raise ValueError("Feedback lesson/action exceeds its length limit.")
        if not isinstance(feedback.verification_status, str) or feedback.verification_status not in VERIFICATION_STATUSES:
            raise ValueError("Unsupported verification_status.")
        if not isinstance(feedback.source_fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
            feedback.source_fingerprint
        ):
            raise ValueError("source_fingerprint must be a 64-character keyed fingerprint.")
        prohibited = re.compile(
            r"(?i)(?:https?://|[\w.+-]+@[\w.-]+\.[a-z]{2,}|"
            r"(?:[a-z]:\\|/home/|/root/|\\\\)|"
            r"\b(?:password|api[_ -]?key|access[_ -]?token|secret)\b)"
        )
        if any(prohibited.search(value) for value in (lesson, action)):
            raise ValueError("Feedback contains a prohibited identifier, path, URL, or secret.")
        return feedback

    def publish_sanitized_feedback(self, feedback: SanitizedFeedback) -> MemoryRef:
        clean = self._validate_feedback(feedback)
        created_at = _utc_now()
        metadata = {
            "category": clean.category,
            "component_id": clean.component_id,
            "version": clean.version,
            "operation": clean.operation,
            "failure_code": clean.failure_code,
            "recommended_action": clean.recommended_action,
            "verification_status": clean.verification_status,
            "source_fingerprint": clean.source_fingerprint,
        }
        metadata_json = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        tags = [clean.category]
        connection = self.__store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _validate_acl(connection)
            cursor = connection.execute(
                """INSERT INTO memories
                   (content, tags, created_at, namespace, memory_type, visibility,
                    sensitivity, task_id, content_hash, metadata_json, owner_agent)
                   SELECT ?, ?, ?, 'workspace', 'lesson', 'shareable',
                          'unknown', NULL, NULL, ?, 'feedback_publisher'
                   WHERE EXISTS (
                     SELECT 1 FROM memory_namespace_acl
                     WHERE principal='feedback_publisher'
                       AND namespace='workspace' AND can_write=1
                   )""",
                (clean.lesson, json.dumps(tags), created_at, metadata_json),
            )
            if cursor.rowcount != 1:
                connection.execute(
                    "INSERT INTO memory_access_audit"
                    "(principal, operation, namespace, memory_id, created_at) "
                    "VALUES ('feedback_publisher','denied','workspace',NULL,?)",
                    (_utc_now(),),
                )
                raise MemoryAuthorizationError("Feedback publisher is not authorized to write.")
            memory_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO memory_access_audit"
                "(principal, operation, namespace, memory_id, created_at) "
                "VALUES ('feedback_publisher','publish_feedback','workspace',?,?)",
                (memory_id, _utc_now()),
            )
            connection.commit()
            return MemoryRef(memory_id, created_at, tags)
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, MemoryAuthorizationError):
                try:
                    denied = self.__store._connect()
                    try:
                        denied.execute("BEGIN IMMEDIATE")
                        denied.execute(
                            "INSERT INTO memory_access_audit"
                            "(principal, operation, namespace, memory_id, created_at) "
                            "VALUES ('feedback_publisher','denied','workspace',NULL,?)",
                            (_utc_now(),),
                        )
                        denied.commit()
                    finally:
                        denied.close()
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()
