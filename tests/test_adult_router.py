"""Tests for exact, deterministic, cost-aware Adult route selection."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.models import CapabilityEvidence, CapabilityQuery, CostPolicy, ModelCapabilitySpec

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-21T12:00:00.000000Z"
FUTURE = "2026-12-21T12:00:00.000000Z"
COST_FUTURE = "2026-11-28T12:00:00.000000Z"
INPUT_FP = "hmac-sha256:0123456789abcdef0123456789abcdef"
CONFIG_FP = "hmac-sha256:abcdef0123456789abcdef0123456789"


def free_text_spec(adapter_id, provider_id, model_id, model_version="1"):
    return ModelCapabilitySpec(
        adapter_id=adapter_id, adapter_version="1", provider_id=provider_id,
        model_id=model_id, model_version=model_version, task_type="text.analyze",
        input_modalities=("text",), output_modalities=("text",), cost_class="free_verified",
    )


def evidence(spec):
    return CapabilityEvidence(
        adapter_id=spec.adapter_id, adapter_version=spec.adapter_version,
        provider_id=spec.provider_id, model_id=spec.model_id,
        model_version=spec.model_version, task_type=spec.task_type,
        input_modalities=spec.input_modalities, output_modalities=spec.output_modalities,
        capability_source="official_provider_docs",
        capability_reference="https://provider.example/docs/model",
        verified_at=STAMP, expires_at=FUTURE,
        cost_source="official_free_tier_terms",
        cost_reference="https://provider.example/free-tier",
        cost_verified_at=STAMP, cost_expires_at=COST_FUTURE, cost_is_zero=True,
    )

def image_analysis_spec():
    return ModelCapabilitySpec(
        adapter_id="image-understanding", adapter_version="1",
        provider_id="image-provider", model_id="image-model", model_version="1",
        task_type="image.analyze", input_modalities=("image",),
        output_modalities=("text",), cost_class="free_verified",
        accepted_mime_types=("image/png", "image/jpeg"),
        max_input_bytes=4096, max_input_width=800, max_input_height=600,
    )


def local_text_spec():
    return ModelCapabilitySpec(
        adapter_id="local-text", adapter_version="1",
        task_type="text.analyze", input_modalities=("text",),
        output_modalities=("text",), cost_class="local",
    )


def local_evidence(spec):
    return CapabilityEvidence(
        adapter_id=spec.adapter_id, adapter_version=spec.adapter_version,
        provider_id=spec.provider_id, model_id=spec.model_id,
        model_version=spec.model_version, task_type=spec.task_type,
        input_modalities=spec.input_modalities, output_modalities=spec.output_modalities,
        capability_source="local_adapter_smoke_test",
        capability_reference="test:local-adapter-smoke-v1",
        verified_at=STAMP, expires_at=FUTURE,
    )



class AdultRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AdultTaskStore(Path(self.temp.name) / "adult.sqlite3")
        self.registry = ModelCapabilityRegistry(self.store, clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, spec):
        record = self.registry.register(spec)
        verified = self.registry.verify(record.capability_id, evidence(spec), record.state_version)
        return self.registry.enable(verified.capability_id, verified.state_version)

    def _query(self):
        return CapabilityQuery(
            task_type="text.analyze", input_modalities=("text",), output_modality="text",
            input_fingerprint=INPUT_FP, config_fingerprint=CONFIG_FP,
        )

    def _router(self):
        return AdultModelRouter(
            self.registry,
            approach_key_builder=AdultAttemptTracker.approach_keys,
            local_adapter_available=lambda _adapter_id: True,
        )

    def test_free_routes_require_explicit_positive_provider_call_budget(self):
        self._register(free_text_spec("adapter-b", "provider-b", "model-b"))
        self.assertEqual([], self._router().choose_routes(
            self._query(), CostPolicy(max_provider_calls=0), set(),
        ))
        routes = self._router().choose_routes(
            self._query(), CostPolicy(max_provider_calls=2), set(),
        )
        self.assertEqual(1, len(routes))
        self.assertEqual("free_verified", routes[0].cost_class)

    def test_route_order_is_deterministic_and_excluded_approach_is_removed(self):
        self._register(free_text_spec("adapter-z", "provider-z", "model-z"))
        self._register(free_text_spec("adapter-a", "provider-a", "model-a"))
        router = self._router()
        all_routes = router.choose_routes(self._query(), CostPolicy(max_provider_calls=5), set())
        self.assertEqual(["adapter-a", "adapter-z"], [route.adapter_id for route in all_routes])
        first_key, _ = AdultAttemptTracker.approach_keys(
            "text.analyze", all_routes[0], INPUT_FP, CONFIG_FP,
        )
        filtered = router.choose_routes(
            self._query(), CostPolicy(max_provider_calls=5), {first_key},
        )
        self.assertEqual(["adapter-z"], [route.adapter_id for route in filtered])

    def test_capability_version_is_part_of_stage5_approach_key(self):
        record = self.registry.register(free_text_spec("adapter-a", "provider-a", "model-a"))
        verified = self.registry.verify(record.capability_id, evidence(record.spec), record.state_version)
        enabled = self.registry.enable(verified.capability_id, verified.state_version)
        route = self._router().choose_routes(self._query(), CostPolicy(max_provider_calls=2), set())[0]
        before, _ = AdultAttemptTracker.approach_keys("text.analyze", route, INPUT_FP, CONFIG_FP)
        revised = self.registry.revise(
            enabled.capability_id,
            free_text_spec("adapter-a", "provider-a", "model-a", model_version="2"),
            enabled.state_version,
        )
        verified_again = self.registry.verify(revised.capability_id, evidence(revised.spec), revised.state_version)
        self.registry.enable(verified_again.capability_id, verified_again.state_version)
        new_route = self._router().choose_routes(self._query(), CostPolicy(max_provider_calls=2), set())[0]
        after, _ = AdultAttemptTracker.approach_keys("text.analyze", new_route, INPUT_FP, CONFIG_FP)
        self.assertNotEqual(before, after)

    def test_image_routes_enforce_exact_mime_byte_and_dimension_constraints(self):
        spec = image_analysis_spec()
        self._register(spec)
        router = self._router()
        valid = CapabilityQuery(
            task_type="image.analyze", input_modalities=("image",),
            output_modality="text", input_fingerprint=INPUT_FP,
            config_fingerprint=CONFIG_FP, mime_type="image/png",
            input_bytes=4096, width=800, height=600,
        )
        self.assertEqual(1, len(router.choose_routes(
            valid, CostPolicy(max_provider_calls=1), set(),
        )))
        for invalid in (
            CapabilityQuery(
                task_type="image.analyze", input_modalities=("image",),
                output_modality="text", input_fingerprint=INPUT_FP,
                config_fingerprint=CONFIG_FP, mime_type="image/webp",
                input_bytes=1024, width=640, height=480,
            ),
            CapabilityQuery(
                task_type="image.analyze", input_modalities=("image",),
                output_modality="text", input_fingerprint=INPUT_FP,
                config_fingerprint=CONFIG_FP, mime_type="image/jpeg",
                input_bytes=4097, width=640, height=480,
            ),
            CapabilityQuery(
                task_type="image.analyze", input_modalities=("image",),
                output_modality="text", input_fingerprint=INPUT_FP,
                config_fingerprint=CONFIG_FP, mime_type="image/jpeg",
                input_bytes=1024, width=801, height=480,
            ),
            CapabilityQuery(
                task_type="image.analyze", input_modalities=("image",),
                output_modality="text", input_fingerprint=INPUT_FP,
                config_fingerprint=CONFIG_FP, mime_type="image/jpeg",
                input_bytes=1024, width=640, height=601,
            ),
        ):
            self.assertEqual([], router.choose_routes(
                invalid, CostPolicy(max_provider_calls=1), set(),
            ))

    def test_local_routes_require_positive_availability_check(self):
        spec = local_text_spec()
        record = self.registry.register(spec)
        verified = self.registry.verify(record.capability_id, local_evidence(spec), record.state_version)
        self.registry.enable(verified.capability_id, verified.state_version)
        unavailable = AdultModelRouter(
            self.registry,
            approach_key_builder=AdultAttemptTracker.approach_keys,
            local_adapter_available=lambda _adapter_id: False,
        )
        self.assertEqual([], unavailable.choose_routes(
            self._query(), CostPolicy(max_provider_calls=0), set(),
        ))
        available = AdultModelRouter(
            self.registry,
            approach_key_builder=AdultAttemptTracker.approach_keys,
            local_adapter_available=lambda _adapter_id: True,
        )
        self.assertEqual(1, len(available.choose_routes(
            self._query(), CostPolicy(max_provider_calls=0), set(),
        )))

    def test_expired_zero_cost_evidence_is_ineligible_and_query_is_read_only(self):
        current = [NOW]
        registry = ModelCapabilityRegistry(self.store, clock=lambda: current[0])
        spec = free_text_spec("expiry-adapter", "expiry-provider", "expiry-model")
        record = registry.register(spec)
        verified = registry.verify(record.capability_id, evidence(spec), record.state_version)
        registry.enable(verified.capability_id, verified.state_version)
        before = self.store._connect()
        try:
            before_counts = (
                before.execute("SELECT COUNT(*) FROM adult_model_capabilities").fetchone()[0],
                before.execute("SELECT COUNT(*) FROM adult_capability_events").fetchone()[0],
            )
        finally:
            before.close()
        self.assertEqual(1, len(AdultModelRouter(
            registry, approach_key_builder=AdultAttemptTracker.approach_keys,
        ).choose_routes(self._query(), CostPolicy(max_provider_calls=1), set())))
        current[0] = datetime(2026, 11, 29, 12, 0, tzinfo=timezone.utc)
        self.assertEqual([], registry.list_eligible(
            self._query(), CostPolicy(max_provider_calls=1),
        ))
        after = self.store._connect()
        try:
            after_counts = (
                after.execute("SELECT COUNT(*) FROM adult_model_capabilities").fetchone()[0],
                after.execute("SELECT COUNT(*) FROM adult_capability_events").fetchone()[0],
            )
        finally:
            after.close()
        self.assertEqual(before_counts, after_counts)

if __name__ == "__main__":
    unittest.main()
