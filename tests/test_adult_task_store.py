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

    def test_task_creation_failure_after_each_insert_rolls_back_all_rows(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        original = store._connect
        failure_points = (
            "INSERT INTO ADULT_TASKS",
            "INSERT INTO ADULT_STEPS",
            "INSERT INTO ADULT_CHECKPOINTS",
            "INSERT INTO ADULT_TASK_EVENTS",
        )

        class FailingConnection:
            def __init__(self, wrapped, failure_point):
                self.wrapped = wrapped
                self.failure_point = failure_point
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith(self.failure_point):
                    raise sqlite3.OperationalError("injected test failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        for failure_point in failure_points:
            with self.subTest(failure_point=failure_point):
                with patch.object(
                    store, "_connect",
                    side_effect=lambda point=failure_point: FailingConnection(original(), point),
                ):
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
                    self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                    self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_task_can_be_reloaded_by_a_fresh_store_instance(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        request = AdultTaskRequest(
            "text.generate", "hmac-sha256:0123456789abcdef",
            instruction_fingerprint="hmac-sha256:fedcba9876543210",
        )
        created = store.create_task(request, TaskBudget())
        reopened = AdultTaskStore(self.db)
        self.assertEqual(created, reopened.get_task(created.task_id))
        self.assertEqual(request, reopened.get_task_request(created.task_id))
        self.assertEqual("initial", reopened.get_latest_checkpoint(created.task_id).reason)


    def test_transition_updates_state_and_appends_event_atomically(self):
        from agent.adult.adult_task_store import StaleTaskVersion
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        updated = store.transition(
            task.task_id, {"queued"}, "cancelled", "state_transition", 0,
            metadata={"reason_code": "user_cancelled"},
        )
        self.assertEqual("cancelled", updated.status)
        self.assertTrue(updated.cancel_requested)
        self.assertFalse(updated.user_action_required)
        self.assertEqual(1, updated.state_version)
        events = store.list_events(task.task_id)
        self.assertEqual(2, len(events))
        self.assertEqual(("state_transition", "queued", "cancelled"), (
            events[-1].event_type, events[-1].from_status, events[-1].to_status,
        ))
        self.assertEqual({"reason_code": "user_cancelled"}, events[-1].metadata)
        with self.assertRaises(StaleTaskVersion):
            store.transition(task.task_id, {"queued"}, "failed", "state_transition", 0)

    def test_transition_rejects_stale_status_or_version_without_partial_write(self):
        from agent.adult.adult_task_store import StaleTaskVersion
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        for expected_status, expected_version in (({"running"}, 0), ({"queued"}, 1)):
            with self.subTest(expected_status=expected_status, expected_version=expected_version):
                with self.assertRaises(StaleTaskVersion):
                    store.transition(
                        task.task_id, expected_status, "failed",
                        "state_transition", expected_version,
                    )
                current = store.get_task(task.task_id)
                self.assertEqual(("queued", 0), (current.status, current.state_version))
                self.assertEqual(1, len(store.list_events(task.task_id)))

    def test_transition_rejects_disallowed_and_terminal_state_changes(self):
        from agent.adult.adult_task_store import InvalidTaskTransition, TaskTerminal
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        with self.assertRaises(InvalidTaskTransition):
            store.transition(task.task_id, {"queued"}, "completed", "state_transition", 0)
        failed = store.transition(task.task_id, {"queued"}, "failed", "state_transition", 0)
        with self.assertRaises(TaskTerminal):
            store.transition(task.task_id, {"failed"}, "cancelled", "state_transition", failed.state_version)
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_active_transition_requires_current_unexpired_fenced_lease(self):
        from datetime import datetime, timedelta, timezone
        from agent.adult.models import AdultTaskRequest, TaskBudget, TaskLease
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET status='running',lease_owner='worker-1',
                   lease_expires_at=?,lease_generation=1,state_version=1
                   WHERE task_id=?""",
                (future, task.task_id),
            )
        valid = TaskLease._from_store(task.task_id, "worker-1", 1, future, 1)
        checkpointed = store.transition(
            task.task_id, {"running"}, "checkpointed", "state_transition", 1,
            lease=valid,
        )
        self.assertEqual("checkpointed", checkpointed.status)
        self.assertEqual(2, checkpointed.state_version)
        with store._connect() as connection:
            row = connection.execute(
                "SELECT lease_owner,lease_expires_at FROM adult_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
        self.assertEqual((None, None), tuple(row))

    def test_active_transition_rejects_expired_or_stale_lease_without_writes(self):
        from datetime import datetime, timedelta, timezone
        from agent.adult.adult_task_store import LeaseLost
        from agent.adult.models import AdultTaskRequest, TaskBudget, TaskLease
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET status='running',lease_owner='worker-1',
                   lease_expires_at=?,lease_generation=2,state_version=1
                   WHERE task_id=?""",
                (expired, task.task_id),
            )
        stale = TaskLease._from_store(task.task_id, "worker-1", 1, expired, 1)
        with self.assertRaises(LeaseLost):
            store.transition(
                task.task_id, {"running"}, "checkpointed", "state_transition", 1,
                lease=stale,
            )
        current = store.get_task(task.task_id)
        self.assertEqual(("running", 1), (current.status, current.state_version))
        self.assertEqual(1, len(store.list_events(task.task_id)))

    def test_generic_transition_cannot_bypass_resume_or_waiting_user_gates(self):
        from agent.adult.adult_task_store import UserApprovalRequired
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        with self.assertRaises(UserApprovalRequired):
            store.transition(task.task_id, {"queued"}, "waiting_user", "state_transition", 0)
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET status='checkpointed',state_version=1
                   WHERE task_id=?""",
                (task.task_id,),
            )
        with self.assertRaises(UserApprovalRequired):
            store.transition(task.task_id, {"checkpointed"}, "queued", "state_transition", 1)

    def test_transition_rejects_empty_expected_status_and_unsafe_event_metadata(self):
        from agent.adult.models import AdultModelValidationError, AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        with self.assertRaises(AdultModelValidationError):
            store.transition(task.task_id, set(), "failed", "state_transition", 0)
        with self.assertRaises(AdultModelValidationError):
            store.transition(
                task.task_id, {"queued"}, "failed", "state_transition", 0,
                metadata={"note": "https://example.com"},
            )
        self.assertEqual(("queued", 0), (
            store.get_task(task.task_id).status, store.get_task(task.task_id).state_version,
        ))
        self.assertEqual(1, len(store.list_events(task.task_id)))


    def test_transition_rolls_back_state_when_event_append_fails(self):
        import sqlite3
        from agent.adult.adult_task_store import AdultTaskStoreError
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        original_connect = store._connect

        class EventFailingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("INSERT INTO ADULT_TASK_EVENTS"):
                    raise sqlite3.OperationalError("injected event write failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        with patch.object(
            store, "_connect",
            side_effect=lambda: EventFailingConnection(original_connect()),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                store.transition(
                    task.task_id, {"queued"}, "failed", "state_transition", 0,
                )
        current = store.get_task(task.task_id)
        self.assertEqual(("queued", 0), (current.status, current.state_version))
        self.assertEqual(1, len(store.list_events(task.task_id)))

if __name__ == "__main__":
    unittest.main()
