"""Tests for the isolated Adult operational database schema."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_task_store import AdultTaskStore, SCHEMA_NAME


class AdultTaskStoreSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "adult.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_new_database_has_exact_stage3_tables_and_versioned_schema(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual({
                "adult_schema_migrations", "adult_tasks", "adult_steps",
                "adult_checkpoints", "adult_task_events",
            }, tables)
            migration = connection.execute(
                "SELECT version,name FROM adult_schema_migrations"
            ).fetchone()
            self.assertEqual((1, SCHEMA_NAME), tuple(migration))
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])

    def test_migration_is_idempotent_and_schema_objects_are_validated(self):
        AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as connection:
            before = connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as connection:
            after = connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        self.assertEqual(before, after)

    def test_every_connection_enables_foreign_keys_and_delete_journal(self):
        store = AdultTaskStore(self.db)
        connection = store._connect()
        try:
            self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(10_000, connection.execute("PRAGMA busy_timeout").fetchone()[0])
        finally:
            connection.close()

    def test_migration_failure_rolls_back_all_schema_objects(self):
        broken_schema = (
            "CREATE TABLE adult_schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL)",
            "CREATE TABLE intentionally_broken (",
        )
        with patch("agent.adult.adult_task_store._SCHEMA", broken_schema):
            with self.assertRaises(sqlite3.OperationalError):
                AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as connection:
            objects = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        self.assertEqual([], objects)

    def test_sql_constraints_reject_invalid_task_and_event_values(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO adult_tasks
                    (task_id,task_type,status,input_fingerprint,retention_policy,
                     max_total_attempts,max_wall_seconds,max_provider_calls,
                     max_cost_microusd,spent_cost_microusd,deadline_at,created_at,updated_at)
                    VALUES ('task-1','shell.exec','queued','hmac-sha256:0123456789abcdef',
                            'ephemeral',20,600,0,0,0,'2026-11-21T00:00:00.000000Z',
                            '2026-11-21T00:00:00.000000Z','2026-11-21T00:00:00.000000Z')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO adult_task_events
                    (event_id,task_id,event_type,metadata_json,created_at)
                    VALUES ('event-1','missing-task','not-an-event','{}',
                            '2026-11-21T00:00:00.000000Z')"""
                )

    def test_task_and_history_delete_triggers_preserve_records(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            connection.execute(
                """INSERT INTO adult_tasks
                (task_id,task_type,status,input_fingerprint,retention_policy,
                 max_total_attempts,max_wall_seconds,max_provider_calls,
                 max_cost_microusd,spent_cost_microusd,deadline_at,created_at,updated_at)
                VALUES ('task-1','text.analyze','queued','hmac-sha256:0123456789abcdef',
                        'ephemeral',20,600,0,0,0,'2026-11-21T00:00:00.000000Z',
                        '2026-11-21T00:00:00.000000Z','2026-11-21T00:00:00.000000Z')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM adult_tasks WHERE task_id='task-1'")
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_tasks WHERE task_id='task-1'"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
