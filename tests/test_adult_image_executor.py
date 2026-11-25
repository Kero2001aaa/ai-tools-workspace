"""Bounded image executor integration over real persistence, no network."""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import struct
import tempfile
import time
import unittest
import zlib
from dataclasses import replace
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_image_evaluator import AdultImageEvaluator
from agent.adult.adult_image_executor import AdultImageExecutor
from agent.adult.adult_image_intake import AdultImageIntake, ImageIntakeError
from agent.adult.adult_image_input import decode_image_input
from agent.adult.adult_image_pipeline import AdultImagePipeline
from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import (
    AdultTaskStore, StaleTaskVersion, TaskBudgetExhausted,
)
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.integrations.protocols import ImageAdapterResult, ImageAnalysisResult
from agent.adult.models import CapabilityEvidence, ModelCapabilitySpec, TaskBudget
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher

CONFIG = 'hmac-sha256:abcdef0123456789abcdef0123456789'
MODALITIES = {
    'image.analyze': (('image',), ('text',)),
    'image.generate': (('text',), ('image',)),
    'image.edit': (('image', 'text'), ('image',)),
}


def png_bytes(width=2, height=2):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Adapter:
    """Deterministic offline plumbing adapter. No model and no network."""

    def __init__(self):
        self.calls = {'generate': 0, 'edit': 0, 'analyze': 0}
        self.requests = []

    def generate(self, request):
        self.calls['generate'] += 1
        self.requests.append(request)
        return ImageAdapterResult(png_bytes(4, 4), 'image/png', {}, 0)

    def edit(self, request):
        self.calls['edit'] += 1
        self.requests.append(request)
        return ImageAdapterResult(png_bytes(4, 4), 'image/png', {}, 0)

    def analyze(self, request):
        self.calls['analyze'] += 1
        self.requests.append(request)
        return ImageAnalysisResult('Deterministic local plumbing description. Not AI inference.', {}, 0)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.cipher = PayloadCipher(self.key)
        self.tasks = AdultTaskStore(self.root / 'tasks.sqlite3')
        self.artifacts = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        self.adapter = Adapter()
        self.executor = self.compose(self.tasks, self.artifacts)
        self.image = png_bytes()

    def compose(self, tasks, artifacts):
        registry = ModelCapabilityRegistry(tasks)
        tracker = AdultAttemptTracker(
            tasks, route_validator=registry.current_image_route_in_transaction)
        router = AdultModelRouter(registry, approach_key_builder=tracker.approach_keys,
                                  local_adapter_available=lambda adapter_id: adapter_id == 'offline-image')
        policies = AdultImagePolicies()
        pipeline = AdultImagePipeline(artifacts, tasks, router, tracker, policies,
                                      AdultImageEvaluator(policies), {('offline-image', '1'): self.adapter})
        return AdultImageExecutor(tasks, AdultImageIntake(tasks, artifacts, self.cipher),
                                  pipeline, self.cipher)

    def enable(self, *task_types):
        registry = ModelCapabilityRegistry(self.tasks)
        now = datetime.now(timezone.utc)
        for task_type in task_types:
            input_modalities, output_modalities = MODALITIES[task_type]
            fields = dict(
                adapter_id='offline-image', adapter_version='1', task_type=task_type,
                input_modalities=input_modalities, output_modalities=output_modalities,
                cost_class='local')
            if 'image' in input_modalities:
                fields.update(accepted_mime_types=('image/png',), max_input_bytes=15 * 1024 * 1024,
                              max_input_width=8192, max_input_height=8192)
            if 'image' in output_modalities:
                fields.update(output_mime_types=('image/png',), max_output_bytes=1024 * 1024,
                              max_output_width=512, max_output_height=512)
            record = registry.register(ModelCapabilitySpec(**fields))
            evidence = CapabilityEvidence(
                capability_source='local_adapter_smoke_test',
                capability_reference='test:offline-image-v1',
                verified_at=now.isoformat().replace('+00:00', 'Z'),
                expires_at=(now + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
                adapter_id='offline-image', adapter_version='1', task_type=task_type,
                input_modalities=input_modalities, output_modalities=output_modalities)
            record = registry.verify(record.capability_id, evidence, record.state_version)
            registry.enable(record.capability_id, record.state_version)

    def test_generate_completes_reopens_and_never_duplicates(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        result = self.executor.run(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('completed', result.status)
        self.assertIsNotNone(result.artifact_id)
        self.assertEqual(png_bytes(4, 4), self.artifacts.read_chunk(result.artifact_id, 0).data)
        reopened = AdultTaskStore(self.root / 'tasks.sqlite3')
        persisted = reopened.get_task(task.task_id)
        self.assertEqual(result.artifact_id, persisted.latest_result_artifact_id)
        self.assertEqual(1, persisted.attempt_count)
        self.assertEqual('step_completed', reopened.get_latest_checkpoint(task.task_id).reason)
        self.assertEqual(result, self.executor.run(task.task_id, CONFIG))
        self.assertEqual(1, self.adapter.calls['generate'])
        self.assertNotIn(b'a red square', (self.root / 'tasks.sqlite3').read_bytes())

    def test_analyze_completes_with_task_scoped_image_reference(self):
        self.enable('image.analyze')
        task = self.executor.create('image.analyze', image=self.image)
        result = self.executor.run(task.task_id, CONFIG, image=self.image)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['analyze'])
        request = self.adapter.requests[0]
        self.assertEqual(task.task_id, request.task_id)
        self.assertEqual('image/png', request.mime_type)
        envelope_id = self.tasks.get_task_request(task.task_id).payload_id
        self.assertNotEqual(envelope_id, request.input_payload_id)
        self.assertEqual(self.image, self.artifacts.load_input(request.input_payload_id, task.task_id))
        chunk = self.artifacts.read_chunk(result.artifact_id, 0)
        self.assertEqual('text/plain', chunk.mime_type)
        self.assertIn(b'Not AI inference', chunk.data)

    def test_edit_completes_with_image_and_prompt_references(self):
        self.enable('image.edit')
        task = self.executor.create('image.edit', image=self.image, prompt='make it blue')
        result = self.executor.run(task.task_id, CONFIG, image=self.image, prompt='make it blue')
        self.assertEqual('completed', result.status)
        request = self.adapter.requests[0]
        self.assertEqual(self.image, self.artifacts.load_input(request.image_payload_id, task.task_id))
        self.assertEqual('make it blue',
                         self.artifacts.load_input(request.prompt_payload_id, task.task_id).decode())

    def test_stage_then_run_without_resupplying_input(self):
        self.enable('image.generate')
        staged = self.executor.stage('image.generate', prompt='a red square')
        self.assertEqual('checkpointed', staged.status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        result = self.executor.run(staged.task_id, CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['generate'])

    def test_unavailable_route_pauses_without_attempt(self):
        task = self.executor.create('image.generate', prompt='a red square')
        result = self.executor.run(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('paused', result.status)
        self.assertEqual('CAPABILITY_UNAVAILABLE', result.error_code)
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertIsNotNone(self.tasks.get_latest_checkpoint(task.task_id))

    def test_missing_input_needs_input(self):
        task = self.executor.create('image.generate', prompt='a red square')
        result = self.executor.run(task.task_id, CONFIG)
        self.assertEqual('needs_input', result.status)
        self.assertEqual('input_required', result.error_code)
        self.assertEqual(0, sum(self.adapter.calls.values()))

    def test_cancellation_before_run_prevents_call(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        self.tasks.request_cancel(task.task_id)
        self.assertEqual('cancelled',
                         self.executor.run(task.task_id, CONFIG, prompt='a red square').status)
        self.assertEqual(0, sum(self.adapter.calls.values()))

    def test_provider_failure_is_one_attempt_not_retry(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        with patch.object(self.adapter, 'generate', side_effect=TimeoutError()) as generate:
            result = self.executor.run(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('paused', result.status)
        self.assertEqual('PROVIDER_TIMEOUT', result.error_code)
        self.assertEqual(1, generate.call_count)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        prior = self.tasks.get_task(task.task_id)
        with patch.object(self.tasks, 'get_task', return_value=replace(prior, status='queued')):
            result = self.executor.run(task.task_id, CONFIG)
        self.assertEqual('prior_attempt_requires_recovery', result.error_code)
        self.assertEqual(1, generate.call_count)

    def test_cache_loss_after_attempt_is_recovery_not_needs_input(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        with patch.object(self.adapter, 'generate', side_effect=TimeoutError()):
            self.assertEqual('paused', self.executor.run(task.task_id, CONFIG,
                                                         prompt='a red square').status)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        self.artifacts.cache.discard(task.task_id)
        prior = self.tasks.get_task(task.task_id)
        with patch.object(self.tasks, 'get_task', return_value=replace(prior, status='queued')):
            result = self.executor.run(task.task_id, CONFIG)
        # A committed attempt keeps the explicit recovery boundary: no
        # needs_input re-upload path that restore_input would refuse anyway.
        self.assertEqual('prior_attempt_requires_recovery', result.error_code)
        with self.assertRaises(ValueError) as raised:
            self.executor.restore_input(task.task_id, prompt='a red square')
        self.assertIn('ephemeral_restore_unavailable', str(raised.exception))

    def test_policy_rejection_pauses_without_attempt_or_artifact(self):
        self.enable('image.analyze')
        task = self.executor.create('image.analyze', image=self.image)
        with patch('agent.adult.adult_image_pipeline.AdultImagePolicies.validate_image_input',
                   return_value=AdultImagePolicies().validate_analysis_output('image.analyze', '')):
            result = self.executor.run(task.task_id, CONFIG, image=self.image)
        self.assertEqual('paused', result.status)
        self.assertEqual('POLICY_REJECTED', result.error_code)
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)
        self.assertIsNone(self.tasks.get_task(task.task_id).latest_result_artifact_id)
        self.assertEqual(0, self.adapter.calls['analyze'])

    def test_invalid_input_creates_no_task(self):
        with self.assertRaises(Exception):
            self.executor.create('image.analyze', image=b'bad')
        with self.assertRaises(Exception):
            self.executor.create('image.generate', image=self.image)
        self.assertEqual((), self.tasks.list_tasks().items)

    def test_changed_content_at_run_is_rejected_before_storage(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        with self.assertRaises(ImageIntakeError):
            self.executor.run(task.task_id, CONFIG, prompt='a blue square')
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.assertIsNone(self.tasks.get_task_request(task.task_id).payload_id)
        self.assertEqual(0, self.artifacts.delete_task_inputs(task.task_id))

    def test_cancellation_during_adapter_prevents_completed_task(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')

        def cancel(request):
            self.tasks.request_cancel(task.task_id)
            return ImageAdapterResult(png_bytes(4, 4), 'image/png', {}, 0)

        with patch.object(self.adapter, 'generate', side_effect=cancel):
            with self.assertRaises(Exception):
                self.executor.run(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('cancelled', self.tasks.get_task(task.task_id).status)
        self.assertIsNone(self.tasks.get_task(task.task_id).latest_result_artifact_id)

    def test_transient_loss_needs_input_then_exact_restore_completes(self):
        self.enable('image.generate')
        staged = self.executor.stage('image.generate', prompt='a red square')
        self.assertEqual('checkpointed', staged.status)
        # A new store over the same directory has an empty process-local cache,
        # exactly like a restarted process with the same encryption key.
        tasks2 = AdultTaskStore(self.root / 'tasks.sqlite3')
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        executor2 = self.compose(tasks2, artifacts2)
        result = executor2.run(staged.task_id, CONFIG)
        self.assertEqual('needs_input', result.status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        with self.assertRaises(ValueError):
            executor2.restore_input(staged.task_id, prompt='a blue square')
        self.assertEqual('needs_input', tasks2.get_task(staged.task_id).status)
        result = executor2.restore_input_and_run(staged.task_id, CONFIG, prompt='a red square')
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['generate'])
        self.assertEqual(1, tasks2.get_task(staged.task_id).attempt_count)
        self.assertEqual(png_bytes(4, 4), artifacts2.read_chunk(result.artifact_id, 0).data)

    def test_restore_response_loss_never_dispatches_twice(self):
        self.enable('image.generate')
        staged = self.executor.stage('image.generate', prompt='a red square')
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        executor2 = self.compose(AdultTaskStore(self.root / 'tasks.sqlite3'), artifacts2)
        self.assertEqual('needs_input', executor2.run(staged.task_id, CONFIG).status)
        restored = executor2.restore_input(staged.task_id, prompt='a red square')
        self.assertEqual('queued', restored.status)
        # A repeated restore after a successful queue is refused before any dispatch.
        with self.assertRaises(ValueError):
            executor2.restore_input(staged.task_id, prompt='a red square')
        self.assertEqual(0, sum(self.adapter.calls.values()))
        result = executor2.run(staged.task_id, CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['generate'])

    def _staged_edit_with_parts(self, budget=None):
        self.enable('image.edit')
        staged = self.executor.stage('image.edit', image=self.image, prompt='make it blue',
                                     budget=budget)
        request = self.tasks.get_task_request(staged.task_id)
        envelope_bytes = self.artifacts.load_input(request.payload_id, staged.task_id)
        envelope = decode_image_input(envelope_bytes, 'image.edit')
        return staged, request, envelope_bytes, envelope

    def test_partial_cache_loss_restores_only_missing_part(self):
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts()
        # Evict only the image part; the envelope and prompt survive.
        self.artifacts.cache.discard_ref(staged.task_id, envelope.image_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        # The surviving envelope keeps its exact bytes and original references.
        self.assertEqual(envelope_bytes,
                         self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual(self.image,
                         self.artifacts.load_input(envelope.image_payload_id, staged.task_id))
        self.assertEqual('queued', self.tasks.get_task(staged.task_id).status)
        result = self.executor.run(staged.task_id, CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['edit'])

    def test_stale_restore_preserves_surviving_input(self):
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts()
        self.artifacts.cache.discard_ref(staged.task_id, envelope.image_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        real_get_task = self.tasks.get_task

        def stale(tid):
            task = real_get_task(tid)
            return replace(task, state_version=task.state_version - 1)

        with patch.object(self.tasks, 'get_task', side_effect=stale):
            with self.assertRaises(StaleTaskVersion):
                self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        # The failed restore never evicted or rewrote the surviving envelope.
        self.assertEqual(envelope_bytes,
                         self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual('needs_input', real_get_task(staged.task_id).status)
        # Only the missing part was staged; an unstale retry is byte-identical.
        self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        self.assertEqual('queued', real_get_task(staged.task_id).status)

    def test_expired_deadline_restore_preserves_surviving_input(self):
        budget = TaskBudget(max_total_attempts=20, max_wall_seconds=1, max_provider_calls=0)
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts(budget)
        self.artifacts.cache.discard_ref(staged.task_id, envelope.image_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        time.sleep(1.2)
        with self.assertRaises(TaskBudgetExhausted):
            self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        self.assertEqual(envelope_bytes,
                         self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual('needs_input', self.tasks.get_task(staged.task_id).status)

    def test_conflicting_survivor_refuses_restore_without_mutation(self):
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts()
        # Simulate a conflicting live payload and a genuinely missing part.
        self.artifacts.cache.discard_ref(staged.task_id, envelope.image_payload_id)
        self.artifacts.cache._put_reference(staged.task_id, envelope.image_payload_id,
                                            png_bytes(3, 3), kind='input')
        self.artifacts.cache.discard_ref(staged.task_id, envelope.prompt_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        with self.assertRaises(ValueError) as raised:
            self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        self.assertEqual('restored_input_mismatch', str(raised.exception))
        # No insertion happened: the missing prompt stays missing, bytes untouched.
        self.assertIsNone(self.artifacts.cache.get_input(staged.task_id,
                                                         envelope.prompt_payload_id))
        self.assertEqual(envelope_bytes,
                         self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual('needs_input', self.tasks.get_task(staged.task_id).status)

    def test_post_precheck_cache_loss_race_needs_input_then_restore(self):
        self.enable('image.generate')
        staged = self.executor.stage('image.generate', prompt='a red square')
        original = self.executor.pipeline.run_one_attempt

        def evict_before_pipeline(*args, **kwargs):
            self.artifacts.cache.discard(staged.task_id)
            return original(*args, **kwargs)

        with patch.object(self.executor.pipeline, 'run_one_attempt',
                          side_effect=evict_before_pipeline):
            result = self.executor.run(staged.task_id, CONFIG)
        # Zero-attempt loss after the pre-check still becomes needs_input.
        self.assertEqual('needs_input', result.status)
        self.assertEqual('input_required', result.error_code)
        self.assertEqual(0, self.tasks.get_task(staged.task_id).attempt_count)
        self.assertEqual(0, sum(self.adapter.calls.values()))
        result = self.executor.restore_input_and_run(staged.task_id, CONFIG,
                                                     prompt='a red square')
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['generate'])

    def test_restore_capacity_failure_never_evicts_surviving_input(self):
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts()
        self.artifacts.cache.discard_ref(staged.task_id, envelope.image_payload_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        cache = self.artifacts.cache
        cache._max_bytes = cache._total_bytes + len(self.image) - 1
        before = dict(cache._entries)
        with self.assertRaises(ValueError) as raised:
            self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        self.assertEqual('ephemeral_restore_capacity_unavailable', str(raised.exception))
        self.assertEqual(before, dict(cache._entries))
        self.assertEqual(envelope_bytes, self.artifacts.load_input(request.payload_id, staged.task_id))
        self.assertEqual('needs_input', self.tasks.get_task(staged.task_id).status)

    def test_full_loss_restore_preflights_later_reference_conflict(self):
        staged, request, envelope_bytes, envelope = self._staged_edit_with_parts()
        self.artifacts.cache.discard(staged.task_id)
        self.assertEqual('needs_input', self.executor.run(staged.task_id, CONFIG).status)
        from agent.adult.adult_image_intake import _content_reference
        prompt_id = _content_reference(b'make it blue')
        self.artifacts.cache._put_reference(staged.task_id, prompt_id, b'different', kind='input')
        before = dict(self.artifacts.cache._entries)
        with self.assertRaises(ValueError):
            self.executor.restore_input(staged.task_id, image=self.image, prompt='make it blue')
        self.assertEqual(before, dict(self.artifacts.cache._entries))
        self.assertEqual('needs_input', self.tasks.get_task(staged.task_id).status)

    def test_terminal_restore_refused(self):
        self.enable('image.generate')
        task = self.executor.create('image.generate', prompt='a red square')
        self.assertEqual('completed',
                         self.executor.run(task.task_id, CONFIG, prompt='a red square').status)
        with self.assertRaises(ValueError) as raised:
            self.executor.restore_input(task.task_id, prompt='a red square')
        self.assertEqual('ephemeral_restore_unavailable', str(raised.exception))
        cancelled = self.executor.create('image.generate', prompt='a red square')
        self.tasks.request_cancel(cancelled.task_id)
        with self.assertRaises(ValueError):
            self.executor.restore_input(cancelled.task_id, prompt='a red square')

    def test_durable_encrypted_stage_reopen_and_run(self):
        self.enable('image.generate')
        with patch.dict('os.environ', {'ADULT_PAYLOAD_ENCRYPTION_KEY': self.key}):
            staged = self.executor.stage_encrypted('image.generate', prompt='a red square')
        self.assertEqual('checkpointed', staged.status)
        tasks2 = AdultTaskStore(self.root / 'tasks.sqlite3')
        artifacts2 = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        executor2 = self.compose(tasks2, artifacts2)
        result = executor2.run(staged.task_id, CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual(1, self.adapter.calls['generate'])
        # Retained input remains readable until the retention boundary handles it.
        request = tasks2.get_task_request(staged.task_id)
        self.assertIsNotNone(artifacts2.load_input(request.payload_id, staged.task_id))


if __name__ == '__main__':
    unittest.main()
