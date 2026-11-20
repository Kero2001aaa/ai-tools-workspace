"""Explicit, backup-first migration for the Workspace SQLite memory database.

Importing this module has no database side effects. The live database is never
migrated by MemoryStore construction or by starting an MCP server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent.memory_store import (
    DEFAULT_DB,
    MemoryBackupVerificationFailed,
    MemoryStore,
    UnsupportedLegacySchema,
    _ACL_POLICY,
    _connect_database,
    _create_acl_and_audit,
    _create_indexes,
    _table_names,
    _validate_current_schema,
)

LEGACY_COLUMNS = {"id", "content", "tags", "created_at"}
MIGRATION_NAME = "namespace_acl_audit_v1"


@dataclass(frozen=True)
class MigrationReport:
    database: str
    exists: bool
    schema_version: int
    legacy_schema: bool
    row_count: int
    minimum_id: int | None
    maximum_id: int | None
    journal_mode: str | None
    integrity_check: str | None
    foreign_key_violations: int | None
    backup: str | None = None
    migrated: bool = False
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _configured_path() -> Path:
    return Path(os.getenv("WORKSPACE_MEMORY_DB") or str(DEFAULT_DB)).expanduser()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    if "memory_schema_migrations" not in _table_names(connection):
        return 0
    row = connection.execute(
        "SELECT MAX(version) AS version FROM memory_schema_migrations"
    ).fetchone()
    return int(row["version"] or 0)


def _legacy_columns(connection: sqlite3.Connection) -> set[str]:
    if "memories" not in _table_names(connection):
        raise UnsupportedLegacySchema("No legacy memories table was found.")
    return {row["name"] for row in connection.execute("PRAGMA table_info(memories)")}


def _legacy_digest(connection: sqlite3.Connection) -> tuple[int, int | None, int | None, str]:
    rows = connection.execute(
        "SELECT id, content, tags, created_at FROM memories ORDER BY id"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        for value in (row["id"], row["content"], row["tags"], row["created_at"]):
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    ids = [int(row["id"]) for row in rows]
    return len(rows), min(ids) if ids else None, max(ids) if ids else None, digest.hexdigest()


def _apply_v1_migration(connection: sqlite3.Connection) -> None:
    tables = _table_names(connection)
    if "memory_schema_migrations" in tables:
        if _schema_version(connection) >= 1:
            _validate_current_schema(connection)
            return
        raise UnsupportedLegacySchema("An incomplete migration ledger was found.")
    if _legacy_columns(connection) != LEGACY_COLUMNS:
        raise UnsupportedLegacySchema("The legacy memories table does not match the supported schema.")

    additions = (
        ("namespace", "TEXT NOT NULL DEFAULT 'workspace'"),
        ("memory_type", "TEXT NOT NULL DEFAULT 'legacy'"),
        ("visibility", "TEXT NOT NULL DEFAULT 'internal'"),
        ("sensitivity", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("task_id", "TEXT"),
        ("content_hash", "TEXT"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("owner_agent", "TEXT NOT NULL DEFAULT 'workspace'"),
    )
    for column, definition in additions:
        connection.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")

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
        (MIGRATION_NAME, datetime.now(timezone.utc).isoformat()),
    )


class MemoryMigrationManager:
    """Inspect or explicitly migrate only the configured Workspace memory DB."""

    def inspect_configured_database(self) -> MigrationReport:
        path = _configured_path()
        if not path.exists():
            return MigrationReport(
                database=path.name, exists=False, schema_version=0,
                legacy_schema=False, row_count=0, minimum_id=None,
                maximum_id=None, journal_mode=None, integrity_check=None,
                foreign_key_violations=None, message="Database does not exist; no migration needed.",
            )
        connection = _readonly_connection(path)
        try:
            tables = _table_names(connection)
            if "memories" not in tables:
                raise UnsupportedLegacySchema("No memories table was found.")
            version = _schema_version(connection)
            if version == 0 and _legacy_columns(connection) != LEGACY_COLUMNS:
                raise UnsupportedLegacySchema("The legacy memories table does not match the supported schema.")
            count, minimum, maximum, _ = _legacy_digest(connection)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            fk_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            legacy = version == 0 and _legacy_columns(connection) == LEGACY_COLUMNS
            return MigrationReport(
                database=path.name, exists=True, schema_version=version,
                legacy_schema=legacy, row_count=count, minimum_id=minimum,
                maximum_id=maximum, journal_mode=journal,
                integrity_check=integrity, foreign_key_violations=fk_count,
                message="Read-only inspection complete.",
            )
        finally:
            connection.close()

    def backup_and_migrate_configured_database(
        self, confirmed: bool = False
    ) -> MigrationReport:
        before = self.inspect_configured_database()
        if not before.exists:
            return before
        if before.schema_version >= 1:
            return MigrationReport(**{
                **before.to_dict(),
                "message": "Schema version 1 is already applied; no changes made.",
            })
        if not before.legacy_schema:
            raise UnsupportedLegacySchema("The configured database is not a supported version-0 database.")
        if before.integrity_check != "ok" or before.foreign_key_violations != 0:
            raise MemoryBackupVerificationFailed("Source database failed read-only integrity checks.")
        if before.journal_mode != "delete":
            raise MemoryBackupVerificationFailed("Only DELETE journal mode is supported for this migration.")
        if not confirmed:
            return MigrationReport(**{
                **before.to_dict(),
                "message": "Dry run only. Re-run with --confirm to create a backup and migrate.",
            })

        path = _configured_path()
        writer = _connect_database(path)
        backup_path: Path | None = None
        try:
            writer.execute("BEGIN IMMEDIATE")
            if _schema_version(writer) >= 1:
                writer.commit()
                return self.inspect_configured_database()
            if _legacy_columns(writer) != LEGACY_COLUMNS:
                raise UnsupportedLegacySchema("The legacy memories table changed before migration.")
            source_digest = _legacy_digest(writer)

            backup_dir = Path(".data") / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                backup_dir.chmod(0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / f"memory-{stamp}-{uuid4().hex[:12]}.sqlite3"
            backup_path.touch(mode=0o600, exist_ok=False)
            try:
                source = _readonly_connection(path)
                destination = sqlite3.connect(str(backup_path), timeout=10, isolation_level=None)
                try:
                    source.backup(destination)
                    destination.commit()
                finally:
                    destination.close()
                    source.close()
                if os.name == "posix":
                    backup_path.chmod(0o600)
                backup = _readonly_connection(backup_path)
                try:
                    backup_digest = _legacy_digest(backup)
                    integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
                    violations = backup.execute("PRAGMA foreign_key_check").fetchall()
                    if (
                        backup_digest != source_digest
                        or integrity != "ok"
                        or violations
                    ):
                        raise MemoryBackupVerificationFailed("Online backup validation failed.")
                finally:
                    backup.close()
            except Exception as exc:
                if backup_path.exists() and backup_path.stat().st_size == 0:
                    backup_path.unlink()
                if isinstance(exc, MemoryBackupVerificationFailed):
                    raise
                raise MemoryBackupVerificationFailed("Online backup could not be created or verified.") from None

            _apply_v1_migration(writer)
            writer.commit()

            migrated = _readonly_connection(path)
            try:
                _validate_current_schema(migrated)
                after_digest = _legacy_digest(migrated)
                integrity = migrated.execute("PRAGMA integrity_check").fetchone()[0]
                violations = migrated.execute("PRAGMA foreign_key_check").fetchall()
                journal = migrated.execute("PRAGMA journal_mode").fetchone()[0]
                if after_digest != source_digest or integrity != "ok" or violations:
                    raise MemoryBackupVerificationFailed("Post-migration verification failed.")
            finally:
                migrated.close()

            # Exercise the public Workspace path after the migration is committed.
            workspace = MemoryStore(path).workspace_scope()
            workspace.recent(limit=20)
            report = self.inspect_configured_database()
            return MigrationReport(**{
                **report.to_dict(),
                "backup": backup_path.name,
                "migrated": True,
                "message": "Backup verified and schema migration completed.",
            })
        except Exception:
            if writer.in_transaction:
                writer.rollback()
            raise
        finally:
            writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or explicitly migrate Workspace memory storage.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("inspect", help="Read-only database inspection.")
    migrate = subparsers.add_parser("backup-and-migrate", help="Create and verify a backup before migration.")
    migrate.add_argument(
        "--confirm", action="store_true",
        help="Required to create a backup and modify the configured database.",
    )
    args = parser.parse_args(argv)
    manager = MemoryMigrationManager()
    try:
        if args.command == "backup-and-migrate":
            report = manager.backup_and_migrate_configured_database(confirmed=args.confirm)
        else:
            report = manager.inspect_configured_database()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Exception messages are deliberately sanitized by repository errors.
        print(f"memory migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
