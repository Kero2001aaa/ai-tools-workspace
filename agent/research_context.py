"""Read-only association of quote evidence to an existing local plan-step snapshot.

Trusted local caller only. An association proves neither execution by that step
nor authorization, ownership, completion, retrieval or semantic truth.
"""
from pathlib import Path
import sqlite3
import uuid
from agent.research_evidence import build_evidence_report


def plan_evidence_envelope(db_path, plan_id, step_id, question, sources, citations):
    for identity in (plan_id, step_id):
        try:
            if type(identity) is not str or str(uuid.UUID(identity)) != identity:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError('research_context_unavailable') from None
    # Validate quotes before touching disk. Do not create a store or migrate it.
    report = build_evidence_report(question, sources, citations)
    db = None
    try:
        uri = Path(db_path).expanduser().resolve().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, timeout=1)
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: 1, 100000)
        row = db.execute('''SELECT p.status,s.status,s.task_type FROM plans p
            JOIN plan_steps s ON s.plan_id=p.id WHERE p.id=? AND s.id=?''',
            (plan_id, step_id)).fetchone()
        if row is None or len(row) != 3:
            raise ValueError('research_context_unavailable')
        if row[0] not in {'queued', 'checkpointed', 'running', 'completed', 'paused'} or row[1] not in {'pending', 'running', 'succeeded', 'failed'}:
            raise ValueError('research_context_unavailable')
        if row[2] not in {'workspace_status', 'memory_recent'}:
            raise ValueError('research_context_unavailable')
    except (sqlite3.Error, OSError, TypeError):
        raise ValueError('research_context_unavailable') from None
    finally:
        if db is not None:
            db.close()
    return {'kind': 'external_evidence_plan_context', 'plan_id': plan_id,
        'step_id': step_id, 'plan_status_snapshot': row[0],
        'step_status_snapshot': row[1], 'step_task_type': row[2],
        'association': 'trusted_local_caller_selected_context_only',
        'persisted': False, 'execution_provenance_verified': False,
        'report': report}
