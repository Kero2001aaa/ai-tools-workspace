"""Bounded text supervision over the existing fenced executor, never background."""
from agent.adult.adult_text_executor import AdultTextExecutor


class AdultSupervisor:
    def __init__(self, executor: AdultTextExecutor):
        if not isinstance(executor, AdultTextExecutor):
            raise TypeError('A real bounded text executor is required.')
        self.executor = executor

    def run_until_done(self, task_id: str, config_fingerprint: str, *,
                       canonical_input: bytes | None = None,
                       max_attempts: int = 5, max_seconds: int = 240,
                       worker_id='bounded-supervisor'):
        if (not isinstance(max_attempts, int) or isinstance(max_attempts, bool)
                or not 1 <= max_attempts <= 5):
            raise ValueError('max_attempts must be between1and5.')
        if (not isinstance(max_seconds, int) or isinstance(max_seconds, bool)
                or not 1 <= max_seconds <= 240):
            raise ValueError('max_seconds must be between1and240.')
        return self.executor._execute(task_id, canonical_input, config_fingerprint,
            worker_id=worker_id, supervised=True, max_attempts=max_attempts, max_seconds=max_seconds)

    def continue_after_approval(self, task_id: str, config_fingerprint: str, **kwargs):
        # A caller must already have persisted its authenticated continue decision
        # via the existing resume API. This method never invents that decision.
        task = self.executor.tasks.get_task(task_id)
        if task is None or task.status != 'queued' or task.approvals_granted < 1:
            raise ValueError('explicit_persisted_approval_required')
        return self.run_until_done(task_id, config_fingerprint, **kwargs)
