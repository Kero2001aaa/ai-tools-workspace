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

    def run_task(self, task_id, *, max_attempts=5, max_seconds=240):
        self._bounds(max_attempts, max_seconds)
        self._task(task_id)
        return self._outcome(self.supervisor.run_until_done(task_id, self.config_fingerprint,
                             max_attempts=max_attempts, max_seconds=max_seconds))

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

    def cancel_task(self, task_id):
        from agent.adult.adult_text_executor import TextExecutionResult
        self._task(task_id)
        task = self.tasks.request_cancel(task_id)
        return self._outcome(TextExecutionResult(task_id, task.status))

    def get_result_chunk(self, task_id, offset=0, max_bytes=262144):
        return task_result_chunk(self._task(task_id), self.artifacts, offset, max_bytes)
