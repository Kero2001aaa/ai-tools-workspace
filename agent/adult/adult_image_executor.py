"""One bounded image journey over existing stores and the one-attempt pipeline.

No scheduler, network client, registry auto-certification, supervision or retry
loop. Callers supply an already configured pipeline. Uncertain prior attempts
are never replayed. Lost process-local input moves a task to needs_input with
zero provider calls; only exact validated content can restore it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hmac
import uuid

from agent.adult.adult_image_input import (
    ImageInputError,
    decode_image_input,
    decode_image_prompt,
    encode_image_input,
    encode_image_prompt,
)
from agent.adult.adult_image_intake import (
    AdultImageIntake,
    _content_reference,
    canonical_image_input,
)
from agent.adult.adult_image_pipeline import AdultImagePipeline
from agent.adult.adult_task_store import AdultTaskStore, TaskBudgetExhausted
from agent.adult.models import (
    AdultTaskRequest,
    CheckpointSnapshot,
    ImageInputEnvelope,
    StepRecord,
    TaskBudget,
)
from agent.adult.payload_store import PayloadCipher, PayloadUnavailable

_IMAGE_TASK_TYPES = frozenset({"image.analyze", "image.generate", "image.edit"})


@dataclass(frozen=True)
class ImageExecutionResult:
    task_id: str
    status: str
    artifact_id: str | None = None
    error_code: str | None = None


class AdultImageExecutor:
    """Own one lease and one invocation at most, never implicit approval/retry."""

    def __init__(self, tasks: AdultTaskStore, intake: AdultImageIntake,
                 pipeline: AdultImagePipeline, fingerprinter: PayloadCipher):
        if not isinstance(tasks, AdultTaskStore) or not isinstance(intake, AdultImageIntake):
            raise TypeError('Real task store and intake are required.')
        if not isinstance(pipeline, AdultImagePipeline) or not isinstance(fingerprinter, PayloadCipher):
            raise TypeError('Real pipeline and fingerprinter are required.')
        if intake.tasks is not tasks or pipeline._task_store is not tasks:
            raise ValueError('Executor components must share one task store.')
        if intake.artifacts is not pipeline._artifact_store:
            raise ValueError('Executor components must share one artifact store.')
        if intake.fingerprinter.key_id != fingerprinter.key_id:
            raise ValueError('Executor fingerprint keys must match.')
        self.tasks, self.intake, self.pipeline, self.fingerprinter = tasks, intake, pipeline, fingerprinter

    def create(self, task_type: str, *, image: bytes | None = None, prompt: str | None = None,
               budget: TaskBudget | None = None, retention_policy: str = "ephemeral"):
        # Validate before creating a durable task; private content never enters its DTO.
        canonical = canonical_image_input(task_type, image, prompt)
        if retention_policy == "encrypted":
            configured = PayloadCipher.from_environment()
            if configured.key_id != self.fingerprinter.key_id:
                raise ValueError("configured_key_mismatch")
            if (self.intake.artifacts.cipher is None
                    or self.intake.artifacts.cipher.key_id != configured.key_id):
                raise ValueError("artifact_key_mismatch")
        return self.tasks.create_task(
            AdultTaskRequest(task_type, self.fingerprinter.fingerprint(canonical),
                             retention_policy=retention_policy),
            budget if budget is not None else TaskBudget(),
        )

    def stage_encrypted(self, task_type: str, *, image: bytes | None = None,
                        prompt: str | None = None, budget: TaskBudget | None = None,
                        worker_id="image-intake-worker") -> ImageExecutionResult:
        """Compatibility entry point for explicit encrypted durable staging."""
        return self.stage(task_type, image=image, prompt=prompt, budget=budget,
                          retention_policy="encrypted", worker_id=worker_id)

    def stage(self, task_type: str, *, image: bytes | None = None, prompt: str | None = None,
              budget: TaskBudget | None = None, retention_policy="ephemeral",
              worker_id="image-intake-worker") -> ImageExecutionResult:
        """Attach bounded input and checkpoint without selecting or calling a model.

        Ephemeral input uses the existing process-local cache only. Encrypted
        retention requires the caller's configured key before task creation.
        """
        task = self.create(task_type, image=image, prompt=prompt, budget=budget,
                           retention_policy=retention_policy)
        lease = self.tasks.claim_task(task.task_id, worker_id, lease_seconds=300)
        try:
            task = self.tasks.get_task(task.task_id)
            self.intake.attach(task.task_id, image=image, prompt=prompt,
                               lease=lease, expected_state_version=task.state_version)
            task = self.tasks.get_task(task.task_id)
            request = self.tasks.get_task_request(task.task_id)
            step = self.tasks.get_step(task.task_id, task.current_step_id)
            self._checkpoint(task, step, request, lease)
            task = self.tasks.get_task(task.task_id)
            self.tasks.release_lease(lease, task.state_version, "checkpointed", "yielded")
            return ImageExecutionResult(task.task_id, "checkpointed")
        except Exception:
            # Preserve any committed input; a later read reconciles unknown state.
            current = self.tasks.get_task(task.task_id)
            if current and current.status in {"analyzing", "running", "evaluating"}:
                try:
                    self.tasks.release_lease(lease, current.state_version,
                        "cancelled" if current.cancel_requested else "paused",
                        "cancel_observed" if current.cancel_requested else "worker_released")
                except Exception:
                    pass
            raise

    def _checkpoint(self, task, step, request, lease, *, artifact_id=None, completed=False, waiting=False):
        remaining = max(0, int((datetime.fromisoformat(task.deadline_at.replace('Z', '+00:00'))
                                - datetime.now(timezone.utc)).total_seconds()))
        attempts = self.pipeline._attempt_tracker.list_summaries(task.task_id)
        snapshot = CheckpointSnapshot(
            1, task.task_id, task.task_type, 'waiting_user' if waiting else task.status,
            active_step_id=task.current_step_id,
            input_fingerprint=request.input_fingerprint, payload_id=request.payload_id,
            plan=(step.step_id,),
            completed_step_ids=(step.step_id,) if completed else (),
            pending_step_ids=() if completed else (step.step_id,),
            latest_result_artifact_id=artifact_id,
            selected_adapter=task.selected_adapter, selected_provider=task.selected_provider,
            selected_model=task.selected_model, selected_tool=task.selected_tool,
            attempted_approach_keys=tuple(item.approach_key for item in attempts),
            excluded_approach_keys=tuple(item.approach_key for item in attempts
                if item.status in {'failed', 'timed_out', 'rejected', 'unknown_outcome'}),
            pending_user_action='approval_required' if waiting else None,
            allowed_decisions=('continue', 'cancel') if waiting else (),
            total_attempts=task.attempt_count,
            failed_attempts_since_approval=task.failed_attempts_since_approval,
            provider_calls=task.provider_calls, spent_cost_microusd=task.spent_cost_microusd,
            remaining_attempts=max(0, task.max_total_attempts-task.attempt_count),
            remaining_wall_seconds=remaining,
            remaining_provider_calls=max(0, task.max_provider_calls-task.provider_calls),
            remaining_cost_microusd=0, last_error_code=task.last_error_code,
        )
        if waiting:
            self.tasks.set_waiting_user(task.task_id, snapshot, lease, task.state_version)
        else:
            self.tasks.save_checkpoint(task.task_id, snapshot,
                                       'step_completed' if completed else 'manual_yield',
                                       task.state_version, lease)

    def _needs_input(self, task_id: str, task) -> ImageExecutionResult:
        """Move an ephemeral task with lost process-local input to needs_input.

        Only an absent process-local input is a recoverable loss here. Wrong
        keys, authentication errors or retained input expiry never become a
        silent replacement permission.
        """
        if task.status == 'checkpointed':
            self.tasks.transition(task_id, {'checkpointed'}, 'needs_input',
                                  'state_transition', task.state_version)
        else:
            # A restore can queue successfully before process loss.
            # Use the existing lease path, never generic active writes.
            lease = self.tasks.claim_task(task_id, 'missing-transient-input', lease_seconds=30)
            current = self.tasks.get_task(task_id)
            self.tasks.release_lease(lease, current.state_version, 'needs_input')
        return ImageExecutionResult(task_id, 'needs_input', error_code='input_required')

    def _input_missing(self, task, request) -> bool:
        """True only when required process-local input is absent.

        Present input is rebound to the durable content fingerprint; a
        mismatch is never softened. An undecodable payload counts as present,
        so the pipeline's own bounded validation produces the outcome.
        """
        try:
            envelope_data = self.intake.artifacts.load_input(request.payload_id, task.task_id)
            envelope = decode_image_input(envelope_data, task.task_type)
            image = (self.intake.artifacts.load_input(envelope.image_payload_id, task.task_id)
                     if envelope.image_payload_id is not None else None)
            prompt = (decode_image_prompt(
                          self.intake.artifacts.load_input(envelope.prompt_payload_id, task.task_id))
                      if envelope.prompt_payload_id is not None else None)
            canonical = canonical_image_input(task.task_type, image, prompt)
            if not hmac.compare_digest(
                self.fingerprinter.fingerprint(canonical), request.input_fingerprint
            ):
                raise ValueError('input_key_mismatch')
        except PayloadUnavailable:
            if request.retention_policy != 'ephemeral':
                raise
            return True
        except ImageInputError:
            # Present but undecodable: the pipeline produces the bounded outcome.
            pass
        return False

    def run(self, task_id: str, config_fingerprint: str, *, image: bytes | None = None,
            prompt: str | None = None, worker_id='bounded-image-worker') -> ImageExecutionResult:
        task = self.tasks.get_task(task_id)
        if task is None:
            raise ValueError('task_unavailable')
        if task.task_type not in _IMAGE_TASK_TYPES:
            raise ValueError('image_task_unavailable')
        if task.status in {'completed', 'failed', 'cancelled'}:
            return ImageExecutionResult(task_id, task.status, task.latest_result_artifact_id)
        if task.status == 'needs_input':
            return ImageExecutionResult(task_id, 'needs_input', error_code='input_required')
        if task.status not in {'queued', 'checkpointed'}:
            return ImageExecutionResult(task_id, task.status, error_code='task_not_runnable')
        request = self.tasks.get_task_request(task_id)
        # Unknown, successful or nonretryable prior attempts are never replayed.
        # This boundary comes first: after a committed attempt, missing cached
        # input is a recovery matter, never a needs_input re-upload request,
        # because restore_input refuses that state.
        prior = self.pipeline._attempt_tracker.list_summaries(task_id)
        if task.attempt_count:
            return ImageExecutionResult(task_id, task.status,
                                        error_code='prior_attempt_requires_recovery')
        if (request is not None and request.payload_id is not None
                and self._input_missing(task, request)):
            return self._needs_input(task.task_id, task)
        checkpoint = self.tasks.get_latest_checkpoint(task_id)
        recovered_pre_attempt = bool(checkpoint and checkpoint.reason == 'lease_recovered'
                                     and checkpoint.snapshot.total_attempts == 0)
        lease = self.tasks.claim_task(task_id, worker_id, lease_seconds=300)
        try:
            task = self.tasks.get_task(task_id)
            request = self.tasks.get_task_request(task_id)
            if request.payload_id is None:
                if image is None and prompt is None:
                    self.tasks.release_lease(lease, task.state_version, 'needs_input')
                    return ImageExecutionResult(task_id, 'needs_input', error_code='input_required')
                self.intake.attach(task_id, image=image, prompt=prompt,
                                   lease=lease, expected_state_version=task.state_version)
                task = self.tasks.get_task(task_id)
                request = self.tasks.get_task_request(task_id)
            intake_step = self.tasks.get_step(task_id, task.current_step_id) if task.current_step_id else None
            if intake_step and intake_step.operation == 'intake':
                self.tasks.save_step(task_id, replace(intake_step, status='skipped',
                                      started_at=None, completed_at=None), lease, task.state_version)
                task = self.tasks.get_task(task_id)
            step = self.tasks.get_step(task_id, task.current_step_id) if task.current_step_id else None
            if step is None:
                step = StepRecord(uuid.uuid4().hex, task_id, 1, task.task_type, 'pending',
                                  input_payload_id=request.payload_id, config_fingerprint=config_fingerprint)
                self.tasks.save_step(task_id, step, lease, task.state_version)
                task = self.tasks.get_task(task_id)
            # A durable zero-attempt counter means begin() never committed, so no
            # adapter could have been invoked by this executor. Preserve the step
            # identity and require an actual expired-lease recovery checkpoint.
            runnable_step = step.status == 'pending' or (
                step.status == 'running' and recovered_pre_attempt
                and checkpoint is not None
                and checkpoint.snapshot.active_step_id == step.step_id
                and checkpoint.snapshot.payload_id == request.payload_id
            )
            if not runnable_step or step.config_fingerprint != config_fingerprint:
                raise ValueError('step_requires_recovery')
            # The step is only marked running once an attempt actually
            # committed. A zero-attempt outcome (for example input lost after
            # the pre-run check) then leaves the step pending and genuinely
            # runnable after exact restore, instead of stranding a running
            # step that no adapter ever saw.
            if task.status == 'analyzing':
                task = self.tasks.transition(task_id, {'analyzing'}, 'running', 'state_transition',
                                             task.state_version, lease=lease)
            excluded = {item.approach_key for item in prior}
            outcome = self.pipeline.run_one_attempt(task, step.step_id, request,
                                                    config_fingerprint, excluded, lease=lease)
            task = self.tasks.get_task(task_id)
            if task.cancel_requested:
                self.tasks.release_lease(lease, task.state_version, 'cancelled', 'cancel_observed')
                return ImageExecutionResult(task_id, 'cancelled')
            if outcome.attempt is not None:
                step = self.tasks.save_step(task_id, replace(step, status='running',
                                            started_at=None, completed_at=None),
                                            lease, task.state_version)
                task = self.tasks.get_task(task_id)
            if outcome.status == 'succeeded':
                # Only the pipeline's evaluated, committed artifact can become a task result.
                self.tasks.save_step(task_id, replace(step, status='succeeded',
                    output_artifact_id=outcome.artifact.artifact_id, started_at=None, completed_at=None),
                    lease, task.state_version)
                task = self.tasks.get_task(task_id)
                task = self.tasks.transition(task_id, {'running'}, 'evaluating', 'state_transition',
                                             task.state_version, lease=lease)
                self._checkpoint(task, step, request, lease, artifact_id=outcome.artifact.artifact_id, completed=True)
                task = self.tasks.get_task(task_id)
                task = self.tasks.transition(task_id, {'evaluating'}, 'completed', 'state_transition',
                                             task.state_version, lease=lease)
                return ImageExecutionResult(task_id, task.status, task.latest_result_artifact_id)
            if outcome.attempt is None and self._input_missing(task, request):
                # Input lost between the pre-run check and a zero-attempt
                # outcome is the same recoverable absence, not a paused end.
                self.tasks.release_lease(lease, task.state_version, 'needs_input')
                return ImageExecutionResult(task_id, 'needs_input', error_code='input_required')
            self._checkpoint(task, step, request, lease)
            task = self.tasks.get_task(task_id)
            self.tasks.release_lease(lease, task.state_version, 'paused')
            return ImageExecutionResult(task_id, 'paused', error_code=outcome.error_code)
        except Exception:
            # Best effort cancellation/yield never invents success. Failed or
            # unknown writes remain visible and require reconciliation.
            task = self.tasks.get_task(task_id)
            if task and task.status in {'analyzing', 'running', 'evaluating'}:
                try:
                    self.tasks.release_lease(lease, task.state_version,
                        'cancelled' if task.cancel_requested else 'paused',
                        'cancel_observed' if task.cancel_requested else 'worker_released')
                except Exception:
                    pass
            raise

    def restore_input(self, task_id: str, *, image: bytes | None = None,
                      prompt: str | None = None):
        """Re-stage exact validated content for one zero-attempt ephemeral task.

        Surviving input is never deleted or overwritten: a surviving envelope
        keeps its bytes and original references, only missing parts are
        restored under those references, and the artifact store's
        byte-identity guard rejects any replacement of live bytes. All caller
        and survivor content checks complete before any insertion, so a
        failed, stale or expired restore cannot destroy valid input. After a
        full loss the rebuilt references are content-addressed, making a lost
        commit response safe to retry byte-identically. No reference,
        fingerprint, budget, deadline, approval or attempt is invented, and a
        changed re-upload is rejected before any queueing.
        """
        task = self.tasks.get_task(task_id)
        request = self.tasks.get_task_request(task_id)
        if (task is None or request is None or task.status != 'needs_input'
                or request.retention_policy != 'ephemeral' or task.attempt_count != 0
                or request.payload_id is None or task.cancel_requested):
            raise ValueError('ephemeral_restore_unavailable')
        canonical = canonical_image_input(request.task_type, image, prompt)
        if not hmac.compare_digest(
            self.fingerprinter.fingerprint(canonical), request.input_fingerprint
        ):
            raise ValueError('restored_input_mismatch')
        # canonical_image_input already validated both parts deterministically.
        prompt_bytes = encode_image_prompt(prompt) if prompt is not None else None
        try:
            surviving = decode_image_input(
                self.intake.artifacts.load_input(request.payload_id, task_id),
                request.task_type)
        except PayloadUnavailable:
            surviving = None
        except ImageInputError:
            # A stored envelope that no longer decodes cannot be replaced
            # safely; only the application boundary reconciles it.
            raise ValueError('ephemeral_restore_unavailable') from None
        planned: list[tuple[str, bytes]] = []
        if surviving is not None:
            # Partial loss: restore only missing parts under the surviving
            # envelope's original references; the envelope stays untouched.
            for reference, data in ((surviving.image_payload_id, image),
                                    (surviving.prompt_payload_id, prompt_bytes)):
                if reference is None:
                    continue
                try:
                    existing = self.intake.artifacts.load_input(reference, task_id)
                except PayloadUnavailable:
                    planned.append((reference, data))
                    continue
                if not hmac.compare_digest(existing, data):
                    # A live payload with different bytes is never replaced.
                    raise ValueError('restored_input_mismatch')
        else:
            # Full loss: content-addressed references make a retried restore
            # byte-identical. Orphaned parts from the original staging stay
            # retention-bound and are never referenced again.
            image_id = _content_reference(image) if image is not None else None
            prompt_id = _content_reference(prompt_bytes) if prompt_bytes is not None else None
            if image_id is not None:
                planned.append((image_id, image))
            if prompt_id is not None:
                planned.append((prompt_id, prompt_bytes))
            envelope_data = encode_image_input(
                ImageInputEnvelope(task_type=request.task_type,
                                   image_payload_id=image_id, prompt_payload_id=prompt_id))
            planned.append((request.payload_id, envelope_data))
        # A bounded cache normally evicts least-recently-used entries. Restore
        # must not evict surviving task input or unrelated task/output bytes.
        # Reserve the whole insertion set under the existing store/cache locks
        # before the first write; reject capacity/conflicts without mutation.
        artifacts = self.intake.artifacts
        with artifacts._lock, artifacts.cache._lock:
            cache = artifacts.cache
            cache._prune_locked()
            missing = []
            for reference, data in planned:
                try:
                    existing = artifacts.load_input(reference, task_id)
                except PayloadUnavailable:
                    existing = None
                if existing is not None:
                    if not hmac.compare_digest(existing, data):
                        raise ValueError('restored_input_mismatch')
                else:
                    missing.append((reference, data))
            if (len(cache._entries) + len(missing) > cache._max_entries
                    or cache._total_bytes + sum(len(data) for _, data in missing) > cache._max_bytes):
                raise ValueError('ephemeral_restore_capacity_unavailable')
            for reference, data in missing:
                artifacts.restore_transient_input(task_id, reference, data)
        from agent.adult.adult_task_restore import resume_restored_ephemeral
        return resume_restored_ephemeral(self.tasks, task_id, request.payload_id,
                                         request.input_fingerprint, task.state_version)

    def restore_input_and_run(self, task_id: str, config_fingerprint: str, *,
                              image: bytes | None = None, prompt: str | None = None,
                              worker_id='bounded-image-worker') -> ImageExecutionResult:
        """Explicit exact ephemeral re-upload and one bounded segment, never generic resume."""
        self.restore_input(task_id, image=image, prompt=prompt)
        return self.run(task_id, config_fingerprint, worker_id=worker_id)
