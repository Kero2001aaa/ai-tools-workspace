"""Explicit generic failure lessons over existing scoped memory.

No raw payload/artifact read, provider call, retry, installation or executor hook.
Private immutable proposals live in Adult scoped memory for this library slice.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from agent.adult.adult_attempt_tracker import AdultAttemptTracker
from agent.adult.adult_memory import AdultMemory
from agent.adult.adult_task_store import AdultTaskStore
from agent.adult.payload_store import PayloadCipher
from agent.memory_store import FeedbackPublisherStore, MemoryRef, SanitizedFeedback


# Deliberate finite mapping. Never interpolate provider errors or adapter labels.
_LESSONS = {
    'PROVIDER_TIMEOUT': ('transient_failure', 'transient',
        'A request timed out; its external outcome may be uncertain.',
        'Check the recorded outcome before considering a separate eligible approach.'),
    'PROVIDER_RATE_LIMIT': ('transient_failure', 'transient',
        'The route reported a rate limit and was unavailable for this attempt.',
        'Wait for a new availability signal; do not repeatedly probe the route.'),
    'BACKEND_UNAVAILABLE': ('transient_failure', 'transient',
        'The configured backend was unavailable for this attempt.',
        'Verify backend availability independently before planning another attempt.'),
    'CAPABILITY_UNAVAILABLE': ('capability_gap', 'capability_mismatch',
        'No verified capability was available for this attempt.',
        'Check existing verified capabilities; keep new integrations as proposals.'),
    'ROUTE_NOT_VERIFIED': ('capability_gap', 'capability_mismatch',
        'The route lacked current capability verification.',
        'Require fresh capability evidence before enabling the route.'),
    'INVALID_SCHEMA': ('compatibility_issue', 'permanent_request',
        'The request schema was invalid for this attempt.',
        'Validate the request schema before creating any new attempt.'),
    'INVALID_CONFIGURATION': ('compatibility_issue', 'permanent_request',
        'The configuration was invalid for this attempt.',
        'Validate configuration without changing cost or privacy limits.'),
    'MODEL_OUTPUT_INVALID': ('quality_issue', 'unknown',
        'The output failed deterministic validation.',
        'Keep the result unaccepted and inspect the validator contract.'),
}


@dataclass(frozen=True)
class FeedbackProposal:
    feedback_id: int


class AdultFeedback:
    """Build, validate and explicitly publish a durable generic proposal.

    Publication is separate from building. Reading a lesson grants no execution
    authority and never schedules a call. This is not a global feedback daemon.
    """

    def __init__(self, tasks: AdultTaskStore, tracker: AdultAttemptTracker,
                 publisher: FeedbackPublisherStore, adult_memory: AdultMemory,
                 fingerprinter: PayloadCipher):
        if not isinstance(tasks, AdultTaskStore) or not isinstance(tracker, AdultAttemptTracker):
            raise TypeError('Real task store and tracker required.')
        if tracker._store is not tasks:
            raise ValueError('Feedback components must share one task store.')
        if not isinstance(publisher, FeedbackPublisherStore) or not isinstance(adult_memory, AdultMemory):
            raise TypeError('Fixed feedback publisher and Adult memory scopes required.')
        if not isinstance(fingerprinter, PayloadCipher):
            raise TypeError('A keyed fingerprinter is required.')
        self.tasks, self.tracker = tasks, tracker
        self.publisher, self.memory, self.fingerprinter = publisher, adult_memory, fingerprinter

    def _feedback(self, task_id, attempt_id=None):
        task = self.tasks.get_task(task_id)
        if (task is None or task.task_type not in {'text.analyze', 'text.generate', 'text.rewrite'}
                or task.status not in {'paused', 'failed', 'completed', 'waiting_user', 'checkpointed'}):
            return None
        attempts = self.tracker.list_summaries(task_id)
        # A policy rejection anywhere must never become routing-around advice.
        if any(item.failure_class == 'policy_rejection' for item in attempts):
            return None
        candidates = [item for item in attempts if attempt_id is None or item.attempt_id == attempt_id]
        if not candidates:
            return None
        attempt = candidates[-1]
        mapping = _LESSONS.get(attempt.error_code)
        if not mapping or attempt.status not in {'failed', 'timed_out'} or attempt.failure_class != mapping[1]:
            return None
        category, _, lesson, action = mapping
        # Use a task/attempt-domain HMAC, never the input fingerprint itself.
        source = self.fingerprinter.fingerprint(
            b'adult-feedback-source-v1:' + task_id.encode('ascii') + b':' + attempt.attempt_id.encode('ascii')
        ).removeprefix('hmac-sha256:')
        clean = SanitizedFeedback(
            category=category, component_id='adult-text-pipeline', version='1',
            operation=task.task_type, failure_code=attempt.error_code.lower(),
            lesson=f'{task.task_type} {attempt.error_code.lower()}: {lesson}',
            recommended_action=action, verification_status='observed', source_fingerprint=source,
        )
        return attempt.attempt_id, self.publisher._validate_feedback(clean)

    @staticmethod
    def _encode(attempt_id, feedback):
        return json.dumps({'format': 'adult-feedback-proposal-v1', 'attempt_id': attempt_id,
                           'feedback': asdict(feedback)}, sort_keys=True, separators=(',', ':'))

    def build_proposal(self, task_id: str) -> FeedbackProposal | None:
        result = self._feedback(task_id)
        if result is None:
            return None
        attempt_id, feedback = result
        ref = self.memory.remember_private(self._encode(attempt_id, feedback), 'lesson', task_id,
                                           {'source': 'adult-feedback-proposal-v1'})
        return FeedbackProposal(ref.id)

    def sanitize(self, proposal: FeedbackProposal) -> SanitizedFeedback:
        if not isinstance(proposal, FeedbackProposal):
            raise TypeError('A durable FeedbackProposal is required.')
        record = self.memory.get_private(proposal.feedback_id)
        if (record is None or record.memory_type != 'lesson' or record.task_id is None
                or record.metadata != {'source': 'adult-feedback-proposal-v1'}):
            raise ValueError('feedback_proposal_unavailable')
        try:
            stored = json.loads(record.content)
            result = self._feedback(record.task_id, stored['attempt_id'])
            if result is None or record.content != self._encode(*result):
                raise ValueError()
            return result[1]
        except (KeyError, TypeError, ValueError):
            raise ValueError('feedback_proposal_invalid') from None

    def publish(self, feedback_id: int) -> MemoryRef:
        clean = self.sanitize(FeedbackProposal(feedback_id))
        return self.publisher.publish_sanitized_feedback(clean, idempotent=True)
