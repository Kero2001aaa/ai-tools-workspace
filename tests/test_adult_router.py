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

if __name__ == "__main__":
    unittest.main()
