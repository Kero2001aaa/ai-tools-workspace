"""Internal bounded image application. No provider factory or transport."""
import base64
from agent.adult.adult_image_executor import AdultImageExecutor, ImageExecutionResult
from agent.adult.models import StepRecord, TaskBudget
from agent.adult.adult_image_supervisor import AdultImageSupervisor


class BoundedImageApplication:
    def __init__(self, executor: AdultImageExecutor, config_fingerprint: str, *, budget: TaskBudget):
        if not isinstance(executor, AdultImageExecutor):
            raise TypeError('image_executor_required')
        if not isinstance(budget, TaskBudget):
            raise TypeError('explicit_task_budget_required')
        StepRecord('config-validation', 'config-validation', 1, 'image.generate', 'pending',
                   config_fingerprint=config_fingerprint)
        self.supervisor = AdultImageSupervisor(executor)
        self.executor = executor
        self.tasks = executor.tasks
        self.artifacts = executor.intake.artifacts
        self.config_fingerprint = config_fingerprint
        self.budget = budget

    def _task(self, task_id):
        task = self.tasks.get_task(task_id)
        if task is None or task.task_type not in {'image.analyze', 'image.generate', 'image.edit'}:
            raise ValueError('image_task_unavailable')
        return task

    @staticmethod
    def _snapshot(task):
        from agent.adult.safe_task_views import task_snapshot
        return task_snapshot(task)

    def _outcome(self, outcome):
        task = self._task(outcome.task_id)
        cleanup = {'status': 'not_applicable'}
        if task.status in {'completed', 'cancelled'}:
            try:
                self.artifacts.delete_task_inputs(task.task_id)
                cleanup = {'status': 'input_removed'}
            except Exception:
                cleanup = {'status': 'cleanup_failed', 'error_code': 'input_cleanup_unavailable'}
        return {'task': self._snapshot(task), 'error_code': outcome.error_code,
                'input_cleanup': cleanup}

    def submit_image_task(self, task_type, *, image=None, prompt=None, retain_payload=False):
        if type(retain_payload) is not bool:
            raise ValueError('retention_boolean_required')
        staged = self.executor.stage(task_type, image=image, prompt=prompt, budget=self.budget,
            retention_policy='encrypted' if retain_payload else 'ephemeral')
        return self.get_task(staged.task_id)

    def get_task(self, task_id):
        return self._snapshot(self._task(task_id))

    def list_tasks(self, status=None, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        page = self.tasks.list_tasks(status, limit, cursor)
        # Dedicated composition state must not silently expose another lane.
        return {'tasks': [self.get_task(task.task_id) for task in page.items],
                'next_cursor': page.next_cursor, 'has_more': page.has_more}

    def get_attempts(self, task_id, limit=20):
        self._task(task_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        attempts = self.executor.pipeline._attempt_tracker.list_summaries(task_id)
        from agent.adult.safe_task_views import attempt_summary
        return [attempt_summary(item) for item in attempts[-limit:]]

    def get_attempt_history(self, task_id, limit=20):
        self._task(task_id)
        # Preserve legacy list-returning get_attempts; the richer internal
        # envelope keeps skipped audit records out of actual attempt counters.
        attempts = self.get_attempts(task_id, limit)
        skipped = self.executor.pipeline._attempt_tracker.list_skipped(task_id, limit)
        return {'attempts': attempts, 'skipped': list(skipped)}

    @staticmethod
    def _bounds(max_attempts, max_seconds):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError('invalid_segment_attempts')
        if type(max_seconds) is not int or not 1 <= max_seconds <= 240:
            raise ValueError('invalid_segment_seconds')

    def run_task(self, task_id, *, max_attempts=1, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        self._task(task_id)
        try:
            outcome = self.supervisor.run_until_done(task_id, self.config_fingerprint,
                max_attempts=max_attempts, max_seconds=max_seconds)
        except Exception:
            # Cancellation can fence pipeline finalization after an adapter
            # returned. Preserve durable cancellation, not a false task error.
            if self._task(task_id).status != 'cancelled':
                raise
            outcome = ImageExecutionResult(task_id, 'cancelled')
        return self._outcome(outcome)

    def restore_input_and_run(self, task_id, *, image=None, prompt=None, max_attempts=1, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        self._task(task_id)
        # An uncertain restore commit must stop before dispatch. A later
        # explicit run can reconcile queued state without replaying the restore.
        self.executor.restore_input(task_id, image=image, prompt=prompt)
        return self.run_task(task_id, max_attempts=max_attempts, max_seconds=max_seconds)

    def resume_task(self, task_id, decision, *, max_attempts=5, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        task = self._task(task_id)
        if type(decision) is not str or decision not in {'continue', 'cancel'}:
            raise ValueError('invalid_resume_decision')
        if decision == 'cancel':
            if task.status not in {'waiting_user', 'checkpointed', 'paused', 'needs_input'}:
                raise ValueError('image_resume_unavailable')
            return self.cancel_task(task_id)
        if task.status not in {'waiting_user', 'checkpointed'}:
            raise ValueError('image_resume_unavailable')
        # Internal caller must ground authenticated real approval separately.
        was_waiting = task.status == 'waiting_user'
        resumed = self.tasks.resume(task_id, 'continue')
        if resumed.status != 'queued':
            return self._outcome(ImageExecutionResult(task_id, resumed.status,
                error_code='execution_budget_exhausted'))
        if not was_waiting:
            return self.run_task(task_id, max_attempts=max_attempts, max_seconds=max_seconds)
        try:
            outcome = self.supervisor.continue_after_approval(task_id, self.config_fingerprint,
                max_attempts=max_attempts, max_seconds=max_seconds)
        except Exception:
            if self._task(task_id).status != 'cancelled':
                raise
            outcome = ImageExecutionResult(task_id, 'cancelled')
        return self._outcome(outcome)

    def cancel_task(self, task_id):
        self._task(task_id)
        task = self.tasks.request_cancel(task_id)
        return self._outcome(ImageExecutionResult(task_id, task.status))

    def get_result_chunk(self, task_id, offset=0, max_bytes=262144):
        task = self._task(task_id)
        if task.status != 'completed' or task.latest_result_artifact_id is None:
            raise ValueError('image_result_unavailable')
        chunk = self.artifacts.read_chunk(task.latest_result_artifact_id, offset, max_bytes,
                                         task_id=task.task_id)
        return {'task_id': task.task_id, 'artifact_id': chunk.artifact_id,
            'data_base64': base64.b64encode(chunk.data).decode('ascii'),
            'offset': chunk.offset, 'total_bytes': chunk.total_bytes,
            'mime_type': chunk.mime_type, 'next_offset': chunk.next_offset, 'complete': chunk.eof}
