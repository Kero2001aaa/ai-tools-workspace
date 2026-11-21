"""Tests for Adult capability persistence, evidence and v2-to-v3 migration."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.capability_registry import (
    CapabilityConflict, CapabilityEvidenceError, CapabilityNotEligible,
    CapabilityStateConflict, ModelCapabilityRegistry,
)
from agent.adult.models import AdultTaskRequest, CapabilityEvidence, ModelCapabilitySpec, TaskBudget, RouteCandidate

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-21T12:00:00.000000Z"
FUTURE = "2026-12-21T12:00:00.000000Z"
COST_FUTURE = "2026-11-28T12:00:00.000000Z"
INPUT_FP = "hmac-sha256:0123456789abcdef0123456789abcdef"
CONFIG_FP = "hmac-sha256:abcdef0123456789abcdef0123456789"


def text_spec(*, adapter_id="mock-adapter", provider_id="mock-provider",
              model_id="mock-model", cost_class="free_verified"):
    return ModelCapabilitySpec(
        adapter_id=adapter_id, adapter_version="1",
        provider_id=provider_id, model_id=model_id, model_version="1",
        task_type="text.analyze", input_modalities=("text",),
        output_modalities=("text",), cost_class=cost_class,
    )


def provider_evidence(spec=None, *, cost=True, cost_is_zero=True,
                      verified_at=STAMP, expires_at=FUTURE):
    selected = spec or text_spec()
    return CapabilityEvidence(
        adapter_id=selected.adapter_id, adapter_version=selected.adapter_version,
        provider_id=selected.provider_id, model_id=selected.model_id,
        model_version=selected.model_version, task_type=selected.task_type,
        input_modalities=selected.input_modalities,
        output_modalities=selected.output_modalities,
        capability_source="official_provider_docs",
        capability_reference="https://provider.example/docs/model",
        verified_at=verified_at, expires_at=expires_at,
        cost_source="official_provider_pricing" if cost else None,
        cost_reference="https://provider.example/pricing" if cost else None,
        cost_verified_at=verified_at if cost else None,
        cost_expires_at=COST_FUTURE if cost else None,
        cost_is_zero=cost_is_zero if cost else None,
    )


def local_spec():
    return ModelCapabilitySpec(
        adapter_id="local-adapter", adapter_version="1",
        task_type="text.analyze", input_modalities=("text",),
        output_modalities=("text",), cost_class="local",
    )


def local_evidence(spec=None):
    selected = spec or local_spec()
    return CapabilityEvidence(
        adapter_id=selected.adapter_id, adapter_version=selected.adapter_version,
        provider_id=selected.provider_id, model_id=selected.model_id,
        model_version=selected.model_version, task_type=selected.task_type,
        input_modalities=selected.input_modalities,
        output_modalities=selected.output_modalities,
        capability_source="local_adapter_smoke_test",
        capability_reference="test:local-adapter-smoke-v1",
        verified_at=STAMP, expires_at=FUTURE,
    )


class AdultCapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "adult.sqlite3"
        self.store = AdultTaskStore(self.db)
        self.registry = ModelCapabilityRegistry(self.store, clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def test_register_starts_disabled_and_unverified_and_duplicate_is_rejected(self):
        record = self.registry.register(text_spec())
        self.assertFalse(record.enabled)
        self.assertEqual("unverified", record.verification_status)
        self.assertEqual(1, record.state_version)
        with self.assertRaises(CapabilityConflict):
            self.registry.register(text_spec())
        db = self.store._connect()
        try:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM adult_model_capabilities").fetchone()[0])
            self.assertEqual("registered", db.execute("SELECT event_type FROM adult_capability_events").fetchone()[0])
        finally:
            db.close()

    def test_verification_does_not_auto_enable_and_enable_requires_current_version(self):
        record = self.registry.register(text_spec())
        verified = self.registry.verify(record.capability_id, provider_evidence(), record.state_version)
        self.assertEqual("verified", verified.verification_status)
        self.assertFalse(verified.enabled)
        enabled = self.registry.enable(verified.capability_id, verified.state_version)
        self.assertTrue(enabled.enabled)
        with self.assertRaises(CapabilityStateConflict):
            self.registry.disable(enabled.capability_id, "operator_hold", verified.state_version)

    def test_free_verified_requires_separate_zero_cost_evidence(self):
        record = self.registry.register(text_spec())
        with self.assertRaises(CapabilityEvidenceError):
            self.registry.verify(record.capability_id, provider_evidence(cost=False), record.state_version)
        self.assertEqual("unverified", self.registry.get(record.capability_id).verification_status)

    def test_evidence_cannot_be_reused_for_another_model_or_adapter(self):
        record = self.registry.register(text_spec())
        mismatched = provider_evidence(text_spec(model_id="different-model"))
        with self.assertRaises(CapabilityEvidenceError):
            self.registry.verify(record.capability_id, mismatched, record.state_version)

    def test_free_verified_requires_explicit_zero_cost_claim(self):
        record = self.registry.register(text_spec())
        evidence = provider_evidence(record.spec, cost_is_zero=False)
        with self.assertRaises(CapabilityEvidenceError):
            self.registry.verify(record.capability_id, evidence, record.state_version)

    def test_expired_capability_or_cost_evidence_is_rejected(self):
        record = self.registry.register(text_spec())
        expired = provider_evidence(
            verified_at="2026-11-20T12:00:00.000000Z",
            expires_at="2026-11-21T11:59:59.000000Z",
        )
        with self.assertRaises(CapabilityEvidenceError):
            self.registry.verify(record.capability_id, expired, record.state_version)

    def test_paid_and_unknown_routes_can_never_be_enabled(self):
        for cost_class in ("paid", "unknown"):
            record = self.registry.register(text_spec(
                adapter_id=f"{cost_class}-adapter", provider_id=f"{cost_class}-provider",
                model_id=f"{cost_class}-model", cost_class=cost_class,
            ))
            verified = self.registry.verify(
                record.capability_id, provider_evidence(record.spec, cost=False), record.state_version,
            )
            with self.assertRaises(CapabilityNotEligible):
                self.registry.enable(verified.capability_id, verified.state_version)

    def test_revise_changes_version_clears_evidence_and_disables(self):
        record = self.registry.register(text_spec())
        verified = self.registry.verify(record.capability_id, provider_evidence(), record.state_version)
        enabled = self.registry.enable(verified.capability_id, verified.state_version)
        revised = self.registry.revise(
            enabled.capability_id,
            text_spec(adapter_id="mock-adapter", model_id="mock-model-v2"),
            enabled.state_version,
        )
        self.assertFalse(revised.enabled)
        self.assertEqual("unverified", revised.verification_status)
        self.assertIsNone(revised.evidence)
        self.assertNotEqual(enabled.capability_version, revised.capability_version)

    def test_revocation_is_terminal_and_audited(self):
        record = self.registry.register(text_spec())
        verified = self.registry.verify(record.capability_id, provider_evidence(), record.state_version)
        revoked = self.registry.revoke(verified.capability_id, "provider_terms_changed", verified.state_version)
        self.assertEqual("revoked", revoked.verification_status)
        with self.assertRaises(CapabilityNotEligible):
            self.registry.enable(revoked.capability_id, revoked.state_version)
        db = self.store._connect()
        try:
            self.assertEqual(
                ["registered", "verified", "revoked"],
                [row[0] for row in db.execute(
                    "SELECT event_type FROM adult_capability_events ORDER BY rowid"
                ).fetchall()],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM adult_model_capabilities WHERE capability_id=?", (record.capability_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE adult_capability_events SET event_type='disabled'")
        finally:
            db.close()

    def test_local_capability_requires_local_smoke_evidence(self):
        record = self.registry.register(local_spec())
        with self.assertRaises(CapabilityEvidenceError):
            self.registry.verify(record.capability_id, provider_evidence(cost=False), record.state_version)
        verified = self.registry.verify(record.capability_id, local_evidence(record.spec), record.state_version)
        enabled = self.registry.enable(verified.capability_id, verified.state_version)
        self.assertTrue(enabled.enabled)

    def test_v2_to_v3_migration_preserves_attempts_and_events(self):
        task = self.store.create_task(
            AdultTaskRequest("text.analyze", INPUT_FP),
            TaskBudget(max_total_attempts=10, max_provider_calls=10),
        )
        self.store.claim_task(task.task_id, "migration-worker", lease_seconds=300)
        tracker = AdultAttemptTracker(self.store)
        route = RouteCandidate(
            adapter_id="mock-adapter", adapter_version="1",
            provider_id="mock-provider", model_id="mock-model", model_version="1",
            tool_id=None, tool_version=None, task_type="text.analyze",
            cost_class="free_verified", capability_version="cap-v1", verified_at=STAMP,
        )
        tracker.begin(task.task_id, task.current_step_id, route, INPUT_FP, CONFIG_FP)
        db = self.store._connect()
        try:
            before_attempts = [tuple(row) for row in db.execute(
                "SELECT attempt_id,task_id,step_id,attempt_no,approach_key,status FROM adult_attempts"
            ).fetchall()]
            before_events = [tuple(row) for row in db.execute(
                "SELECT event_id,task_id,attempt_id,event_type,approach_key FROM adult_attempt_events"
            ).fetchall()]
            db.execute("BEGIN IMMEDIATE")
            for trigger in (
                "validate_adult_capability_update", "prevent_adult_capabilities_delete",
                "prevent_adult_capability_events_update", "prevent_adult_capability_events_delete",
            ):
                db.execute(f"DROP TRIGGER {trigger}")
            db.execute("DROP INDEX idx_adult_capability_identity")
            db.execute("DROP TABLE adult_capability_events")
            db.execute("DROP TABLE adult_model_capabilities")
            db.execute("DELETE FROM adult_schema_migrations WHERE version=3")
            db.commit()
        finally:
            db.close()

        upgraded = AdultTaskStore(self.db)
        db = upgraded._connect()
        try:
            after_attempts = [tuple(row) for row in db.execute(
                "SELECT attempt_id,task_id,step_id,attempt_no,approach_key,status FROM adult_attempts"
            ).fetchall()]
            after_events = [tuple(row) for row in db.execute(
                "SELECT event_id,task_id,attempt_id,event_type,approach_key FROM adult_attempt_events"
            ).fetchall()]
            versions = [tuple(row) for row in db.execute(
                "SELECT version,name FROM adult_schema_migrations ORDER BY version"
            ).fetchall()]
        finally:
            db.close()
        self.assertEqual(before_attempts, after_attempts)
        self.assertEqual(before_events, after_events)
        self.assertEqual([
            (1, "adult_operational_store_v1"),
            (2, "adult_attempt_tracking_v2"),
            (3, "adult_capability_registry_v3"),
        ], versions)

    def test_v2_to_v3_migration_failure_rolls_back(self):
        db = self.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            for trigger in (
                "validate_adult_capability_update", "prevent_adult_capabilities_delete",
                "prevent_adult_capability_events_update", "prevent_adult_capability_events_delete",
            ):
                db.execute(f"DROP TRIGGER {trigger}")
            db.execute("DROP INDEX idx_adult_capability_identity")
            db.execute("DROP TABLE adult_capability_events")
            db.execute("DROP TABLE adult_model_capabilities")
            db.execute("DELETE FROM adult_schema_migrations WHERE version=3")
            db.commit()
        finally:
            db.close()
        with patch(
            "agent.adult.adult_task_store._CAPABILITY_SCHEMA",
            ("CREATE TABLE intentionally_broken (",),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                AdultTaskStore(self.db)
        db = sqlite3.connect(self.db)
        try:
            versions = db.execute(
                "SELECT version FROM adult_schema_migrations ORDER BY version"
            ).fetchall()
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        finally:
            db.close()
        self.assertEqual([(1,), (2,)], versions)
        self.assertNotIn("adult_model_capabilities", tables)


if __name__ == "__main__":
    unittest.main()
