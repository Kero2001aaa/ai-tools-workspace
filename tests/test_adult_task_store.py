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


    def test_create_task_atomically_persists_safe_request_step_checkpoint_and_event(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        request = AdultTaskRequest(
            "text.analyze", "hmac-sha256:0123456789abcdef",
            instruction_fingerprint="hmac-sha256:fedcba9876543210",
        )
        task = store.create_task(request, TaskBudget(max_total_attempts=7, max_wall_seconds=90))
        self.assertEqual("queued", task.status)
        self.assertEqual(0, task.state_version)
        self.assertEqual(7, task.max_total_attempts)
        self.assertEqual(90, task.max_wall_seconds)
        self.assertIsNotNone(task.current_step_id)
        with store._connect() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_steps WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_checkpoints WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_task_events WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
        self.assertEqual(request, store.get_task_request(task.task_id))
        checkpoint = store.get_latest_checkpoint(task.task_id)
        self.assertEqual("initial", checkpoint.reason)
        self.assertEqual("queued", checkpoint.snapshot.status)
        self.assertEqual([("task_created", "queued")], [
            (event.event_type, event.to_status) for event in store.list_events(task.task_id)
        ])

    def test_task_queries_are_safe_and_missing_ids_return_none(self):
        from agent.adult.models import AdultTaskRequest
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("image.analyze", "hmac-sha256:0123456789abcdef"),
            __import__("agent.adult.models", fromlist=["TaskBudget"]).TaskBudget(),
        )
        self.assertIsNone(store.get_task("missing-task"))
        self.assertIsNone(store.get_task_request("missing-task"))
        self.assertIsNone(store.get_latest_checkpoint("missing-task"))
        page = store.list_tasks(status="queued", limit=1)
        self.assertEqual((task.task_id,), tuple(item.task_id for item in page.items))
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_cursor)

    def test_task_list_cursor_paginates_and_rejects_malformed_values(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        for _ in range(3):
            store.create_task(
                AdultTaskRequest("text.generate", "hmac-sha256:0123456789abcdef"),
                TaskBudget(),
            )
        first = store.list_tasks(limit=2)
        self.assertEqual(2, len(first.items))
        self.assertTrue(first.has_more)
        self.assertIsNotNone(first.next_cursor)
        second = store.list_tasks(limit=2, cursor=first.next_cursor)
        self.assertEqual(1, len(second.items))
        self.assertFalse(second.has_more)
        self.assertTrue(set(x.task_id for x in first.items).isdisjoint(
            x.task_id for x in second.items
        ))
        for invalid in ("!", "e30", "A" * 2049):
            with self.subTest(cursor=invalid), self.assertRaises(Exception):
                store.list_tasks(cursor=invalid)

    def test_encrypted_retention_is_rejected_before_any_task_write(self):
        from agent.adult.adult_task_store import PayloadRetentionUnavailable
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        request = AdultTaskRequest(
            "text.generate", "hmac-sha256:0123456789abcdef",
            retention_policy="encrypted",
        )
        with self.assertRaises(PayloadRetentionUnavailable):
            store.create_task(request, TaskBudget())
        with store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM adult_tasks").fetchone()[0])

    def test_task_creation_failure_rolls_back_all_rows(self):
        from unittest.mock import patch
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        original = store._connect

        class FailingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.inserts = 0
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("INSERT INTO ADULT_CHECKPOINTS"):
                    raise sqlite3.OperationalError("injected test failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        with patch.object(store, "_connect", side_effect=lambda: FailingConnection(original())):
            with self.assertRaises(sqlite3.OperationalError):
                store.create_task(
                    AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
                    TaskBudget(),
                )
        with original() as connection:
            for table in ("adult_tasks", "adult_steps", "adult_checkpoints", "adult_task_events"):
                self.assertEqual(0, connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
