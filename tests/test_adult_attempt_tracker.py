"""Security and recovery tests for Adult Stage 5 AttemptTracker."""
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import (
    AdultAttemptTracker,
    AttemptAlreadyFinalized,
    AttemptApprovalRequired,
    AttemptBudgetExhausted,
    AttemptInProgress,
    AttemptNotAvailable,
    AttemptValidationError,
    DuplicateApproach,
    RetryAuthorizationRequired,
    StaleAttemptHandle,
)
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.models import AdultTaskRequest, RouteCandidate, TaskBudget


INPUT_FP = "hmac-sha256:0123456789abcdef0123456789abcdef"
CONFIG_FP = "hmac-sha256:abcdef0123456789abcdef0123456789"


def route(
    *, adapter_id="mock-adapter", provider_id="mock-provider",
    model_id="mock-model", cost_class="free_verified",
    capability_version="cap-v1", version="1",
):
    return RouteCandidate(
        adapter_id=adapter_id,
        adapter_version=version,
        provider_id=provider_id,
        model_id=model_id,
        model_version=version,
        tool_id=None,
        tool_version=None,
        task_type="text.analyze",
        cost_class=cost_class,
        capability_version=capability_version,
        verified_at=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


class AdultAttemptTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "adult.sqlite3"
        self.store = AdultTaskStore(self.db)
        self.task = self.store.create_task(
            AdultTaskRequest("text.analyze", INPUT_FP),
            TaskBudget(max_total_attempts=10, max_provider_calls=10),
        )
        self.lease = self.store.claim_task(self.task.task_id, "worker-1", lease_seconds=300)
        self.tracker = AdultAttemptTracker(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def begin(self, selected_route=None, *, input_fp=INPUT_FP, config_fp=CONFIG_FP, retry_reason=None):
        return self.tracker.begin(
            self.task.task_id, self.task.current_step_id, selected_route or route(),
            input_fp, config_fp, retry_reason=retry_reason,
        )

    def query(self, sql, parameters=()):
        db = self.store._connect()
        try:
            return db.execute(sql, parameters).fetchall()
        finally:
            db.close()

    def test_begin_persists_running_attempt_before_terminal_result(self):
        handle = self.begin()
        self.assertEqual(1, self.tracker.total_attempts(self.task.task_id))
        self.assertEqual(0, self.tracker.failures_since_approval(self.task.task_id))
        attempt = self.query("SELECT status,attempt_no,lease_generation FROM adult_attempts")
        self.assertEqual(("running", 1, self.lease.lease_generation), tuple(attempt[0]))
        events = self.query("SELECT event_type FROM adult_attempt_events")
        self.assertEqual(["attempt_started"], [row[0] for row in events])
        restarted = AdultAttemptTracker(AdultTaskStore(self.db))
        prior = restarted.find_prior_approach(self.task.task_id, handle.approach_key)
        self.assertEqual("running", prior.status)
        self.assertEqual(handle.attempt_id, prior.attempt_id)

    def test_success_is_terminal_once_and_records_only_bounded_usage(self):
        handle = self.begin()
        result = self.tracker.succeed(
            handle, "a" * 32,
            {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
            0,
        )
        self.assertEqual("succeeded", result.status)
        self.assertEqual({"input_tokens": 12, "output_tokens": 34, "total_tokens": 46}, result.usage)
        with self.assertRaises(AttemptAlreadyFinalized):
            self.tracker.succeed(handle, "b" * 32, None, 0)
        row = self.query("SELECT status,output_artifact_id,usage_json FROM adult_attempts")[0]
        self.assertEqual("succeeded", row["status"])
        self.assertEqual("a" * 32, row["output_artifact_id"])

    def test_transient_failure_increments_failure_counter_and_allows_distinct_route(self):
        handle = self.begin()
        result = self.tracker.fail(handle, "PROVIDER_ERROR", "Provider request failed.", True)
        self.assertEqual(("failed", "transient", True), (result.status, result.failure_class, result.retryable))
        self.assertEqual(1, self.tracker.failures_since_approval(self.task.task_id))
        second = self.begin(route(adapter_id="different-adapter"))
        self.assertEqual(2, second.attempt_no)

    def test_timeout_maps_to_timed_out_and_counts_as_failure(self):
        handle = self.begin()
        result = self.tracker.fail(handle, "PROVIDER_TIMEOUT", "Provider timed out.", True)
        self.assertEqual("timed_out", result.status)
        self.assertEqual(1, self.tracker.failures_since_approval(self.task.task_id))

    def test_policy_rejection_is_rejected_and_does_not_count_as_retryable_failure(self):
        handle = self.begin()
        result = self.tracker.fail(handle, "POLICY_REJECTED", "Provider policy rejected the request.", False)
        self.assertEqual(("rejected", "policy_rejection", False), (
            result.status, result.failure_class, result.retryable
        ))
        self.assertEqual(0, self.tracker.failures_since_approval(self.task.task_id))
        with self.assertRaises(AttemptValidationError):
            self.begin(route(adapter_id="another"))
        # A policy-rejected identical route must never be routed around by a repeat.
        with self.assertRaises(DuplicateApproach):
            self.begin()

    def test_non_retryable_failure_classes_cannot_be_marked_retryable(self):
        handle = self.begin()
        with self.assertRaises(AttemptValidationError):
            self.tracker.fail(handle, "INVALID_IMAGE", "Image input was invalid.", True)
        self.assertEqual("running", self.query("SELECT status FROM adult_attempts")[0][0])

    def test_duplicate_identical_approach_is_suppressed_and_audited_after_restart(self):
        handle = self.begin()
        self.tracker.fail(handle, "PROVIDER_ERROR", "Temporary provider failure.", True)
        restarted = AdultAttemptTracker(AdultTaskStore(self.db))
        with self.assertRaises(DuplicateApproach) as raised:
            restarted.begin(
                self.task.task_id, self.task.current_step_id, route(), INPUT_FP, CONFIG_FP
            )
        self.assertEqual(handle.attempt_id, raised.exception.prior.attempt_id)
        event_types = [row[0] for row in self.query(
            "SELECT event_type FROM adult_attempt_events ORDER BY created_at,event_id"
        )]
        self.assertIn("approach_skipped", event_types)
        self.assertEqual(1, self.tracker.total_attempts(self.task.task_id))

    def test_same_route_family_material_change_requires_retry_reason(self):
        first = self.begin()
        self.tracker.fail(first, "PROVIDER_ERROR", "Temporary provider failure.", True)
        changed_config = "hmac-sha256:fedcba9876543210fedcba9876543210"
        with self.assertRaises(RetryAuthorizationRequired):
            self.begin(config_fp=changed_config)
        second = self.begin(config_fp=changed_config, retry_reason="Configuration changed to fix timeout.")
        self.assertNotEqual(first.approach_key, second.approach_key)
        with self.assertRaises(DuplicateApproach):
            self.begin(config_fp=CONFIG_FP, retry_reason="Trying the same failed configuration again.")

    def test_retry_authorization_is_persisted_without_serializing_configuration(self):
        first = self.begin()
        self.tracker.fail(first, "PROVIDER_ERROR", "Temporary provider failure.", True)
        changed = "hmac-sha256:fedcba9876543210fedcba9876543210"
        self.begin(config_fp=changed, retry_reason="Updated local configuration fingerprint.")
        rows = self.query(
            "SELECT event_type,metadata_json FROM adult_attempt_events ORDER BY rowid"
        )
        self.assertTrue(any(row["event_type"] == "retry_authorized" for row in rows))
        self.assertNotIn(CONFIG_FP, "".join(row["metadata_json"] for row in rows))
        self.assertNotIn(changed, "".join(row["metadata_json"] for row in rows))

    def test_different_route_family_does_not_require_retry_reason(self):
        first = self.begin()
        self.tracker.fail(first, "PROVIDER_ERROR", "Temporary provider failure.", True)
        second = self.begin(route(adapter_id="new-adapter"))
        self.assertEqual(2, second.attempt_no)

    def test_paid_and_unknown_routes_fail_before_attempt_creation(self):
        before = self.tracker.total_attempts(self.task.task_id)
        for cost_class in ("paid", "unknown"):
            with self.subTest(cost_class=cost_class), self.assertRaises(AttemptNotAvailable):
                self.begin(route(adapter_id="blocked-route", cost_class=cost_class))
        self.assertEqual(before, self.tracker.total_attempts(self.task.task_id))
        self.assertEqual([], self.query("SELECT * FROM adult_attempts"))

    def test_local_route_does_not_consume_provider_call_budget(self):
        local_task = self.store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:local0123456789abcdef"),
            TaskBudget(max_total_attempts=2, max_provider_calls=0),
        )
        self.store.claim_task(local_task.task_id, "worker-local", lease_seconds=300)
        local_route = route(adapter_id="local-adapter", provider_id=None, cost_class="local")
        handle = self.tracker.begin(
            local_task.task_id, local_task.current_step_id, local_route, INPUT_FP, CONFIG_FP
        )
        task = self.query(
            "SELECT total_attempts,provider_calls FROM adult_tasks WHERE task_id=?",
            (local_task.task_id,),
        )[0]
        self.assertEqual((1, 0), tuple(task))
        self.tracker.succeed(handle, None, None, 0)

    def test_total_attempt_budget_fails_before_a_new_attempt_row(self):
        budget_task = self.store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:budget0123456789abc"),
            TaskBudget(max_total_attempts=1, max_provider_calls=5),
        )
        self.store.claim_task(budget_task.task_id, "worker-budget", lease_seconds=300)
        self.tracker.begin(
            budget_task.task_id, budget_task.current_step_id, route(), INPUT_FP, CONFIG_FP
        )
        with self.assertRaises(AttemptBudgetExhausted):
            self.tracker.begin(
                budget_task.task_id, budget_task.current_step_id,
                route(adapter_id="different"), INPUT_FP, CONFIG_FP,
            )
        count = self.query(
            "SELECT COUNT(*) FROM adult_attempts WHERE task_id=?", (budget_task.task_id,)
        )[0][0]
        self.assertEqual(1, count)

    def test_provider_call_budget_fails_before_a_new_provider_attempt(self):
        budget_task = self.store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:providerbudget012345"),
            TaskBudget(max_total_attempts=5, max_provider_calls=1),
        )
        self.store.claim_task(budget_task.task_id, "worker-provider", lease_seconds=300)
        first = self.tracker.begin(
            budget_task.task_id, budget_task.current_step_id, route(), INPUT_FP, CONFIG_FP
        )
        self.tracker.fail(first, "PROVIDER_ERROR", "Temporary provider failure.", True)
        with self.assertRaises(AttemptBudgetExhausted):
            self.tracker.begin(
                budget_task.task_id, budget_task.current_step_id,
                route(adapter_id="different"), INPUT_FP, CONFIG_FP,
            )

    def test_five_failures_block_the_sixth_attempt_until_approval(self):
        task = self.store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:fivefailures012345678"),
            TaskBudget(max_total_attempts=20, max_provider_calls=20),
        )
        self.store.claim_task(task.task_id, "worker-five", lease_seconds=300)
        for index in range(5):
            selected = route(adapter_id=f"adapter-{index}")
            handle = self.tracker.begin(
                task.task_id, task.current_step_id, selected,
                INPUT_FP, CONFIG_FP,
            )
            self.tracker.fail(handle, "PROVIDER_ERROR", "Transient backend failure.", True)
        self.assertEqual(5, self.tracker.failures_since_approval(task.task_id))
        with self.assertRaises(AttemptApprovalRequired):
            self.tracker.begin(
                task.task_id, task.current_step_id, route(adapter_id="sixth-adapter"),
                INPUT_FP, CONFIG_FP,
            )
        self.assertEqual(5, self.tracker.total_attempts(task.task_id))

    def test_crash_after_begin_is_recovered_as_unknown_only_after_lease_recovery(self):
        handle = self.begin()
        self.assertEqual(0, self.tracker.recover_uncertain_attempts())
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            self.store.recover_expired_leases()
        self.assertEqual(1, self.tracker.recover_uncertain_attempts())
        row = self.query(
            "SELECT status,failure_class,error_code FROM adult_attempts WHERE attempt_id=?",
            (handle.attempt_id,),
        )[0]
        self.assertEqual(("unknown_outcome", "unknown", "PROVIDER_OUTCOME_UNKNOWN"), tuple(row))
        self.assertEqual(1, self.tracker.failures_since_approval(self.task.task_id))

    def test_stale_worker_cannot_finalize_after_lease_generation_recovery(self):
        handle = self.begin()
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with patch("agent.adult.adult_task_store._utc_now", return_value=future):
            self.store.recover_expired_leases()
        self.tracker.recover_uncertain_attempts()
        with self.assertRaises(AttemptAlreadyFinalized):
            self.tracker.succeed(handle, "a" * 32, None, 0)

    def test_concurrent_begin_calls_cannot_create_two_running_attempts(self):
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait()
                handle = self.tracker.begin(
                    self.task.task_id, self.task.current_step_id, route(),
                    INPUT_FP, CONFIG_FP,
                )
                value = ("ok", handle.attempt_id)
            except Exception as exc:  # assertions are made on the collected result below
                value = (type(exc).__name__, None)
            with lock:
                outcomes.append(value)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, sum(value[0] == "ok" for value in outcomes))
        self.assertEqual(1, sum(value[0] in {"DuplicateApproach", "AttemptInProgress"} for value in outcomes))
        self.assertEqual(1, self.tracker.total_attempts(self.task.task_id))
        self.assertEqual(1, self.query(
            "SELECT COUNT(*) FROM adult_attempts WHERE task_id=? AND status='running'",
            (self.task.task_id,),
        )[0][0])

    def test_attempt_and_audit_rows_cannot_be_updated_or_deleted(self):
        handle = self.begin()
        self.tracker.succeed(handle, "a" * 32, {"total_tokens": 1}, 0)
        with self.store._connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "UPDATE adult_attempts SET approach_key=? WHERE attempt_id=?",
                    ("f" * 64, handle.attempt_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM adult_attempts WHERE attempt_id=?", (handle.attempt_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE adult_attempt_events SET event_type='approach_skipped'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM adult_attempt_events")

    def test_step_task_mismatch_and_unsafe_fields_are_rejected(self):
        other = self.store.create_task(
            AdultTaskRequest("text.analyze", "hmac-sha256:other0123456789abcdef"),
            TaskBudget(max_provider_calls=2),
        )
        with self.assertRaises(AttemptValidationError):
            self.tracker.begin(
                self.task.task_id, other.current_step_id, route(), INPUT_FP, CONFIG_FP
            )
        for bad_usage in (
            {"prompt": "raw user input"},
            {"input_tokens": True},
            {"unexpected": 4},
            {"input_tokens": -1},
        ):
            handle = self.begin(route(adapter_id="usage-" + str(len(str(bad_usage)))))
            with self.subTest(usage=bad_usage), self.assertRaises(AttemptValidationError):
                self.tracker.succeed(handle, None, bad_usage, 0)
            self.tracker.fail(handle, "PROVIDER_ERROR", "Provider returned invalid usage.", False)

    def test_invalid_retry_reason_error_summary_and_cost_are_rejected(self):
        first = self.begin()
        self.tracker.fail(first, "PROVIDER_ERROR", "Temporary provider failure.", True)
        changed_config = "hmac-sha256:changed01234567890123456789"
        for reason in ("", "See https://example.com", "API_KEY=secret-value"):
            with self.subTest(reason=reason), self.assertRaises(AttemptValidationError):
                self.begin(config_fp=changed_config, retry_reason=reason)
        second = self.begin(
            route(adapter_id="safe-other"), retry_reason=None
        )
        with self.assertRaises(AttemptValidationError):
            self.tracker.succeed(second, None, None, True)
        with self.assertRaises(AttemptValidationError):
            self.tracker.fail(second, "PROVIDER_ERROR", "/home/user/private", True)
        self.tracker.fail(second, "PROVIDER_ERROR", "Retryable service error.", True)

    def test_tracker_issued_handle_cannot_be_constructed_by_callers(self):
        with self.assertRaises(TypeError):
            from agent.adult.adult_attempt_tracker import AttemptHandle
            AttemptHandle("id", "task", 1, "f" * 64, 1, "2026-01-01T00:00:00.000000Z")


if __name__ == "__main__":
    unittest.main()
