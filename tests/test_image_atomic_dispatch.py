"""Image dispatch boundaries over real stores and deterministic offline adapters."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import test_adult_image_executor as fixtures
from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.models import CapabilityQuery, CostPolicy, ModelCapabilitySpec, TaskLease
import test_adult_capability_registry as registry_fixtures


class ImageAtomicDispatchTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    compose = fixtures.ExecutorTests.compose
    enable = fixtures.ExecutorTests.enable

    def guarded(self):
        registry = ModelCapabilityRegistry(self.tasks)
        self.executor.pipeline._attempt_tracker = AdultAttemptTracker(
            self.tasks, route_validator=registry.current_image_route_in_transaction)
        return registry

    def route(self, registry, task_type):
        inputs, outputs = fixtures.MODALITIES[task_type]
        fields = dict(task_type=task_type, input_modalities=inputs,
                      output_modality=outputs[0], input_fingerprint=self.cipher.fingerprint(b'x'),
                      config_fingerprint=fixtures.CONFIG)
        if 'image' in inputs:
            fields.update(mime_type='image/png', input_bytes=len(self.image), width=2, height=2)
        return registry.list_eligible(CapabilityQuery(**fields), CostPolicy(1, 0, True))[0]

    def test_all_image_shapes_require_exact_current_candidate_in_transaction(self):
        self.enable(*fixtures.MODALITIES)
        registry = self.guarded()
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        for task_type in fixtures.MODALITIES:
            route = self.route(registry, task_type)
            with self.subTest(task_type=task_type), self.tasks._transaction() as db:
                self.assertTrue(registry.current_image_route_in_transaction(db, route, now))
                self.assertFalse(registry.current_image_route_in_transaction(db, route, future))
                for changed in (replace(route, capability_version='a'*64),
                                replace(route, max_output_bytes=7),
                                replace(route, max_output_width=7),
                                replace(route, max_output_height=7),
                                replace(route, output_mime_types=('image/webp',)),
                                replace(route, tool_id='unexpected-tool'),
                                replace(route, verified_at=future),
                                replace(route, adapter_version='other')):
                    self.assertFalse(registry.current_image_route_in_transaction(db, changed, now))
            with self.tasks._connect() as db:
                self.assertFalse(registry.current_image_route_in_transaction(db, route, now))

    def test_disable_after_selection_prevents_attempt_and_adapter_call(self):
        self.enable('image.generate')
        registry = self.guarded()
        original = self.executor.pipeline._attempt_tracker.begin
        def disable_then_begin(*args, **kwargs):
            with self.tasks._connect() as db:
                row = db.execute("SELECT capability_id,state_version FROM adult_model_capabilities").fetchone()
            registry.disable(row['capability_id'], 'operator_disabled', row['state_version'])
            return original(*args, **kwargs)
        task = self.executor.create('image.generate', prompt='a red square')
        with patch.object(self.executor.pipeline._attempt_tracker, 'begin', side_effect=disable_then_begin):
            result = self.executor.run(task.task_id, fixtures.CONFIG, prompt='a red square')
        self.assertEqual('ATTEMPT_UNAVAILABLE', result.error_code)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)

    def test_pipeline_passes_caller_lease_into_atomic_attempt(self):
        self.enable('image.generate')
        self.guarded()
        original = self.executor.pipeline._attempt_tracker.begin
        seen = []
        def capture(*args, **kwargs):
            seen.append(kwargs.get('lease'))
            return original(*args, **kwargs)
        task = self.executor.create('image.generate', prompt='a red square')
        with patch.object(self.executor.pipeline._attempt_tracker, 'begin', side_effect=capture):
            self.executor.run(task.task_id, fixtures.CONFIG, prompt='a red square')
        self.assertEqual(1, len(seen))
        self.assertIsNotNone(seen[0])
        self.assertEqual(task.task_id, seen[0].task_id)

    def test_wrong_worker_fence_cannot_dispatch_even_with_live_task_lease(self):
        self.enable('image.generate')
        self.guarded()
        original = self.executor.pipeline.run_one_attempt
        def stale(task, step_id, request, config, excluded, *, lease):
            wrong = TaskLease._from_store(lease.task_id, 'stale-worker',
                lease.lease_generation, lease.expires_at, lease.state_version_at_grant)
            return original(task, step_id, request, config, excluded, lease=wrong)
        task = self.executor.create('image.generate', prompt='a red square')
        with patch.object(self.executor.pipeline, 'run_one_attempt', side_effect=stale):
            result = self.executor.run(task.task_id, fixtures.CONFIG, prompt='a red square')
        self.assertEqual('ATTEMPT_UNAVAILABLE', result.error_code)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)

    def test_free_image_cost_evidence_expiry_is_checked_inside_transaction(self):
        clock = registry_fixtures.NOW
        registry = ModelCapabilityRegistry(self.tasks, clock=lambda: clock)
        spec = ModelCapabilitySpec(adapter_id='free-image', adapter_version='1',
            provider_id='provider', model_id='model', model_version='1',
            task_type='image.generate', input_modalities=('text',), output_modalities=('image',),
            cost_class='free_verified', output_mime_types=('image/png',),
            max_output_bytes=1024, max_output_width=32, max_output_height=32)
        record = registry.register(spec)
        record = registry.verify(record.capability_id,
                                 registry_fixtures.provider_evidence(spec), record.state_version)
        registry.enable(record.capability_id, record.state_version)
        query = CapabilityQuery('image.generate', ('text',), 'image',
                                self.cipher.fingerprint(b'x'), fixtures.CONFIG)
        route = registry.list_eligible(query, CostPolicy(1, 0, True))[0]
        with self.tasks._transaction() as db:
            self.assertTrue(registry.current_image_route_in_transaction(db, route,
                             registry_fixtures.STAMP))
            self.assertFalse(registry.current_image_route_in_transaction(db, route,
                              '2026-11-29T12:00:00.000000Z'))
            self.assertFalse(registry.current_image_route_in_transaction(db,
                             replace(route, cost_class='local'), registry_fixtures.STAMP))


    def test_revoked_image_candidate_is_rejected_before_commit(self):
        self.enable('image.generate')
        registry = self.guarded()
        route = self.route(registry, 'image.generate')
        with self.tasks._connect() as db:
            row = db.execute("SELECT capability_id,state_version FROM adult_model_capabilities").fetchone()
        registry.revoke(row['capability_id'], 'provider_terms_changed', row['state_version'])
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        with self.tasks._transaction() as db:
            self.assertFalse(registry.current_image_route_in_transaction(db, route, now))

    def test_revised_and_reverified_image_candidate_cannot_reuse_old_version(self):
        self.enable('image.generate')
        registry = self.guarded()
        route = self.route(registry, 'image.generate')
        with self.tasks._connect() as db:
            row = db.execute("SELECT capability_id,state_version FROM adult_model_capabilities").fetchone()
        record = registry.get(row['capability_id'])
        record = registry.revise(record.capability_id,
                                 replace(record.spec, max_output_bytes=2048), record.state_version)
        now_dt = datetime.now(timezone.utc)
        evidence = fixtures.CapabilityEvidence(capability_source='local_adapter_smoke_test',
            capability_reference='test:offline-image-v2',
            verified_at=now_dt.isoformat().replace('+00:00','Z'),
            expires_at=(now_dt+timedelta(hours=1)).isoformat().replace('+00:00','Z'),
            adapter_id='offline-image', adapter_version='1', task_type='image.generate',
            input_modalities=('text',), output_modalities=('image',))
        record = registry.verify(record.capability_id, evidence, record.state_version)
        registry.enable(record.capability_id, record.state_version)
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        with self.tasks._transaction() as db:
            self.assertFalse(registry.current_image_route_in_transaction(db, route, now))
        current = self.route(registry, 'image.generate')
        with self.tasks._transaction() as db:
            self.assertTrue(registry.current_image_route_in_transaction(db, current, now))
