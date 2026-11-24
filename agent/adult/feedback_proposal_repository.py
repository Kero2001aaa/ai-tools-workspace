"""Private feedback proposal and receipt repository in the operational store."""
from dataclasses import asdict
import json
import uuid

from agent.adult.adult_task_store import AdultTaskStore, _utc_now
from agent.memory_store import SanitizedFeedback


class FeedbackProposalRepository:
    def __init__(self, tasks: AdultTaskStore):
        if not isinstance(tasks, AdultTaskStore):
            raise TypeError('task_store_required')
        self.tasks = tasks

    def ensure(self, private_memory_id: int, task_id: str, feedback: SanitizedFeedback):
        if type(private_memory_id) is not int or private_memory_id < 1:
            raise ValueError('private_proposal_id_invalid')
        if not isinstance(feedback, SanitizedFeedback):
            raise TypeError('sanitized_feedback_required')
        encoded = json.dumps(asdict(feedback), sort_keys=True, separators=(',', ':'))
        with self.tasks._transaction() as db:
            existing = db.execute('SELECT * FROM adult_feedback_proposals WHERE private_memory_id=?',
                                  (private_memory_id,)).fetchone()
            if existing is not None:
                if (existing['task_id'] != task_id or existing['feedback_type'] != feedback.category
                    or existing['sanitized_summary'] != feedback.lesson
                    or existing['recommendation_json'] != encoded
                    or existing['source_fingerprint'] != feedback.source_fingerprint):
                    raise ValueError('operational_proposal_mismatch')
                return existing['feedback_id']
            feedback_id = str(uuid.uuid4())
            db.execute('''INSERT INTO adult_feedback_proposals
                (feedback_id,task_id,feedback_type,sanitized_summary,recommendation_json,status,
                 source_fingerprint,private_memory_id,created_at)
                VALUES(?,?,?,?,?,'proposed',?,?,?)''',
                (feedback_id,task_id,feedback.category,feedback.lesson,encoded,
                 feedback.source_fingerprint,private_memory_id,_utc_now()))
            return feedback_id

    def record_publication(self, private_memory_id: int, published_memory_id: int):
        if type(published_memory_id) is not int or published_memory_id < 1:
            raise ValueError('publication_id_invalid')
        with self.tasks._transaction() as db:
            row = db.execute('SELECT * FROM adult_feedback_proposals WHERE private_memory_id=?',
                             (private_memory_id,)).fetchone()
            if row is None:
                raise ValueError('operational_proposal_unavailable')
            if row['status'] == 'published':
                if row['published_memory_id'] != published_memory_id:
                    raise ValueError('publication_receipt_mismatch')
                return
            db.execute("UPDATE adult_feedback_proposals SET status='published',published_memory_id=?,published_at=? WHERE private_memory_id=? AND status='proposed'",
                       (published_memory_id,_utc_now(),private_memory_id))

    def status(self, private_memory_id: int):
        if type(private_memory_id) is not int or private_memory_id < 1:
            raise ValueError('private_proposal_id_invalid')
        with self.tasks._connect() as db:
            row = db.execute('SELECT feedback_id,status,private_memory_id,published_memory_id,created_at,published_at FROM adult_feedback_proposals WHERE private_memory_id=?',
                             (private_memory_id,)).fetchone()
        return dict(row) if row is not None else None
