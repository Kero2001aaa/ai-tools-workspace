"""Offline integration through real stores, registry, router and text pipeline."""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_evaluator import AdultEvaluator
from agent.adult.adult_policies import AdultPolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore, InvalidTaskTransition
from agent.adult.adult_text_input import encode_text_input
from agent.adult.adult_text_intake import AdultTextIntake, TextIntakeError
from agent.adult.adult_text_pipeline import AdultTextPipeline
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.integrations.protocols import TextAdapterResult
from agent.adult.models import AdultTaskRequest, TaskBudget, StepRecord, ModelCapabilitySpec, CapabilityEvidence
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher, PayloadNotFound

CONFIG = 'hmac-sha256:abcdef0123456789abcdef0123456789'


class OfflineAdapter:
    def __init__(self):
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        return TextAdapterResult('A deterministic offline test output.', {}, 0)


class AdultTextIntakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AdultTaskStore(self.root / 'tasks.sqlite3')
        self.cipher = PayloadCipher(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii'))
        self.artifacts = AdultArtifactStore(self.root / 'payloads', cipher=self.cipher)
        self.data = encode_text_input('text.analyze', 'Private intake integration test source.')
        self.intake = AdultTextIntake(self.store, self.artifacts, self.cipher)
        self.task = self.store.create_task(AdultTaskRequest('text.analyze', self.cipher.fingerprint(self.data)), TaskBudget())
        self.lease = self.store.claim_task(self.task.task_id, 'offline-test', lease_seconds=300)

    def attach(self):
        current = self.store.get_task(self.task.task_id)
        return self.intake.attach(current.task_id, self.data, self.lease, current.state_version)

    def test_attach_reopen_and_single_assignment(self):
        task = self.attach()
        request = AdultTaskStore(self.root / 'tasks.sqlite3').get_task_request(task.task_id)
        self.assertEqual(self.data, self.artifacts.load_input(request.payload_id, task.task_id))
        self.assertEqual(2, task.state_version)
        with self.assertRaises(TextIntakeError):
            self.attach()
        with self.assertRaises(InvalidTaskTransition):
            self.store.attach_input(task.task_id, 'a' * 32, self.lease, task.state_version)
        self.assertNotIn(self.data, (self.root / 'tasks.sqlite3').read_bytes())

    def test_fingerprint_mismatch_creates_no_payload(self):
        with self.assertRaises(TextIntakeError):
            self.intake.attach(self.task.task_id, encode_text_input('text.analyze', 'Changed input.'), self.lease, 1)
        self.assertIsNone(self.store.get_task_request(self.task.task_id).payload_id)
        self.assertEqual([], list((self.root / 'payloads').glob('*.aap')))

    def test_stale_attach_cleans_created_input(self):
        with self.assertRaises(TextIntakeError):
            self.intake.attach(self.task.task_id, self.data, self.lease, 0)
        self.assertIsNone(self.store.get_task_request(self.task.task_id).payload_id)
        self.assertEqual([], list((self.root / 'payloads').glob('*.aap')))
        self.assertEqual(0, len(self.artifacts.cache._entries))

    def test_wrong_lease_cleans_created_input(self):
        other = self.store.create_task(AdultTaskRequest('text.analyze', self.cipher.fingerprint(self.data)), TaskBudget())
        # A genuine lease for another task must be rejected, without forged tokens.
        with self.assertRaises(TextIntakeError):
            self.intake.attach(other.task_id, self.data, self.lease, 0)
        self.assertIsNone(self.store.get_task_request(self.task.task_id).payload_id)
        self.assertEqual([], list((self.root / 'payloads').glob('*.aap')))
        self.assertEqual(0, len(self.artifacts.cache._entries))

    def test_discard_rejects_cross_task_and_output(self):
        ref = self.artifacts.save_input(self.task.task_id, self.data, 'application/json', retain_encrypted=True)
        with self.assertRaises(PayloadNotFound):
            self.artifacts.discard_input(ref.payload_id, 'other-task')
        out = self.artifacts.save_output(self.task.task_id, 'attempt-test', b'test output', 'text/plain')
        with self.assertRaises(PayloadNotFound):
            self.artifacts.discard_input(out.artifact_id, self.task.task_id)
        self.assertEqual(self.data, self.artifacts.load_input(ref.payload_id, self.task.task_id))
        self.assertTrue(self.artifacts.discard_input(ref.payload_id, self.task.task_id))
        self.assertFalse(self.artifacts.discard_input(ref.payload_id, self.task.task_id))

    def test_invalid_envelope_and_cleanup_error_are_explicit(self):
        with self.assertRaises(TextIntakeError):
            self.intake.attach(self.task.task_id, b'not-json', self.lease, 1)
        with patch.object(self.artifacts, 'discard_input', side_effect=OSError()):
            with self.assertRaisesRegex(TextIntakeError, 'input_cleanup_failed'):
                self.intake.attach(self.task.task_id, self.data, self.lease, 0)

    def test_encrypted_attach_survives_reopen_and_excludes_plaintext(self):
        # The generated key is test-only, scoped to this temporary directory.
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
        cipher = PayloadCipher(key)
        artifacts = AdultArtifactStore(self.root / 'retained', cipher=cipher)
        with patch.dict('os.environ', {'ADULT_PAYLOAD_ENCRYPTION_KEY':key}):
            task = self.store.create_task(AdultTaskRequest('text.analyze', cipher.fingerprint(self.data), retention_policy='encrypted'), TaskBudget())
        # Release the first task before claiming the independent retained task.
        self.store.release_lease(self.lease, 1, reason='yielded')
        lease = self.store.claim_task(task.task_id, 'retained-worker', lease_seconds=300)
        current = self.store.get_task(task.task_id)
        AdultTextIntake(self.store, artifacts, cipher).attach(task.task_id, self.data, lease, current.state_version)
        request = AdultTaskStore(self.root / 'tasks.sqlite3').get_task_request(task.task_id)
        reopened = AdultArtifactStore(self.root / 'retained', cipher=cipher)
        self.assertEqual(self.data, reopened.load_input(request.payload_id, task.task_id))
        self.assertNotIn(self.data, (self.root / 'retained' / (request.payload_id + '.aap')).read_bytes())
        self.assertNotIn(self.data, (self.root / 'tasks.sqlite3').read_bytes())

    def test_discard_transient_cannot_remove_output_or_other_task_input(self):
        artifacts = AdultArtifactStore(self.root / 'transient')
        ref = artifacts.save_input('task-a', b'input', 'text/plain', retain_encrypted=False)
        self.assertFalse(artifacts.discard_input(ref.payload_id, 'task-b'))
        self.assertEqual(b'input', artifacts.load_input(ref.payload_id, 'task-a'))
        out = artifacts.save_output('task-a', 'attempt-a', b'output', 'text/plain')
        with self.assertRaises(PayloadNotFound):
            artifacts.discard_input(out.artifact_id, 'task-a')
        self.assertEqual(b'output', artifacts.read_chunk(out.artifact_id, 0).data)

    def test_real_intake_registry_router_attempt_pipeline_and_artifact(self):
        task = self.attach()
        intake_step = self.store.get_step(task.task_id, task.current_step_id)
        self.store.save_step(task.task_id, replace(intake_step, status='skipped'), self.lease, task.state_version)
        task = self.store.get_task(task.task_id)
        request = self.store.get_task_request(task.task_id)
        step = StepRecord('text-step-000000000001', task.task_id, 1, 'text.analyze', 'pending', input_payload_id=request.payload_id, config_fingerprint=CONFIG)
        self.store.save_step(task.task_id, step, self.lease, task.state_version)
        task = self.store.get_task(task.task_id)
        self.store.save_step(task.task_id, replace(step, status='running'), self.lease, task.state_version)
        task = self.store.get_task(task.task_id)
        registry = ModelCapabilityRegistry(self.store)
        spec = ModelCapabilitySpec(adapter_id='offline-text', adapter_version='1', task_type='text.analyze', input_modalities=('text',), output_modalities=('text',), cost_class='local')
        record = registry.register(spec)
        now = datetime.now(timezone.utc)
        evidence = CapabilityEvidence(capability_source='local_adapter_smoke_test', capability_reference='test:offline-text-v1', verified_at=now.isoformat().replace('+00:00','Z'), expires_at=(now+timedelta(hours=1)).isoformat().replace('+00:00','Z'), adapter_id='offline-text', adapter_version='1', task_type='text.analyze', input_modalities=('text',), output_modalities=('text',))
        record = registry.verify(record.capability_id, evidence, record.state_version)
        registry.enable(record.capability_id, record.state_version)
        tracker = AdultAttemptTracker(self.store)
        router = AdultModelRouter(registry, approach_key_builder=tracker.approach_keys, local_adapter_available=lambda identifier: identifier == 'offline-text')
        adapter = OfflineAdapter()
        pipeline = AdultTextPipeline(self.artifacts, self.store, router, tracker, AdultPolicies(), AdultEvaluator(AdultPolicies()), {('offline-text','1'):adapter})
        result = pipeline.run_one_attempt(task, step.step_id, request, CONFIG, set())
        self.assertEqual('succeeded', result.status, result)
        self.assertEqual(1, adapter.calls)
        self.assertEqual(b'A deterministic offline test output.', self.artifacts.read_chunk(result.artifact.artifact_id, 0).data)
        reopened = AdultTaskStore(self.root / 'tasks.sqlite3').get_task(task.task_id)
        self.assertEqual(1, reopened.attempt_count)
        self.assertEqual(0, reopened.spent_cost_microusd)
        self.assertNotIn('A deterministic offline test output.', repr(result))
