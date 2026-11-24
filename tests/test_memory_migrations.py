"""Temporary-database tests for explicit, backup-first memory migration."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.memory_migrations import MemoryMigrationManager
from agent.memory_store import (
    MemoryBackupVerificationFailed,
    MemoryDataIntegrityError,
    MemoryMigrationRequired,
    MemoryStore,
)


def make_legacy_database(path: Path, *, malformed_tags: bool = False) -> list[tuple]:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_memories_created_at ON memories(created_at DESC)"
    )
    rows = [
        (7, "Legacy workspace memory about CNC plasma.", "[\"engineering\",\"legacy\"]", "2025-01-01T00:00:00+00:00"),
        (12, "Legacy memory about SQLite migrations.", "not-json" if malformed_tags else "[]", "2025-01-02T00:00:00+00:00"),
    ]
    connection.executemany(
        "INSERT INTO memories(id, content, tags, created_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return rows


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "memory.sqlite3"
        self.previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.previous_env = os.environ.get("WORKSPACE_MEMORY_DB")
        os.environ["WORKSPACE_MEMORY_DB"] = str(self.db_path)

    def tearDown(self):
        if self.previous_env is None:
            os.environ.pop("WORKSPACE_MEMORY_DB", None)
        else:
            os.environ["WORKSPACE_MEMORY_DB"] = self.previous_env
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def test_existing_legacy_database_is_not_migrated_on_store_construction(self):
        original = make_legacy_database(self.db_path)
        before = sqlite3.connect(self.db_path)
        before_schema = before.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        before.close()

        with self.assertRaises(MemoryMigrationRequired):
            MemoryStore(self.db_path)

        after = sqlite3.connect(self.db_path)
        after_schema = after.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        after_rows = after.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY id"
        ).fetchall()
        after.close()
        self.assertEqual(before_schema, after_schema)
        self.assertEqual(original, after_rows)

    def test_read_only_inspection_and_dry_run_do_not_create_backup_or_change_source(self):
        original = make_legacy_database(self.db_path)
        manager = MemoryMigrationManager()
        report = manager.inspect_configured_database()
        self.assertTrue(report.legacy_schema)
        self.assertEqual(2, report.row_count)
        self.assertEqual(7, report.minimum_id)
        self.assertEqual(12, report.maximum_id)
        dry_run = manager.backup_and_migrate_configured_database(confirmed=False)
        self.assertFalse(dry_run.migrated)
        self.assertIn("Dry run", dry_run.message)
        self.assertFalse((self.root / ".data" / "backups").exists())

        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY id"
        ).fetchall()
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
        self.assertEqual(original, rows)
        self.assertNotIn("memory_schema_migrations", tables)

    def test_confirmed_migration_creates_verified_backup_and_preserves_legacy_values(self):
        original = make_legacy_database(self.db_path)
        report = MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)
        self.assertTrue(report.migrated)
        self.assertEqual(1, report.schema_version)
        self.assertTrue(report.backup)
        backup_path = self.root / ".data" / "backups" / report.backup
        self.assertTrue(backup_path.is_file())
        if os.name == "posix":
            self.assertEqual(0o600, backup_path.stat().st_mode & 0o777)

        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT id, content, tags, created_at, namespace, memory_type, "
            "visibility, sensitivity, owner_agent FROM memories ORDER BY id"
        ).fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        fk = connection.execute("PRAGMA foreign_key_check").fetchall()
        ledger = connection.execute(
            "SELECT version, name FROM memory_schema_migrations"
        ).fetchall()
        connection.close()
        self.assertEqual(
            [(*row, "workspace", "legacy", "internal", "unknown", "workspace") for row in original],
            rows,
        )
        self.assertEqual("ok", integrity)
        self.assertEqual([], fk)
        self.assertEqual([(1, "namespace_acl_audit_v1")], ledger)

        backup = sqlite3.connect(backup_path)
        backup_rows = backup.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY id"
        ).fetchall()
        backup.close()
        self.assertEqual(original, backup_rows)

        store = MemoryStore(self.db_path)
        self.assertEqual(2, len(store.recent()))

    def test_concurrent_new_database_initializers_serialize(self):
        from concurrent.futures import ThreadPoolExecutor

        path = self.root / "concurrent.sqlite3"
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(pool.map(lambda _: MemoryStore(path), range(2)))
        self.assertEqual(2, len(stores))
        connection = sqlite3.connect(path)
        self.assertEqual(
            [(1, "namespace_acl_audit_v1")],
            connection.execute(
                "SELECT version, name FROM memory_schema_migrations"
            ).fetchall(),
        )
        self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        connection.close()

    def test_concurrent_creator_delayed_after_open_does_not_look_like_legacy_empty_file(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        import agent.memory_store as module
        opened = threading.Event()
        observed_empty = threading.Event()
        release = threading.Event()
        original_connect = module._connect_database
        original_tables = module._table_names
        creator_id = []
        def delayed_connect(path, **kwargs):
            connection = original_connect(path, **kwargs)
            if threading.get_ident() in creator_id:
                opened.set()
                if not release.wait(3):
                    connection.close()
                    raise AssertionError('creator release timed out')
            return connection
        def observed_tables(connection):
            tables = original_tables(connection)
            if not tables and threading.get_ident() not in creator_id:
                observed_empty.set()
            return tables
        def creator():
            creator_id.append(threading.get_ident())
            return MemoryStore(self.db_path)
        with patch.object(module, '_connect_database', side_effect=delayed_connect), patch.object(module, '_table_names', side_effect=observed_tables):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(creator)
                try:
                    self.assertTrue(opened.wait(3))
                    second = pool.submit(MemoryStore, self.db_path)
                    self.assertTrue(observed_empty.wait(3))
                finally:
                    release.set()
                self.assertIsInstance(first.result(timeout=3), MemoryStore)
                self.assertIsInstance(second.result(timeout=3), MemoryStore)
        self.assertEqual([], MemoryStore(self.db_path).recent())

    def test_preexisting_empty_database_is_still_refused_without_changes(self):
        from agent.memory_store import UnsupportedLegacySchema
        self.db_path.touch()
        before = self.db_path.read_bytes()
        with self.assertRaises(UnsupportedLegacySchema):
            MemoryStore(self.db_path)
        self.assertEqual(before, self.db_path.read_bytes())

    def test_migration_is_idempotent(self):
        make_legacy_database(self.db_path)
        manager = MemoryMigrationManager()
        first = manager.backup_and_migrate_configured_database(confirmed=True)
        second = manager.backup_and_migrate_configured_database(confirmed=True)
        self.assertTrue(first.migrated)
        self.assertFalse(second.migrated)
        connection = sqlite3.connect(self.db_path)
        version_rows = connection.execute(
            "SELECT version FROM memory_schema_migrations"
        ).fetchall()
        acl_rows = connection.execute("SELECT COUNT(*) FROM memory_namespace_acl").fetchone()[0]
        connection.close()
        self.assertEqual([(1,)], version_rows)
        self.assertEqual(6, acl_rows)

    def test_mem_mig_12_failed_backup_verification_preserves_source_bytes(self):
        original = make_legacy_database(self.db_path)
        before = self.db_path.read_bytes()
        with patch('agent.memory_migrations._legacy_digest', side_effect=[
            (2, 7, 12, 'synthetic-source'), (2, 7, 12, 'synthetic-mismatch')
        ]):
            with self.assertRaises(MemoryBackupVerificationFailed):
                MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)
        self.assertEqual(before, self.db_path.read_bytes())
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(original, db.execute('SELECT id,content,tags,created_at FROM memories ORDER BY id').fetchall())
        self.assertEqual([], list((self.root/'.data'/'backups').iterdir()))

    def test_mem_acl_06_migrated_legacy_rows_hidden_from_adult(self):
        make_legacy_database(self.db_path)
        MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)
        store = MemoryStore(self.db_path)
        self.assertEqual(2, len(store.recent()))
        self.assertEqual([], store.adult_scope().recent())
        self.assertEqual([], store.adult_scope().recall('Legacy'))
        self.assertEqual([], store.adult_scope().recall_shareable_workspace('Legacy'))

    def test_migration_failure_rolls_back_source_and_retains_verified_backup(self):
        original = make_legacy_database(self.db_path)
        with patch("agent.memory_migrations._create_indexes", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)

        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY id"
        ).fetchall()
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        connection.close()
        self.assertEqual(original, rows)
        self.assertNotIn("memory_schema_migrations", tables)
        backups = list((self.root / ".data" / "backups").glob("memory-*.sqlite3"))
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        self.assertEqual(original, backup.execute(
            "SELECT id, content, tags, created_at FROM memories ORDER BY id"
        ).fetchall())
        backup.close()

    def test_malformed_legacy_tags_are_preserved_and_reported_on_read(self):
        original = make_legacy_database(self.db_path, malformed_tags=True)
        MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)
        connection = sqlite3.connect(self.db_path)
        stored = connection.execute(
            "SELECT id, tags FROM memories WHERE id=12"
        ).fetchone()
        connection.close()
        self.assertEqual((12, original[1][2]), stored)
        with self.assertRaises(MemoryDataIntegrityError) as caught:
            MemoryStore(self.db_path).recent()
        self.assertEqual(12, caught.exception.memory_id)
        self.assertNotIn("Legacy memory", str(caught.exception))

    def test_unsupported_legacy_schema_is_rejected_without_replacement(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, body TEXT)")
        connection.commit()
        connection.close()
        before = sqlite3.connect(self.db_path)
        schema_before = before.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        before.close()
        with self.assertRaises(Exception):
            MemoryMigrationManager().backup_and_migrate_configured_database(confirmed=True)
        after = sqlite3.connect(self.db_path)
        schema_after = after.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        after.close()
        self.assertEqual(schema_before, schema_after)


if __name__ == "__main__":
    unittest.main()
