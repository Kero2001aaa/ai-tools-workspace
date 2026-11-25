"""Actual bounded image pipeline failures and durable wait/resume, no live service."""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_image_evaluator import AdultImageEvaluator
from agent.adult.adult_image_executor import AdultImageExecutor
from agent.adult.adult_image_intake import AdultImageIntake
from agent.adult.adult_image_pipeline import AdultImagePipeline
from agent.adult.adult_image_policies import AdultImagePolicies
from agent.adult.adult_image_supervisor import AdultImageSupervisor
from agent.adult.adult_router import AdultModelRouter
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.capability_registry import ModelCapabilityRegistry
from agent.adult.integrations.protocols import ImageAdapterResult
from agent.adult.models import CapabilityEvidence, ModelCapabilitySpec, TaskBudget
from agent.adult.payload_store import AdultArtifactStore, PayloadCipher
from tests import test_adult_image_executor as fixtures

CONFIG = fixtures.CONFIG


class TimeoutAdapter:
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise TimeoutError()


class SuccessAdapter:
    """Deterministic offline plumbing adapter. No model and no network."""

    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return ImageAdapterResult(fixtures.png_bytes(4, 4), 'image/png', {}, 0)


class ImageSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.cipher = PayloadCipher(self.key)
        self.tasks = AdultTaskStore(self.root / 'tasks.sqlite3')
        self.artifacts = AdultArtifactStore(self.root / 'artifacts', cipher=self.cipher)
        self.registry = ModelCapabilityRegistry(self.tasks)
        self.tracker = AdultAttemptTracker(
            self.tasks, route_validator=self.registry.current_image_route_in_transaction)
        self.router = AdultModelRouter(self.registry, approach_key_builder=self.tracker.approach_keys,
                                       local_adapter_available=lambda adapter_id: False)
        self.policies = AdultImagePolicies()
        self.evaluator = AdultImageEvaluator(self.policies)
        self.pipeline = AdultImagePipeline(self.artifacts, self.tasks, self.router, self.tracker,
                                           self.policies, self.evaluator, {})
        self.intake = AdultImageIntake(self.tasks, self.artifacts, self.cipher)
        self.executor = AdultImageExecutor(self.tasks, self.intake, self.pipeline, self.cipher)
        self.supervisor = AdultImageSupervisor(self.executor)

    def routes(self, count, succeed_last=False):
        self.adapters = []
        now = datetime.now(timezone.utc)
        for index in range(count):
            name = f'offline-supervised-image-{index}'
            adapter = SuccessAdapter() if (succeed_last and index == count - 1) else TimeoutAdapter()
            self.adapters.append(adapter)
            self.pipeline._adapters[(name, '1')] = adapter
            spec = ModelCapabilitySpec(
                adapter_id=name, adapter_version='1', task_type='image.generate',
                input_modalities=('text',), output_modalities=('image',), cost_class='local',
                output_mime_types=('image/png',), max_output_bytes=1024 * 1024,
                max_output_width=512, max_output_height=512)
            record = self.registry.register(spec)
            evidence = CapabilityEvidence(
                capability_source='local_adapter_smoke_test',
                capability_reference='test:supervisor-image-local',
                verified_at=now.isoformat().replace('+00:00', 'Z'),
                expires_at=(now + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
                adapter_id=name, adapter_version='1', task_type='image.generate',
                input_modalities=('text',), output_modalities=('image',))
            record = self.registry.verify(record.capability_id, evidence, record.state_version)
            self.registry.enable(record.capability_id, record.state_version)
        self.router._local_adapter_available = (
            lambda adapter_id: adapter_id.startswith('offline-supervised-image-'))

    def test_exact_five_wait_reopen_explicit_resume_then_sixth_success_no_replay(self):
        self.routes(6, succeed_last=True)
        task = self.executor.create('image.generate', prompt='a red square')
        result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('waiting_user', result.status)
        current = self.tasks.get_task(task.task_id)
        self.assertEqual((5, 5, True),
                         (current.attempt_count, current.failed_attempts_since_approval,
                          current.user_action_required))
        self.assertEqual([1, 1, 1, 1, 1, 0], [a.calls for a in self.adapters])
        checkpoint = self.tasks.get_latest_checkpoint(task.task_id)
        self.assertEqual('approval_required', checkpoint.reason)
        self.assertEqual(5, len(checkpoint.snapshot.excluded_approach_keys))
        self.assertEqual('waiting_user', self.supervisor.run_until_done(task.task_id, CONFIG).status)
        with self.assertRaises(ValueError):
            self.supervisor.continue_after_approval(task.task_id, CONFIG)
        # Synthetic explicit decision validates software behavior, not owner consent.
        self.tasks.resume(task.task_id, 'continue')
        result = AdultImageSupervisor(self.executor).continue_after_approval(task.task_id, CONFIG)
        self.assertEqual('completed', result.status)
        self.assertEqual([1] * 6, [a.calls for a in self.adapters])
        self.assertEqual((6, 1), (self.tasks.get_task(task.task_id).attempt_count,
                                  self.tasks.get_task(task.task_id).approvals_granted))
        self.assertEqual(result, self.supervisor.run_until_done(task.task_id, CONFIG))

    def test_segment_limit_checkpoint_then_distinct_next_route(self):
        self.routes(3, succeed_last=True)
        task = self.executor.create('image.generate', prompt='a red square')
        first = self.supervisor.run_until_done(task.task_id, CONFIG,
                                               prompt='a red square', max_attempts=1)
        self.assertEqual('checkpointed', first.status)
        self.assertEqual('completed', self.supervisor.run_until_done(task.task_id, CONFIG).status)
        self.assertEqual([1, 1, 1], [a.calls for a in self.adapters])

    def test_no_route_pauses_zero_attempt_and_no_generic_resume(self):
        self.routes(0)
        task = self.executor.create('image.generate', prompt='a red square')
        self.assertEqual('paused', self.supervisor.run_until_done(
            task.task_id, CONFIG, prompt='a red square').status)
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual('paused', self.supervisor.run_until_done(task.task_id, CONFIG).status)

    def test_total_attempt_cap_on_fifth_fails_instead_of_wait(self):
        self.routes(6)
        task = self.executor.create('image.generate', prompt='a red square',
                                    budget=TaskBudget(max_total_attempts=5))
        result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('failed', result.status)
        current = self.tasks.get_task(task.task_id)
        self.assertEqual(('failed', 5, False),
                         (current.status, current.attempt_count, current.user_action_required))
        self.assertEqual([1, 1, 1, 1, 1, 0], [a.calls for a in self.adapters])

    def test_unknown_committed_attempt_is_not_replayed(self):
        self.routes(2)
        task = self.executor.create('image.generate', prompt='a red square')

        class ProcessLoss(BaseException):
            pass
        with patch.object(self.adapters[0], 'generate', side_effect=ProcessLoss):
            with self.assertRaises(ProcessLoss):
                self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        later = (datetime.now(timezone.utc) + timedelta(seconds=301)).isoformat().replace('+00:00', 'Z')
        with patch('agent.adult.adult_task_store._utc_now', return_value=later):
            self.tasks.recover_expired_leases()
        result = self.supervisor.run_until_done(task.task_id, CONFIG)
        self.assertEqual('prior_attempt_requires_recovery', result.error_code)
        self.assertEqual(0, self.adapters[1].calls)

    def test_invalid_segment_limits_rejected_before_task_change(self):
        self.routes(1)
        task = self.executor.create('image.generate', prompt='a red square')
        for kwargs in ({'max_attempts': 6}, {'max_attempts': True},
                       {'max_seconds': 241}, {'max_seconds': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.supervisor.run_until_done(task.task_id, CONFIG, **kwargs)
        self.assertEqual('queued', self.tasks.get_task(task.task_id).status)

    def test_nonretryable_storage_failure_stops_after_one(self):
        self.routes(2, succeed_last=True)
        task = self.executor.create('image.generate', prompt='a red square')
        # First adapter produces accepted image bytes but artifact write fails permanently.
        self.pipeline._adapters[('offline-supervised-image-0', '1')] = SuccessAdapter()
        with patch.object(self.artifacts, 'save_output', side_effect=OSError):
            result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('paused', result.status)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0, self.adapters[1].calls)

    def test_cancel_between_distinct_attempts_prevents_next_call(self):
        self.routes(3)
        task = self.executor.create('image.generate', prompt='a red square')
        original = self.executor._checkpoint

        def checkpoint_then_cancel(current, *args, **kwargs):
            original(current, *args, **kwargs)
            if current.attempt_count == 1:
                self.tasks.request_cancel(task.task_id)
        with patch.object(self.executor, '_checkpoint', side_effect=checkpoint_then_cancel):
            result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('cancelled', result.status)
        self.assertEqual([1, 0, 0], [a.calls for a in self.adapters])

    def test_invalid_evaluation_is_nonretryable_and_never_routes_around(self):
        self.routes(2)
        task = self.executor.create('image.generate', prompt='a red square')
        self.pipeline._adapters[('offline-supervised-image-0', '1')] = SuccessAdapter()
        with patch.object(self.pipeline._evaluator, 'evaluate', return_value=None):
            result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('paused', result.status)
        self.assertEqual(0, self.adapters[1].calls)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)

    def test_wall_segment_yields_without_second_attempt(self):
        self.routes(3)
        task = self.executor.create('image.generate', prompt='a red square')
        with patch('agent.adult.adult_image_executor.time.monotonic', side_effect=[0, 2]):
            result = self.supervisor.run_until_done(task.task_id, CONFIG,
                                                    prompt='a red square', max_seconds=1)
        self.assertEqual('checkpointed', result.status)
        self.assertEqual([1, 0, 0], [a.calls for a in self.adapters])

    def test_five_failure_wait_recovery_before_approval_still_no_sixth(self):
        self.routes(6)
        task = self.executor.create('image.generate', prompt='a red square')
        original = self.executor._checkpoint

        class ProcessLoss(BaseException):
            pass

        def crash_before_wait(*args, **kwargs):
            if kwargs.get('waiting'):
                raise ProcessLoss()
            return original(*args, **kwargs)
        with patch.object(self.executor, '_checkpoint', side_effect=crash_before_wait):
            with self.assertRaises(ProcessLoss):
                self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        later = (datetime.now(timezone.utc) + timedelta(seconds=301)).isoformat().replace('+00:00', 'Z')
        with patch('agent.adult.adult_task_store._utc_now', return_value=later):
            self.tasks.recover_expired_leases()
        result = self.supervisor.run_until_done(task.task_id, CONFIG)
        self.assertEqual('waiting_user', result.status)
        self.assertEqual([1, 1, 1, 1, 1, 0], [a.calls for a in self.adapters])

    def test_output_policy_rejection_is_terminal_and_no_other_route_runs(self):
        from agent.adult.models import EvaluationResult
        self.routes(2)
        task = self.executor.create('image.generate', prompt='a red square')
        self.pipeline._adapters[('offline-supervised-image-0', '1')] = SuccessAdapter()
        denied = EvaluationResult(False, 0.0, (), ('policy_rejected',), False)
        with patch.object(self.pipeline._evaluator, 'evaluate', return_value=denied):
            result = self.supervisor.run_until_done(task.task_id, CONFIG, prompt='a red square')
        self.assertEqual('failed', result.status)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0, self.adapters[1].calls)

    def test_unstaged_task_needs_input_zero_calls(self):
        self.routes(1, succeed_last=True)
        task = self.executor.create('image.generate', prompt='a red square')
        result = self.supervisor.run_until_done(task.task_id, CONFIG)
        self.assertEqual('needs_input', result.status)
        self.assertEqual('input_required', result.error_code)
        self.assertEqual(0, self.adapters[0].calls)
        self.assertEqual(0, self.tasks.get_task(task.task_id).attempt_count)

    def test_cached_input_loss_after_committed_attempt_pauses_without_reupload(self):
        self.routes(2)
        task = self.executor.create('image.generate', prompt='a red square')
        first = self.supervisor.run_until_done(task.task_id, CONFIG,
                                               prompt='a red square', max_attempts=1)
        self.assertEqual('checkpointed', first.status)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        self.artifacts.cache.discard(task.task_id)
        result = self.supervisor.run_until_done(task.task_id, CONFIG)
        # A committed attempt keeps the explicit recovery boundary: no
        # needs_input re-upload path that restore_input would refuse anyway,
        # and no further route is dispatched without its exact input bytes.
        self.assertEqual('paused', result.status)
        self.assertEqual('INVALID_INPUT', result.error_code)
        self.assertEqual('paused', self.tasks.get_task(task.task_id).status)
        self.assertEqual(1, self.tasks.get_task(task.task_id).attempt_count)
        self.assertEqual(0, self.adapters[1].calls)
        with self.assertRaises(ValueError) as raised:
            self.executor.restore_input(task.task_id, prompt='a red square')
        self.assertIn('ephemeral_restore_unavailable', str(raised.exception))
