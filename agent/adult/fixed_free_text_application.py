"""Text-only application boundary over a fixed injected free service.

Not the full AdultAgent contract: image submission and approval resume
are deliberately not exposed. No transport registration or background execution.
"""
from __future__ import annotations

import base64

from agent.adult.fixed_free_text_service import FixedFreeTextService
from agent.adult.adult_feedback import AdultFeedback
from agent.adult.models import TaskBudget


class FixedFreeTextApplication:
    def __init__(self, service: FixedFreeTextService, *, budget: TaskBudget, feedback: AdultFeedback | None = None):
        if not isinstance(service, FixedFreeTextService):
            raise TypeError('fixed_free_service_required')
        if not isinstance(budget, TaskBudget) or budget.max_provider_calls < 1:
            raise ValueError('explicit_provider_budget_required')
        if feedback is not None and (not isinstance(feedback, AdultFeedback)
                or feedback.tasks is not service.tasks
                or feedback.fingerprinter.key_id != service.cipher.key_id):
            raise ValueError('matching_feedback_components_required')
        self._service = service
        self._budget = budget
        self._feedback = feedback

    @staticmethod
    def _snapshot(task):
        if task is None or task.task_type not in {'text.analyze', 'text.generate', 'text.rewrite'}:
            raise ValueError('text_task_unavailable')
        return {'task_id': task.task_id, 'task_type': task.task_type, 'status': task.status,
            'created_at': task.created_at, 'updated_at': task.updated_at,
            'deadline_at': task.deadline_at, 'attempt_count': task.attempt_count,
            'provider_calls': task.provider_calls, 'current_step_id': task.current_step_id,
            'selected_adapter': task.selected_adapter, 'selected_provider': task.selected_provider,
            'selected_model': task.selected_model, 'selected_tool': task.selected_tool,
            'latest_result_artifact_id': task.latest_result_artifact_id,
            'last_error_code': task.last_error_code, 'user_action_required': task.user_action_required}

    def _task(self, task_id):
        task = self._service.tasks.get_task(task_id)
        self._snapshot(task)
        return task

    def submit_text_task(self, task_type: str, text: str, instruction: str = '', *, retain_payload=False):
        if not isinstance(retain_payload, bool):
            raise ValueError('retention_boolean_required')
        outcome = self._service.submit(task_type, text, instruction, budget=self._budget,
                                       retain_payload=retain_payload)
        return self.get_task(outcome.task_id)

    def run_task(self, task_id: str, *, max_seconds: int = 120):
        before = self._task(task_id)
        outcome = self._service.run(task_id, max_attempts=1, max_seconds=max_seconds)
        after = self._task(task_id)
        result = {'task': self._snapshot(after), 'error_code': outcome.error_code}
        if self._feedback is not None:
            result['feedback'] = {'status': 'not_applicable'}
            # Only a new, durably finished attempt can trigger private proposal
            # preparation. Terminal reruns and zero-call pauses never rebuild it.
            if after.attempt_count > before.attempt_count:
                try:
                    proposal = self._feedback.build_proposal(task_id)
                    if proposal is not None:
                        result['feedback'] = {'status': 'private_proposal_created',
                                              'private_proposal_id': proposal.feedback_id}
                except Exception:
                    # Task/result remain authoritative even if the separate
                    # private feedback write failed. Never hide or replay work.
                    result['feedback'] = {'status': 'proposal_failed',
                                          'error_code': 'private_feedback_unavailable'}
        return result

    def get_task(self, task_id: str):
        return self._snapshot(self._task(task_id))

    def list_tasks(self, status: str | None = None, limit: int = 20, cursor: str | None = None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        page = self._service.tasks.list_tasks(status, limit, cursor)
        # A dedicated application state directory must be used. Fail closed on
        # another task type, rather than leaking or silently skipping records.
        return {'tasks': [self._snapshot(task) for task in page.items],
                'next_cursor': page.next_cursor, 'has_more': page.has_more}

    def get_attempts(self, task_id: str, limit: int = 20):
        self._task(task_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError('invalid_page_limit')
        attempts = self._service.tracker.list_summaries(task_id)
        # No input/config/approach fingerprints, raw errors or metadata blobs.
        return [{'attempt_id': item.attempt_id, 'attempt_no': item.attempt_no,
                 'adapter_id': item.adapter_id, 'adapter_version': item.adapter_version,
                 'provider_id': item.provider_id, 'model_id': item.model_id,
                 'status': item.status, 'elapsed_ms': item.elapsed_ms,
                 'error_code': item.error_code, 'cost_microusd': item.cost_microusd}
                for item in attempts[-limit:]]

    def cancel_task(self, task_id: str):
        self._task(task_id)
        return self._snapshot(self._service.tasks.request_cancel(task_id))

    def get_result_chunk(self, task_id: str, offset: int = 0, max_bytes: int = 262144):
        return task_result_chunk(self._task(task_id), self._service.artifacts, offset, max_bytes)


def task_result_chunk(task, artifacts, offset=0, max_bytes=262144):
    """Shared task-owned read without requiring provider configuration or a call."""
    from agent.adult.payload_store import AdultArtifactStore
    FixedFreeTextApplication._snapshot(task)
    if not isinstance(artifacts, AdultArtifactStore):
        raise TypeError('artifact_store_required')
    if task.status != 'completed' or task.latest_result_artifact_id is None:
        raise ValueError('text_result_unavailable')
    # The caller names a task, never a path or arbitrary artifact reference.
    chunk = artifacts.read_chunk(task.latest_result_artifact_id, offset, max_bytes, task_id=task.task_id)
    return {'task_id': task.task_id, 'artifact_id': chunk.artifact_id,
            'data_base64': base64.b64encode(chunk.data).decode('ascii'),
            'offset': chunk.offset, 'total_bytes': chunk.total_bytes,
            'mime_type': chunk.mime_type, 'next_offset': chunk.next_offset, 'complete': chunk.eof}
