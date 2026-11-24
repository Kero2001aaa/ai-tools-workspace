"""One bounded text journey over existing stores and the one-attempt pipeline.

No scheduler, network client, registry auto-certification or retry loop. Callers
supply an already configured pipeline. Uncertain prior attempts are never replayed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import uuid
import time

from agent.adult.adult_task_store import AdultTaskStore, TaskBudgetExhausted
from agent.adult.adult_text_intake import AdultTextIntake
from agent.adult.adult_text_pipeline import AdultTextPipeline
from agent.adult.models import AdultTaskRequest, CheckpointSnapshot, StepRecord, TaskBudget
from agent.adult.payload_store import PayloadCipher


@dataclass(frozen=True)
class TextExecutionResult:
    task_id: str
    status: str
    artifact_id: str | None = None
    error_code: str | None = None


class AdultTextExecutor:
    """Own one lease and one invocation at most, never implicit approval/retry."""

    def __init__(self, tasks: AdultTaskStore, intake: AdultTextIntake,
                 pipeline: AdultTextPipeline, fingerprinter: PayloadCipher):
        if not isinstance(tasks, AdultTaskStore) or not isinstance(intake, AdultTextIntake):
            raise TypeError('Real task store and intake are required.')
        if not isinstance(pipeline, AdultTextPipeline) or not isinstance(fingerprinter, PayloadCipher):
            raise TypeError('Real pipeline and fingerprinter are required.')
        if intake.tasks is not tasks or pipeline._task_store is not tasks:
            raise ValueError('Executor components must share one task store.')
        if intake.artifacts is not pipeline._artifact_store:
            raise ValueError('Executor components must share one artifact store.')
        if intake.fingerprinter.key_id != fingerprinter.key_id:
            raise ValueError('Executor fingerprint keys must match.')
        self.tasks, self.intake, self.pipeline, self.fingerprinter = tasks, intake, pipeline, fingerprinter

    def create(self, task_type: str, canonical_input: bytes, budget: TaskBudget | None = None, *,
               retention_policy: str = "ephemeral"):
        # Validate before creating a durable task; private text never enters its DTO.
        from agent.adult.adult_text_input import decode_text_input
        decode_text_input(canonical_input, task_type)
        if retention_policy == "encrypted":
            configured = PayloadCipher.from_environment()
            if configured.key_id != self.fingerprinter.key_id:
                raise ValueError("configured_key_mismatch")
            if (self.intake.artifacts.cipher is None
                    or self.intake.artifacts.cipher.key_id != configured.key_id):
                raise ValueError("artifact_key_mismatch")
        return self.tasks.create_task(
            AdultTaskRequest(task_type, self.fingerprinter.fingerprint(canonical_input),
                             retention_policy=retention_policy),
            budget if budget is not None else TaskBudget(),
        )

    def stage_encrypted(self, task_type: str, canonical_input: bytes,
                        budget: TaskBudget | None = None, *, worker_id="text-intake-worker") -> TextExecutionResult:
        """Persist input/checkpoint and yield without selecting or invoking a model.

        Requires the caller's configured key. No generated persistent secret,
        replacement input or implicit resume. The existing wall deadline starts
        at creation and is not reset by staging or reopening.
        """
        task = self.create(task_type, canonical_input, budget, retention_policy="encrypted")
        lease = self.tasks.claim_task(task.task_id, worker_id, lease_seconds=300)
        try:
            task = self.tasks.get_task(task.task_id)
            self.intake.attach(task.task_id, canonical_input, lease, task.state_version)
            task = self.tasks.get_task(task.task_id)
            request = self.tasks.get_task_request(task.task_id)
            step = self.tasks.get_step(task.task_id, task.current_step_id)
            self._checkpoint(task, step, request, lease)
            task = self.tasks.get_task(task.task_id)
            self.tasks.release_lease(lease, task.state_version, "checkpointed", "yielded")
            return TextExecutionResult(task.task_id, "checkpointed")
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

    def run(self, task_id: str, canonical_input: bytes | None,
            config_fingerprint: str, *, worker_id='bounded-text-worker') -> TextExecutionResult:
        return self._execute(task_id, canonical_input, config_fingerprint, worker_id=worker_id)

    def _execute(self, task_id, canonical_input, config_fingerprint, *, worker_id,
                 supervised=False, max_attempts=1, max_seconds=300):
        started = time.monotonic()
        task = self.tasks.get_task(task_id)
        if task is None:
            raise ValueError('task_unavailable')
        if task.status in {'completed', 'failed', 'cancelled'}:
            return TextExecutionResult(task_id, task.status, task.latest_result_artifact_id)
        if task.status not in {'queued', 'checkpointed'}:
            return TextExecutionResult(task_id, task.status, error_code='task_not_runnable')
        # Unknown, successful or nonretryable prior attempts are never replayed.
        # Supervision may continue only known terminal retryable failures.
        prior = self.pipeline._attempt_tracker.list_summaries(task_id)
        safe_prior = (supervised and bool(prior) and len(prior) == task.attempt_count
                      and all(item.status in {'failed', 'timed_out'} and item.retryable
                              and item.failure_class not in {'policy_rejection', 'budget_exhaustion'}
                              for item in prior))
        if task.attempt_count and not safe_prior:
            return TextExecutionResult(task_id, task.status, error_code='prior_attempt_requires_recovery')
        checkpoint = self.tasks.get_latest_checkpoint(task_id)
        recovered_pre_attempt = bool(checkpoint and checkpoint.reason == 'lease_recovered'
                                     and checkpoint.snapshot.total_attempts == 0)
        try:
            lease = self.tasks.claim_task(task_id, worker_id, lease_seconds=300)
        except TaskBudgetExhausted:
            if not supervised:
                raise
            return TextExecutionResult(task_id, self.tasks.get_task(task_id).status,
                                       error_code='execution_budget_exhausted')
        try:
            task = self.tasks.get_task(task_id)
            request = self.tasks.get_task_request(task_id)
            if request.payload_id is None:
                if canonical_input is None:
                    self.tasks.release_lease(lease, task.state_version, 'needs_input')
                    return TextExecutionResult(task_id, 'needs_input', error_code='input_required')
                self.intake.attach(task_id, canonical_input, lease, task.state_version)
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
                step.status == 'running' and (recovered_pre_attempt or (supervised and safe_prior))
                and checkpoint is not None
                and checkpoint.snapshot.active_step_id == step.step_id
                and checkpoint.snapshot.payload_id == request.payload_id
            )
            if not runnable_step or step.config_fingerprint != config_fingerprint:
                raise ValueError('step_requires_recovery')
            step = self.tasks.save_step(task_id, replace(step, status='running', started_at=None,
                                         completed_at=None), lease, task.state_version)
            task = self.tasks.get_task(task_id)
            if task.status == 'analyzing':
                task = self.tasks.transition(task_id, {'analyzing'}, 'running', 'state_transition',
                                             task.state_version, lease=lease)
            excluded = {item.approach_key for item in prior}
            for segment_index in range(max_attempts):
                if (supervised and task.failed_attempts_since_approval == 5
                        and task.max_provider_calls > 0 and task.provider_calls >= task.max_provider_calls):
                    self._checkpoint(task, step, request, lease)
                    task = self.tasks.get_task(task_id)
                    self.tasks.transition(task_id, {'running'}, 'failed', 'budget_exhausted',
                                          task.state_version, lease=lease)
                    return TextExecutionResult(task_id, 'failed', error_code='execution_budget_exhausted')
                if supervised and task.failed_attempts_since_approval == 5:
                    try:
                        self._checkpoint(task, step, request, lease, waiting=True)
                    except TaskBudgetExhausted:
                        pass
                    current = self.tasks.get_task(task_id)
                    return TextExecutionResult(task_id, current.status, error_code='approval_required')
                outcome = self.pipeline.run_one_attempt(task, step.step_id, request, config_fingerprint, excluded, lease=lease)
                task = self.tasks.get_task(task_id)
                if task.cancel_requested:
                    self.tasks.release_lease(lease, task.state_version, 'cancelled', 'cancel_observed')
                    return TextExecutionResult(task_id, 'cancelled')
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
                    return TextExecutionResult(task_id, task.status, task.latest_result_artifact_id)
                if not supervised:
                    self._checkpoint(task, step, request, lease)
                    task = self.tasks.get_task(task_id)
                    self.tasks.release_lease(lease, task.state_version, 'paused')
                    return TextExecutionResult(task_id, 'paused', error_code=outcome.error_code)
                attempts = self.pipeline._attempt_tracker.list_summaries(task_id)
                excluded.update(item.approach_key for item in attempts)
                if (task.failed_attempts_since_approval == 5 and task.max_provider_calls > 0
                        and task.provider_calls >= task.max_provider_calls):
                    self._checkpoint(task, step, request, lease)
                    task = self.tasks.get_task(task_id)
                    self.tasks.transition(task_id, {'running'}, 'failed', 'budget_exhausted',
                                          task.state_version, lease=lease)
                    return TextExecutionResult(task_id, 'failed', error_code=outcome.error_code)
                if task.failed_attempts_since_approval == 5:
                    try:
                        self._checkpoint(task, step, request, lease, waiting=True)
                    except TaskBudgetExhausted:
                        pass
                    current = self.tasks.get_task(task_id)
                    return TextExecutionResult(task_id, current.status, error_code=outcome.error_code)
                if outcome.status == 'rejected':
                    self.tasks.transition(task_id, {'running'}, 'failed', 'state_transition',
                                          task.state_version, lease=lease)
                    return TextExecutionResult(task_id, 'failed', error_code=outcome.error_code)
                retryable = (outcome.attempt is not None and outcome.attempt.retryable
                             and outcome.attempt.status in {'failed', 'timed_out'})
                self._checkpoint(task, step, request, lease)
                task = self.tasks.get_task(task_id)
                exhausted = (task.attempt_count >= task.max_total_attempts
                    or datetime.now(timezone.utc) >= datetime.fromisoformat(task.deadline_at.replace('Z', '+00:00'))
                    or task.provider_calls >= task.max_provider_calls and task.max_provider_calls > 0)
                if exhausted:
                    self.tasks.transition(task_id, {'running'}, 'failed', 'budget_exhausted',
                                          task.state_version, lease=lease)
                    return TextExecutionResult(task_id, 'failed', error_code=outcome.error_code)
                if not retryable:
                    self.tasks.release_lease(lease, task.state_version, 'paused')
                    return TextExecutionResult(task_id, 'paused', error_code=outcome.error_code)
                if segment_index + 1 >= max_attempts or time.monotonic() - started >= max_seconds:
                    self.tasks.release_lease(lease, task.state_version, 'checkpointed', 'yielded')
                    return TextExecutionResult(task_id, 'checkpointed', error_code=outcome.error_code)
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
