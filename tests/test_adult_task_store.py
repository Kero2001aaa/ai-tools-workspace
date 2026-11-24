"""Tests for the isolated Adult operational database schema."""
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_task_store import (
    AdultTaskStore, TaskStoreSchemaError, SCHEMA_NAME, ATTEMPT_SCHEMA_NAME,
    CAPABILITY_SCHEMA_NAME, _SCHEMA,
)


class AdultTaskStoreSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "adult.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _make_task(self, store=None, budget=None):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        selected_store = store or AdultTaskStore(self.db)
        selected_budget = budget or TaskBudget()
        task = selected_store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            selected_budget,
        )
        return selected_store, task

    @staticmethod
    def _checkpoint_snapshot(task, *, status=None, step_ids=None, **overrides):
        from agent.adult.models import CheckpointSnapshot
        status = status or task.status
        step_ids = tuple(step_ids if step_ids is not None else ([task.current_step_id] if task.current_step_id else []))
        values = {
            "schema_version": 1,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "status": status,
            "active_step_id": task.current_step_id,
            "input_fingerprint": "hmac-sha256:0123456789abcdef",
            "plan": step_ids,
            "completed_step_ids": (),
            "pending_step_ids": step_ids,
            "total_attempts": task.attempt_count,
            "failed_attempts_since_approval": task.failed_attempts_since_approval,
            "provider_calls": task.provider_calls,
            "spent_cost_microusd": task.spent_cost_microusd,
            "remaining_attempts": max(0, task.max_total_attempts - task.attempt_count),
            "remaining_wall_seconds": task.max_wall_seconds,
            "remaining_provider_calls": max(0, task.max_provider_calls - task.provider_calls),
            "remaining_cost_microusd": max(0, task.max_cost_microusd - task.spent_cost_microusd),
        }
        values.update(overrides)
        return CheckpointSnapshot(**values)

    def test_get_step_returns_only_the_requested_task_owned_step(self):
        store, task = self._make_task()
        step = store.get_step(task.task_id, task.current_step_id)
        self.assertIsNotNone(step)
        self.assertEqual((task.task_id, task.current_step_id, "intake", "pending"), (
            step.task_id, step.step_id, step.operation, step.status,
        ))
        self.assertIsNone(store.get_step(task.task_id, "step-not-owned-0001"))

    def test_save_step_inserts_and_advances_with_server_timestamps(self):
        from agent.adult.models import StepRecord
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "step-worker", lease_seconds=300)
        initial = StepRecord(
            step_id=task.current_step_id, task_id=task.task_id,
            sequence_no=0, operation="intake", status="running",
        )
        started = store.save_step(task.task_id, initial, lease, 1)
        self.assertEqual("running", started.status)
        self.assertIsNotNone(started.started_at)
        self.assertIsNone(started.completed_at)
        current = store.get_task(task.task_id)
        self.assertEqual(2, current.state_version)

        finished = store.save_step(
            task.task_id,
            StepRecord(
                step_id=task.current_step_id, task_id=task.task_id,
                sequence_no=0, operation="intake", status="succeeded",
                adapter_id="adapter-1",
            ),
            lease, current.state_version,
        )
        self.assertEqual("succeeded", finished.status)
        self.assertEqual("adapter-1", finished.adapter_id)
        self.assertIsNotNone(finished.completed_at)
        self.assertLessEqual(finished.started_at, finished.completed_at)
        self.assertEqual(3, store.get_task(task.task_id).state_version)
        self.assertEqual(
            ["task_created", "task_claimed", "step_transition", "step_transition"],
            [event.event_type for event in store.list_events(task.task_id)],
        )

    def test_save_step_adds_next_sequence_and_rejects_caller_timestamps(self):
        from agent.adult.adult_task_store import InvalidTaskTransition
        from agent.adult.models import AdultModelValidationError, StepRecord
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "step-worker", lease_seconds=300)
        with self.assertRaises(AdultModelValidationError):
            store.save_step(
                task.task_id,
                StepRecord(
                    step_id="step-with-time-001", task_id=task.task_id,
                    sequence_no=1, operation="text.analyze", status="pending",
                    started_at="2026-11-21T00:00:00.000000Z",
                ),
                lease, 1,
            )
        with self.assertRaises(AdultModelValidationError):
            store.save_step(
                task.task_id,
                StepRecord(
                    step_id="step-out-of-order-01", task_id=task.task_id,
                    sequence_no=2, operation="text.analyze", status="pending",
                ),
                lease, 1,
            )
        added = store.save_step(
            task.task_id,
            StepRecord(
                step_id="step-new-sequence-001", task_id=task.task_id,
                sequence_no=1, operation="text.analyze", status="pending",
            ),
            lease, 1,
        )
        self.assertEqual(1, added.sequence_no)
        with self.assertRaises(InvalidTaskTransition):
            store.save_step(
                task.task_id,
                StepRecord(
                    step_id=added.step_id, task_id=task.task_id,
                    sequence_no=1, operation="text.analyze", status="succeeded",
                ),
                lease, 2,
            )
        current = store.get_task(task.task_id)
        self.assertEqual(2, current.state_version)

    def test_save_step_rejects_cross_task_and_stale_lease_without_partial_write(self):
        from agent.adult.adult_task_store import LeaseLost, StaleTaskVersion
        from agent.adult.models import StepRecord, TaskLease
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "step-worker", lease_seconds=300)
        with self.assertRaises(StaleTaskVersion):
            store.save_step(
                task.task_id,
                StepRecord(
                    task.current_step_id, task.task_id, 0, "intake", "running"
                ),
                lease, 0,
            )
        with self.assertRaises(LeaseLost):
            store.save_step(
                task.task_id,
                StepRecord(
                    task.current_step_id, task.task_id, 0, "intake", "running"
                ),
                TaskLease._from_store(task.task_id, "other-worker", lease.lease_generation, lease.expires_at, 1),
                1,
            )
        self.assertEqual(1, store.get_task(task.task_id).state_version)
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_rejected_path_metadata_leaves_task_and_event_history_unchanged(self):
        from agent.adult.models import AdultModelValidationError
        store, task = self._make_task()
        before = store.list_events(task.task_id)
        with self.assertRaises(AdultModelValidationError):
            store.transition(task.task_id, {'queued'}, 'failed', 'state_transition', 0,
                             metadata={'diagnostic':{'nested':['/var/synthetic/private']}})
        self.assertEqual(task, store.get_task(task.task_id))
        self.assertEqual(before, store.list_events(task.task_id))

    def test_checkpoint_reader_rejects_unsupported_persisted_version(self):
        import json
        store, task = self._make_task()
        original = store.get_latest_checkpoint(task.task_id)
        self.assertEqual(1, original.schema_version)
        with store._connect() as db:
            db.execute('DROP TRIGGER prevent_adult_checkpoints_update')
            raw = original.snapshot.to_dict()
            raw['schema_version'] = 999
            db.execute('UPDATE adult_checkpoints SET schema_version=999,snapshot_json=? WHERE task_id=?',
                       (json.dumps(raw), task.task_id))
        with self.assertRaises(TaskStoreSchemaError) as caught:
            store.get_latest_checkpoint(task.task_id)
        self.assertNotIn('999', str(caught.exception))
        with store._connect() as db:
            row = db.execute('SELECT * FROM adult_checkpoints WHERE task_id=?', (task.task_id,)).fetchone()
            with self.assertRaises(TaskStoreSchemaError):
                store._checkpoint_from_row(row)

    def test_save_checkpoint_appends_revision_and_updates_task_version_atomically(self):
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "checkpoint-worker", lease_seconds=300)
        snapshot = self._checkpoint_snapshot(store.get_task(task.task_id))
        checkpoint = store.save_checkpoint(
            task.task_id, snapshot, "manual_yield", 1, lease=lease
        )
        self.assertEqual(2, checkpoint.revision)
        self.assertEqual("analyzing", checkpoint.snapshot.status)
        current = store.get_task(task.task_id)
        self.assertEqual(2, current.state_version)
        next_snapshot = self._checkpoint_snapshot(current)
        second = store.save_checkpoint(
            task.task_id, next_snapshot, "step_completed", current.state_version, lease=lease
        )
        self.assertEqual(3, second.revision)
        self.assertEqual(second.checkpoint_id, store.get_latest_checkpoint(task.task_id).checkpoint_id)
        self.assertEqual("checkpoint_saved", store.list_events(task.task_id)[-1].event_type)

    def test_save_checkpoint_rejects_stale_snapshot_and_rolls_back(self):
        from agent.adult.adult_task_store import StaleTaskVersion
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "checkpoint-worker", lease_seconds=300)
        snapshot = self._checkpoint_snapshot(store.get_task(task.task_id))
        with self.assertRaises(StaleTaskVersion):
            store.save_checkpoint(task.task_id, snapshot, "manual_yield", 0, lease=lease)
        with store._connect() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_checkpoints WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
        self.assertEqual(1, store.get_task(task.task_id).state_version)

    def test_set_waiting_user_requires_five_failures_and_releases_lease(self):
        from agent.adult.adult_task_store import UserApprovalRequired
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "approval-worker", lease_seconds=300)
        snapshot = self._checkpoint_snapshot(
            store.get_task(task.task_id), status="waiting_user",
            pending_user_action="approval_required",
            allowed_decisions=("continue", "cancel"),
        )
        with self.assertRaises(UserApprovalRequired):
            store.set_waiting_user(task.task_id, snapshot, lease, 1)
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET total_attempts=5,failed_attempts_since_approval=5
                   WHERE task_id=?""",
                (task.task_id,),
            )
        current = store.get_task(task.task_id)
        snapshot = self._checkpoint_snapshot(
            current, status="waiting_user",
            failed_attempts_since_approval=5, total_attempts=5,
            pending_user_action="approval_required",
            allowed_decisions=("continue", "cancel"),
        )
        checkpoint = store.set_waiting_user(task.task_id, snapshot, lease, current.state_version)
        current = store.get_task(task.task_id)
        self.assertEqual(("waiting_user", 1, 5), (
            current.status, current.user_action_required,
            current.failed_attempts_since_approval,
        ))
        self.assertEqual("approval_required", checkpoint.reason)
        with store._connect() as connection:
            row = connection.execute(
                "SELECT lease_owner,lease_expires_at FROM adult_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
        self.assertEqual((None, None), tuple(row))

    def test_set_waiting_user_with_exhausted_total_budget_fails_task_instead(self):
        from agent.adult.adult_task_store import TaskBudgetExhausted
        from agent.adult.models import TaskBudget
        store, task = self._make_task(budget=TaskBudget(max_total_attempts=5))
        lease = store.claim_task(task.task_id, "approval-worker", lease_seconds=300)
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET total_attempts=5,failed_attempts_since_approval=5
                   WHERE task_id=?""",
                (task.task_id,),
            )
        current = store.get_task(task.task_id)
        snapshot = self._checkpoint_snapshot(
            current, status="waiting_user",
            failed_attempts_since_approval=5, total_attempts=5,
            pending_user_action="approval_required",
            allowed_decisions=("continue", "cancel"),
        )
        with self.assertRaises(TaskBudgetExhausted):
            store.set_waiting_user(task.task_id, snapshot, lease, current.state_version)
        current = store.get_task(task.task_id)
        self.assertEqual("failed", current.status)
        self.assertEqual(0, current.user_action_required)
        self.assertEqual("budget_exhausted", store.list_events(task.task_id)[-1].event_type)

    def test_resume_waiting_user_requires_explicit_continue_and_resets_approval_counter(self):
        from agent.adult.models import TaskBudget
        store, task = self._make_task(budget=TaskBudget(max_total_attempts=10))
        lease = store.claim_task(task.task_id, "approval-worker", lease_seconds=300)
        with store._connect() as connection:
            connection.execute(
                """UPDATE adult_tasks SET total_attempts=5,failed_attempts_since_approval=5
                   WHERE task_id=?""",
                (task.task_id,),
            )
        current = store.get_task(task.task_id)
        snapshot = self._checkpoint_snapshot(
            current, status="waiting_user",
            failed_attempts_since_approval=5, total_attempts=5,
            pending_user_action="approval_required",
            allowed_decisions=("continue", "cancel"),
        )
        store.set_waiting_user(task.task_id, snapshot, lease, current.state_version)
        resumed = store.resume(task.task_id, "continue")
        self.assertEqual(("queued", 1, 0, 0), (
            resumed.status, resumed.approvals_granted,
            resumed.failed_attempts_since_approval, resumed.user_action_required,
        ))
        self.assertEqual("task_resumed", store.list_events(task.task_id)[-1].event_type)

    def test_resume_checkpointed_does_not_increment_approval_counter(self):
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "resume-worker", lease_seconds=300)
        checkpoint = self._checkpoint_snapshot(store.get_task(task.task_id))
        saved = store.save_checkpoint(task.task_id, checkpoint, "manual_yield", 1, lease=lease)
        current = store.get_task(task.task_id)
        store.release_lease(
            lease, current.state_version, next_status="checkpointed", reason="yielded"
        )
        resumed = store.resume(task.task_id, "continue")
        self.assertEqual("queued", resumed.status)
        self.assertEqual(0, resumed.approvals_granted)
        self.assertEqual(0, resumed.failed_attempts_since_approval)

    def test_resume_rejects_needs_input_and_terminal_states(self):
        from agent.adult.adult_task_store import TaskTerminal, UserApprovalRequired
        store, task = self._make_task()
        with store._connect() as connection:
            connection.execute(
                "UPDATE adult_tasks SET status='needs_input',user_action_required=1 WHERE task_id=?",
                (task.task_id,),
            )
        with self.assertRaises(UserApprovalRequired):
            store.resume(task.task_id, "continue")
        cancelled = store.resume(task.task_id, "cancel")
        self.assertEqual("cancelled", cancelled.status)
        with self.assertRaises(TaskTerminal):
            store.resume(task.task_id, "continue")

    def test_request_cancel_is_idempotent_for_queued_and_active_tasks(self):
        store, queued = self._make_task()
        cancelled = store.request_cancel(queued.task_id)
        self.assertEqual("cancelled", cancelled.status)
        event_count = len(store.list_events(queued.task_id))
        self.assertEqual("cancelled", store.request_cancel(queued.task_id).status)
        self.assertEqual(event_count, len(store.list_events(queued.task_id)))

        active_store = AdultTaskStore(Path(self.temp.name) / "active-cancel.sqlite3")
        active, active_task = self._make_task(active_store)
        active_lease = active.claim_task(active_task.task_id, "active-cancel-worker", lease_seconds=300)
        cancel_requested = active.request_cancel(active_task.task_id)
        self.assertEqual(("analyzing", True), (
            cancel_requested.status, cancel_requested.cancel_requested,
        ))
        event_count = len(active.list_events(active_task.task_id))
        again = active.request_cancel(active_task.task_id)
        self.assertTrue(again.cancel_requested)
        self.assertEqual(event_count, len(active.list_events(active_task.task_id)))
        from agent.adult.adult_task_store import TaskCancelled
        with self.assertRaises(TaskCancelled):
            active.renew_lease(active_lease, lease_seconds=30)

    def test_recover_expired_lease_creates_checkpoint_and_increments_fencing_generation(self):
        from datetime import datetime, timedelta, timezone
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "recovery-worker", lease_seconds=1)
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            self.assertEqual(1, store.recover_expired_leases())
            self.assertEqual(0, store.recover_expired_leases())
        recovered = store.get_task(task.task_id)
        self.assertEqual(("checkpointed", 2, 0), (
            recovered.status, recovered.state_version, recovered.attempt_count,
        ))
        with store._connect() as connection:
            row = connection.execute(
                "SELECT lease_generation,lease_owner,lease_expires_at FROM adult_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
        self.assertEqual((lease.lease_generation + 1, None, None), tuple(row))
        checkpoint = store.get_latest_checkpoint(task.task_id)
        self.assertEqual("lease_recovered", checkpoint.reason)
        self.assertEqual("checkpointed", checkpoint.snapshot.status)
        self.assertEqual("lease_expired_recovered", store.list_events(task.task_id)[-1].event_type)

    def test_stale_lease_cannot_renew_release_or_mutate_after_recovery(self):
        from datetime import datetime, timedelta, timezone
        from agent.adult.adult_task_store import LeaseLost
        from agent.adult.models import StepRecord
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "stale-generation-worker", lease_seconds=1)
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            self.assertEqual(1, store.recover_expired_leases())
        with self.assertRaises(LeaseLost):
            store.renew_lease(lease, lease_seconds=30)
        with self.assertRaises(LeaseLost):
            store.release_lease(lease, expected_state_version=1)
        with self.assertRaises(LeaseLost):
            store.save_step(
                task.task_id,
                StepRecord(task.current_step_id, task.task_id, 0, "intake", "running"),
                lease, 2,
            )
        current = store.get_task(task.task_id)
        self.assertEqual(("checkpointed", 2), (current.status, current.state_version))

    def test_recover_expired_lease_with_cancel_request_transitions_to_cancelled(self):
        from datetime import datetime, timedelta, timezone
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "recovery-cancel-worker", lease_seconds=1)
        store.request_cancel(task.task_id)
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            self.assertEqual(1, store.recover_expired_leases())
        recovered = store.get_task(task.task_id)
        self.assertEqual(("cancelled", True, 0), (
            recovered.status, recovered.cancel_requested, recovered.user_action_required,
        ))
        checkpoint = store.get_latest_checkpoint(task.task_id)
        self.assertEqual("cancelled", checkpoint.reason)
        self.assertEqual("cancelled", checkpoint.snapshot.status)
        self.assertEqual("cancel_recovered", store.list_events(task.task_id)[-1].event_type)

    def test_generic_transition_cannot_enter_checkpointed_without_durable_checkpoint(self):
        from agent.adult.adult_task_store import InvalidCheckpoint
        store, task = self._make_task()
        with store._connect() as connection:
            connection.execute("DROP TRIGGER prevent_adult_checkpoints_delete")
            connection.execute("DELETE FROM adult_checkpoints WHERE task_id=?", (task.task_id,))
        lease = store.claim_task(task.task_id, "checkpoint-guard-worker", lease_seconds=300)
        with self.assertRaises(InvalidCheckpoint):
            store.transition(
                task.task_id, {"analyzing"}, "checkpointed",
                "state_transition", 1, lease=lease,
            )
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1), (current.status, current.state_version))
        self.assertEqual(["task_created", "task_claimed"], [
            event.event_type for event in store.list_events(task.task_id)
        ])

    def test_save_checkpoint_rolls_back_checkpoint_and_state_when_event_append_fails(self):
        from agent.adult.models import TaskLease
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "checkpoint-rollback-worker", lease_seconds=300)
        snapshot = self._checkpoint_snapshot(store.get_task(task.task_id))
        original = store._connect

        class FailingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("INSERT INTO ADULT_TASK_EVENTS"):
                    raise sqlite3.OperationalError("injected checkpoint event failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        with patch.object(store, "_connect", side_effect=lambda: FailingConnection(original())):
            with self.assertRaises(sqlite3.OperationalError):
                store.save_checkpoint(
                    task.task_id, snapshot, "manual_yield", 1, lease=lease
                )
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1), (current.status, current.state_version))
        with original() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_checkpoints WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM adult_task_events WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])

    def test_save_checkpoint_rejects_terminal_task(self):
        from agent.adult.adult_task_store import TaskTerminal
        store, task = self._make_task()
        failed = store.transition(
            task.task_id, {"queued"}, "failed", "state_transition", 0
        )
        snapshot = self._checkpoint_snapshot(failed)
        with self.assertRaises(TaskTerminal):
            store.save_checkpoint(
                task.task_id, snapshot, "manual_yield", failed.state_version
            )
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_resume_rejects_non_string_decisions_without_type_errors(self):
        from agent.adult.models import AdultModelValidationError
        store, task = self._make_task()
        for value in (None, [], {}, True):
            with self.subTest(value=value), self.assertRaises(AdultModelValidationError):
                store.resume(task.task_id, value)

    def test_new_pending_step_becomes_current_after_previous_step_is_finished(self):
        from agent.adult.models import StepRecord
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "step-order-worker", lease_seconds=300)
        started = store.save_step(
            task.task_id,
            StepRecord(task.current_step_id, task.task_id, 0, "intake", "running"),
            lease, 1,
        )
        current = store.get_task(task.task_id)
        store.save_step(
            task.task_id,
            StepRecord(task.current_step_id, task.task_id, 0, "intake", "succeeded"),
            lease, current.state_version,
        )
        current = store.get_task(task.task_id)
        next_step = StepRecord(
            "step-follow-up-0001", task.task_id, 1, "text.analyze", "pending"
        )
        store.save_step(task.task_id, next_step, lease, current.state_version)
        self.assertEqual(next_step.step_id, store.get_task(task.task_id).current_step_id)

    def test_concurrent_claims_across_tasks_have_exactly_one_winner(self):
        import threading
        from agent.adult.adult_task_store import TaskAlreadyActive
        store, first = self._make_task()
        second = store.create_task(
            __import__("agent.adult.models", fromlist=["AdultTaskRequest"]).AdultTaskRequest(
                "text.generate", "hmac-sha256:fedcba9876543210"
            ),
            __import__("agent.adult.models", fromlist=["TaskBudget"]).TaskBudget(),
        )
        barrier = threading.Barrier(2)
        results = []
        guard = threading.Lock()

        def claim(task_id, worker):
            try:
                barrier.wait(timeout=3)
                lease = store.claim_task(task_id, worker, lease_seconds=60)
                value = ("claimed", lease.task_id)
            except TaskAlreadyActive:
                value = ("blocked", task_id)
            except Exception as exc:
                value = (type(exc).__name__, task_id)
            with guard:
                results.append(value)

        workers = [
            threading.Thread(target=claim, args=(first.task_id, "concurrent-1")),
            threading.Thread(target=claim, args=(second.task_id, "concurrent-2")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(1, sum(value[0] == "claimed" for value in results))
        self.assertEqual(1, sum(value[0] == "blocked" for value in results))
        statuses = {store.get_task(task_id).status for task_id in (first.task_id, second.task_id)}
        self.assertEqual({"queued", "analyzing"}, statuses)

    def test_save_step_rolls_back_step_and_task_when_event_append_fails(self):
        from agent.adult.models import StepRecord
        store, task = self._make_task()
        lease = store.claim_task(task.task_id, "step-rollback-worker", lease_seconds=300)
        original = store._connect

        class FailingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("INSERT INTO ADULT_TASK_EVENTS"):
                    raise sqlite3.OperationalError("injected step event failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        with patch.object(store, "_connect", side_effect=lambda: FailingConnection(original())):
            with self.assertRaises(sqlite3.OperationalError):
                store.save_step(
                    task.task_id,
                    StepRecord(task.current_step_id, task.task_id, 0, "intake", "running"),
                    lease, 1,
                )
        with original() as connection:
            step = connection.execute(
                "SELECT status,started_at FROM adult_steps WHERE step_id=?",
                (task.current_step_id,),
            ).fetchone()
        self.assertEqual(("pending", None), tuple(step))
        self.assertEqual(("analyzing", 1), (
            store.get_task(task.task_id).status, store.get_task(task.task_id).state_version,
        ))
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_recovery_rolls_back_checkpoint_and_lease_when_audit_append_fails(self):
        from datetime import datetime, timedelta, timezone
        store, task = self._make_task()
        store.claim_task(task.task_id, "recovery-rollback-worker", lease_seconds=1)
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        original = store._connect

        class FailingConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("INSERT INTO ADULT_TASK_EVENTS"):
                    raise sqlite3.OperationalError("injected recovery event failure")
                return self.wrapped.execute(sql, parameters)
            def close(self):
                self.wrapped.close()

        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            with patch.object(store, "_connect", side_effect=lambda: FailingConnection(original())):
                with self.assertRaises(sqlite3.OperationalError):
                    store.recover_expired_leases()
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1, 0), (
            current.status, current.state_version, current.attempt_count,
        ))
        with original() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM adult_checkpoints WHERE task_id=?", (task.task_id,)
            ).fetchone()[0])
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_waiting_user_cancel_decision_does_not_grant_approval(self):
        from agent.adult.models import TaskBudget
        store, task = self._make_task(budget=TaskBudget(max_total_attempts=10))
        lease = store.claim_task(task.task_id, "approval-cancel-worker", lease_seconds=300)
        with store._connect() as connection:
            connection.execute(
                "UPDATE adult_tasks SET total_attempts=5,failed_attempts_since_approval=5 WHERE task_id=?",
                (task.task_id,),
            )
        current = store.get_task(task.task_id)
        snapshot = self._checkpoint_snapshot(
            current, status="waiting_user", failed_attempts_since_approval=5,
            total_attempts=5, pending_user_action="approval_required",
            allowed_decisions=("continue", "cancel"),
        )
        store.set_waiting_user(task.task_id, snapshot, lease, current.state_version)
        cancelled = store.resume(task.task_id, "cancel")
        self.assertEqual(("cancelled", 0, 5), (
            cancelled.status, cancelled.approvals_granted,
            cancelled.failed_attempts_since_approval,
        ))
        self.assertEqual("task_cancelled", store.list_events(task.task_id)[-1].event_type)

    def test_terminal_step_cannot_be_reopened_or_reidentified(self):
        from agent.adult.adult_task_store import InvalidTaskTransition
        from agent.adult.models import AdultModelValidationError, StepRecord
        store, task = self._make_task()
        step_id = task.current_step_id
        lease = store.claim_task(task.task_id, "step-final-worker", lease_seconds=300)
        store.save_step(
            task.task_id, StepRecord(step_id, task.task_id, 0, "intake", "running"),
            lease, 1,
        )
        finished = store.save_step(
            task.task_id, StepRecord(step_id, task.task_id, 0, "intake", "succeeded"),
            lease, store.get_task(task.task_id).state_version,
        )
        with self.assertRaises(InvalidTaskTransition):
            store.save_step(
                task.task_id,
                StepRecord(step_id, task.task_id, 0, "intake", "running"),
                lease, store.get_task(task.task_id).state_version,
            )
        with self.assertRaises(AdultModelValidationError):
            store.save_step(
                task.task_id,
                StepRecord(step_id, task.task_id, 0, "text.analyze", "succeeded"),
                lease, store.get_task(task.task_id).state_version,
            )
        self.assertEqual("succeeded", finished.status)

    def test_new_database_has_exact_stage5_tables_and_versioned_schema(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual({
                "adult_schema_migrations", "adult_tasks", "adult_steps",
                "adult_checkpoints", "adult_task_events", "adult_attempts",
                "adult_attempt_events", "adult_model_capabilities",
                "adult_capability_events",
            }, tables)
            migrations = connection.execute(
                "SELECT version,name FROM adult_schema_migrations ORDER BY version"
            ).fetchall()
            self.assertEqual(
                [(1, SCHEMA_NAME), (2, ATTEMPT_SCHEMA_NAME), (3, CAPABILITY_SCHEMA_NAME)],
                [tuple(row) for row in migrations],
            )
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("delete", connection.execute("PRAGMA journal_mode").fetchone()[0])

    def test_stage3_database_upgrades_additively_and_preserves_task_history(self):
        store = AdultTaskStore(self.db)
        from agent.adult.models import AdultTaskRequest, TaskBudget
        created = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        with store._connect() as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("adult_tasks", "adult_steps", "adult_checkpoints", "adult_task_events")
            }
            connection.execute("BEGIN IMMEDIATE")
            for trigger in (
                "validate_adult_capability_update", "prevent_adult_capabilities_delete",
                "prevent_adult_capability_events_update", "prevent_adult_capability_events_delete",
                "validate_adult_attempt_update", "prevent_adult_attempts_delete",
                "prevent_adult_attempt_events_update", "prevent_adult_attempt_events_delete",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            for index in (
                "idx_adult_capability_identity",
                "idx_adult_attempts_task_no", "idx_adult_attempts_approach",
                "idx_adult_attempts_one_running", "idx_adult_attempt_events_task_created",
            ):
                connection.execute(f"DROP INDEX {index}")
            connection.execute("DROP TABLE adult_capability_events")
            connection.execute("DROP TABLE adult_model_capabilities")
            connection.execute("DELETE FROM adult_schema_migrations WHERE version=3")
            connection.execute("DROP TABLE adult_attempt_events")
            connection.execute("DROP TABLE adult_attempts")
            connection.execute("DELETE FROM adult_schema_migrations WHERE version=2")
            connection.commit()

        upgraded = AdultTaskStore(self.db)
        self.assertEqual(created.task_id, upgraded.get_task(created.task_id).task_id)
        with upgraded._connect() as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("adult_tasks", "adult_steps", "adult_checkpoints", "adult_task_events")
            }
            migrations = [
                tuple(row) for row in connection.execute(
                    "SELECT version,name FROM adult_schema_migrations ORDER BY version"
                )
            ]
            self.assertEqual([(1, SCHEMA_NAME), (2, ATTEMPT_SCHEMA_NAME), (3, CAPABILITY_SCHEMA_NAME)], migrations)
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(before, after)

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

    def test_schema_rejects_tampered_capability_identity_index(self):
        AdultTaskStore(self.db)
        with sqlite3.connect(self.db) as connection:
            connection.execute("DROP INDEX idx_adult_capability_identity")
            connection.execute(
                "CREATE UNIQUE INDEX idx_adult_capability_identity "
                "ON adult_model_capabilities(capability_id)"
            )
        with self.assertRaises(TaskStoreSchemaError):
            AdultTaskStore(self.db)

    def test_schema_rejects_same_name_noop_history_trigger(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            connection.execute('DROP TRIGGER prevent_adult_task_events_update')
            connection.execute('CREATE TRIGGER prevent_adult_task_events_update BEFORE UPDATE ON adult_task_events BEGIN SELECT 1; END')
        with self.assertRaises(TaskStoreSchemaError):
            AdultTaskStore(self.db)

    def test_schema_rejects_same_name_nonunique_active_task_index(self):
        store = AdultTaskStore(self.db)
        with store._connect() as connection:
            connection.execute('DROP INDEX idx_adult_single_active_task')
            connection.execute('CREATE INDEX idx_adult_single_active_task ON adult_tasks(status)')
        with self.assertRaises(TaskStoreSchemaError):
            AdultTaskStore(self.db)

    def test_canonical_owned_definitions_validate_for_each_supported_schema_version(self):
        from agent.adult.adult_task_store import _ATTEMPT_SCHEMA, _CAPABILITY_SCHEMA
        statements = ()
        for version, additions in ((1, _SCHEMA), (2, _ATTEMPT_SCHEMA), (3, _CAPABILITY_SCHEMA)):
            statements += additions
            with self.subTest(version=version):
                connection = sqlite3.connect(':memory:')
                connection.row_factory = sqlite3.Row
                try:
                    for statement in statements:
                        connection.execute(statement)
                    AdultTaskStore._validate_schema(connection, schema_version=version)
                finally:
                    connection.close()

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

    def test_encrypted_retention_is_rejected_before_any_task_write_without_key(self):
        from agent.adult.adult_task_store import PayloadRetentionUnavailable
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        request = AdultTaskRequest(
            "text.generate", "hmac-sha256:0123456789abcdef",
            retention_policy="encrypted",
        )
        with patch.dict("os.environ", {"ADULT_PAYLOAD_ENCRYPTION_KEY": ""}, clear=False):
            with self.assertRaises(PayloadRetentionUnavailable):
                store.create_task(request, TaskBudget())
        with store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM adult_tasks").fetchone()[0])

    def test_encrypted_retention_requires_a_valid_key_and_accepts_one(self):
        from agent.adult.adult_task_store import PayloadRetentionUnavailable
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        request = AdultTaskRequest(
            "text.generate", "hmac-sha256:0123456789abcdef",
            retention_policy="encrypted",
        )
        with patch.dict("os.environ", {"ADULT_PAYLOAD_ENCRYPTION_KEY": "not-a-valid-key"}, clear=False):
            with self.assertRaises(PayloadRetentionUnavailable):
                store.create_task(request, TaskBudget())
        with store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM adult_tasks").fetchone()[0])

        key = base64.urlsafe_b64encode(b"K" * 32).decode("ascii")
        with patch.dict("os.environ", {"ADULT_PAYLOAD_ENCRYPTION_KEY": key}, clear=False):
            task = store.create_task(request, TaskBudget())
        self.assertEqual("queued", task.status)
        with store._connect() as connection:
            retention = connection.execute(
                "SELECT retention_policy FROM adult_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()[0]
        self.assertEqual("encrypted", retention)

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
        from agent.adult.adult_task_store import TaskTerminal
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
        with self.assertRaises(TaskTerminal):
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

    def test_claim_task_issues_fenced_lease_and_records_atomic_event(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        lease = store.claim_task(task.task_id, "worker-1", lease_seconds=45)
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1), (current.status, current.state_version))
        self.assertEqual(task.task_id, lease.task_id)
        self.assertEqual("worker-1", lease.worker_id)
        self.assertEqual(1, lease.lease_generation)
        self.assertEqual(1, lease.state_version_at_grant)
        events = store.list_events(task.task_id)
        self.assertEqual("task_claimed", events[-1].event_type)
        self.assertEqual({"lease_generation": 1, "state_version": 1}, events[-1].metadata)

    def test_claim_task_rejects_second_worker_for_same_task(self):
        from agent.adult.adult_task_store import LeaseNotAvailable
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        store.claim_task(task.task_id, "worker-1")
        with self.assertRaises(LeaseNotAvailable):
            store.claim_task(task.task_id, "worker-2")
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_claim_task_enforces_single_active_task_across_tasks(self):
        from agent.adult.adult_task_store import TaskAlreadyActive
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        first = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"), TaskBudget()
        )
        second = store.create_task(
            AdultTaskRequest("text.generate", "hmac-sha256:fedcba9876543210"), TaskBudget()
        )
        store.claim_task(first.task_id, "worker-1")
        with self.assertRaises(TaskAlreadyActive):
            store.claim_task(second.task_id, "worker-2")
        self.assertEqual("queued", store.get_task(second.task_id).status)
        self.assertEqual(1, len(store.list_events(second.task_id)))

    def test_renew_lease_extends_expiry_without_changing_state_version(self):
        from datetime import datetime, timedelta, timezone
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"), TaskBudget()
        )
        lease = store.claim_task(task.task_id, "worker-1", lease_seconds=5)
        before_renewal = store.get_task(task.task_id)
        renewed = store.renew_lease(lease, lease_seconds=90)
        current = store.get_task(task.task_id)
        self.assertEqual(before_renewal.updated_at, current.updated_at)
        self.assertEqual(1, current.state_version)
        self.assertEqual(lease.lease_generation, renewed.lease_generation)
        self.assertEqual(lease.state_version_at_grant, renewed.state_version_at_grant)
        self.assertGreater(
            datetime.fromisoformat(renewed.expires_at.replace("Z", "+00:00")),
            datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00")),
        )
        self.assertEqual("lease_renewed", store.list_events(task.task_id)[-1].event_type)

    def test_renew_lease_rejects_expired_lease_without_writes(self):
        from datetime import datetime, timedelta, timezone
        from agent.adult.adult_task_store import LeaseLost
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"), TaskBudget()
        )
        lease = store.claim_task(task.task_id, "worker-1")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with store._connect() as connection:
            connection.execute(
                "UPDATE adult_tasks SET lease_expires_at=? WHERE task_id=?",
                (expired, task.task_id),
            )
        with self.assertRaises(LeaseLost):
            store.renew_lease(lease)
        self.assertEqual(2, len(store.list_events(task.task_id)))
        self.assertEqual(1, store.get_task(task.task_id).state_version)

    def test_release_lease_clears_fenced_owner_and_records_atomic_event(self):
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        lease = store.claim_task(task.task_id, "worker-1")
        released = store.release_lease(
            lease, expected_state_version=lease.state_version_at_grant
        )
        self.assertEqual(("checkpointed", 2), (released.status, released.state_version))
        with store._connect() as connection:
            row = connection.execute(
                "SELECT lease_owner,lease_expires_at FROM adult_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
        self.assertEqual((None, None), tuple(row))
        event = store.list_events(task.task_id)[-1]
        self.assertEqual(("lease_released", "analyzing", "checkpointed"), (
            event.event_type, event.from_status, event.to_status,
        ))
        self.assertEqual({
            "lease_generation": lease.lease_generation,
            "reason_code": "worker_released",
        }, event.metadata)

    def test_release_lease_rejects_stale_version_and_expired_or_stale_generation(self):
        from agent.adult.adult_task_store import LeaseLost, StaleTaskVersion
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        lease = store.claim_task(task.task_id, "worker-1")
        with self.assertRaises(StaleTaskVersion):
            store.release_lease(lease, expected_state_version=0)
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1), (current.status, current.state_version))
        stale = type(lease)._from_store(
            lease.task_id, lease.worker_id, lease.lease_generation + 1,
            lease.expires_at, lease.state_version_at_grant,
        )
        with self.assertRaises(LeaseLost):
            store.release_lease(stale, expected_state_version=1)
        self.assertEqual(2, len(store.list_events(task.task_id)))

    def test_release_lease_rolls_back_state_when_event_append_fails(self):
        from agent.adult.adult_task_store import AdultTaskStoreError
        from agent.adult.models import AdultTaskRequest, TaskBudget
        store = AdultTaskStore(self.db)
        task = store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:0123456789abcdef"),
            TaskBudget(),
        )
        lease = store.claim_task(task.task_id, "worker-1")
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
                store.release_lease(lease, expected_state_version=1)
        current = store.get_task(task.task_id)
        self.assertEqual(("analyzing", 1), (current.status, current.state_version))
        self.assertEqual(2, len(store.list_events(task.task_id)))

if __name__ == "__main__":
    unittest.main()
