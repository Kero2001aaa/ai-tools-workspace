"""Internal bounded text orchestration, not a public approval or provider API."""
from agent.adult.adult_text_executor import AdultTextExecutor
from agent.adult.adult_supervisor import AdultSupervisor
from agent.adult.adult_text_input import encode_text_input
from agent.adult.fixed_free_text_application import FixedFreeTextApplication, task_result_chunk
from agent.adult.models import TaskBudget


class BoundedTextApplication:
    def __init__(self, executor: AdultTextExecutor, config_fingerprint: str, *, budget: TaskBudget):
        if not isinstance(executor, AdultTextExecutor):
            raise TypeError('text_executor_required')
        if not isinstance(budget, TaskBudget):
            raise TypeError('explicit_task_budget_required')
        # Reuse the existing DTO fingerprint validator without persisting a task.
        from agent.adult.models import StepRecord
        StepRecord('config-validation', 'config-validation', 1, 'text.analyze', 'pending',
                   config_fingerprint=config_fingerprint)
        self.executor = executor
        self.tasks = executor.tasks
        self.artifacts = executor.intake.artifacts
        self.supervisor = AdultSupervisor(executor)
        self.config_fingerprint = config_fingerprint
        self.budget = budget

    @staticmethod
    def _bounds(max_attempts, max_seconds):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError('invalid_segment_attempts')
        if type(max_seconds) is not int or not 1 <= max_seconds <= 240:
            raise ValueError('invalid_segment_seconds')

    def _task(self, task_id):
        task = self.tasks.get_task(task_id)
        FixedFreeTextApplication._snapshot(task)
        return task

    def _outcome(self, outcome):
        task = self._task(outcome.task_id)
        cleanup = {'status': 'not_applicable'}
        if task.status in {'completed', 'cancelled'}:
            try:
                self.artifacts.delete_task_inputs(task.task_id)
                cleanup = {'status': 'input_removed'}
            except Exception:
                cleanup = {'status': 'cleanup_failed', 'error_code': 'input_cleanup_unavailable'}
        return {'task': FixedFreeTextApplication._snapshot(task),
                'error_code': outcome.error_code, 'input_cleanup': cleanup}

    def submit_text_task(self, task_type: str, text: str, instruction: str = '', *, retain_payload=False):
        if type(retain_payload) is not bool:
            raise ValueError('retention_boolean_required')
        data = encode_text_input(task_type, text, instruction)
        staged = self.executor.stage(task_type, data, self.budget,
                                     retention_policy='encrypted' if retain_payload else 'ephemeral')
        return self.get_task(staged.task_id)

    def get_task(self, task_id):
        return FixedFreeTextApplication._snapshot(self._task(task_id))

    def list_tasks(self, status=None, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        page = self.tasks.list_tasks(status, limit, cursor)
        return {'tasks': [self.get_task(task.task_id) for task in page.items],
                'next_cursor': page.next_cursor, 'has_more': page.has_more}

    def get_attempts(self, task_id, limit=20):
        self._task(task_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        attempts = self.executor.pipeline._attempt_tracker.list_summaries(task_id)
        from agent.adult.safe_task_views import attempt_summary
        return [attempt_summary(item) for item in attempts[-limit:]]

    def run_task(self, task_id, *, max_attempts=5, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        self._task(task_id)
        try:
            outcome = self.supervisor.run_until_done(task_id, self.config_fingerprint,
                             max_attempts=max_attempts, max_seconds=max_seconds)
        except Exception:
            # A cancellation can fence pipeline finalization after a result.
            # Return durable cancellation and cleanup, not a false task error.
            if self._task(task_id).status != 'cancelled':
                raise
            from agent.adult.adult_text_executor import TextExecutionResult
            outcome = TextExecutionResult(task_id, 'cancelled')
        return self._outcome(outcome)

    def resume_task(self, task_id, decision, *, max_attempts=5, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        task = self._task(task_id)
        if type(decision) is not str or decision not in {'continue', 'cancel'}:
            raise ValueError('invalid_resume_decision')
        if task.status not in {'waiting_user', 'checkpointed'}:
            raise ValueError('text_resume_unavailable')
        if decision == 'cancel':
            return self.cancel_task(task_id)
        # External authenticated callers must ground the real approval themselves.
        # This only persists their explicit decision and runs one bounded segment.
        was_waiting = task.status == 'waiting_user'
        resumed = self.tasks.resume(task_id, 'continue')
        if resumed.status != 'queued':
            from agent.adult.adult_text_executor import TextExecutionResult
            return self._outcome(TextExecutionResult(task_id, resumed.status,
                                                    error_code='execution_budget_exhausted'))
        run = self.supervisor.continue_after_approval if was_waiting else self.supervisor.run_until_done
        return self._outcome(run(task_id, self.config_fingerprint,
                             max_attempts=max_attempts, max_seconds=max_seconds))

    def restore_input_and_resume(self, task_id, text, instruction='', *, max_attempts=5, max_seconds=240):
        """Restore the exact zero-attempt transient envelope and run one segment."""
        import hmac
        from agent.adult.adult_task_restore import resume_restored_ephemeral
        self._bounds(max_attempts, max_seconds)
        task = self._task(task_id)
        request = self.tasks.get_task_request(task_id)
        if (task.status != 'needs_input' or request.retention_policy != 'ephemeral'
                or task.attempt_count != 0 or request.payload_id is None or task.cancel_requested):
            raise ValueError('ephemeral_restore_unavailable')
        data = encode_text_input(task.task_type, text, instruction)
        if not hmac.compare_digest(self.executor.fingerprinter.fingerprint(data), request.input_fingerprint):
            raise ValueError('restored_input_mismatch')
        self.artifacts.restore_transient_input(task_id, request.payload_id, data)
        # Any uncertain decision commit stops before execution, as in resume_task.
        resume_restored_ephemeral(self.tasks, task_id, request.payload_id,
                                  request.input_fingerprint, task.state_version)
        return self.run_task(task_id, max_attempts=max_attempts, max_seconds=max_seconds)

    def cancel_task(self, task_id):
        from agent.adult.adult_text_executor import TextExecutionResult
        self._task(task_id)
        task = self.tasks.request_cancel(task_id)
        return self._outcome(TextExecutionResult(task_id, task.status))

    def get_result_chunk(self, task_id, offset=0, max_bytes=262144):
        return task_result_chunk(self._task(task_id), self.artifacts, offset, max_bytes)
