"""Plan-wide five-failure ceiling over recorded general attempts, not a retry loop."""
import uuid

COLUMNS = {'decision_id', 'plan_id', 'failure_count', 'created_at'}


class ContinuationRequired(ValueError):
    """A separate explicit continuation decision is needed before retry."""


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS plan_continuations (
        decision_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
        failure_count INTEGER NOT NULL CHECK(failure_count>=5), created_at TEXT NOT NULL,
        UNIQUE(plan_id,failure_count))''')


def checkpoint(db, plan_id):
    count = db.execute('''SELECT COUNT(*) FROM task_attempts a JOIN plan_steps s
        ON s.id=a.plan_step_id WHERE s.plan_id=? AND a.status='failed' ''', (plan_id,)).fetchone()[0]
    approved = db.execute('SELECT COALESCE(MAX(failure_count),0) FROM plan_continuations WHERE plan_id=?', (plan_id,)).fetchone()[0]
    if approved > count:
        raise ValueError('continuation_history_requires_reconciliation')
    return {'recorded_failures': count, 'approved_through': approved,
            'failures_since_continue': count-approved, 'continuation_required': count-approved>=5}


def require_below_ceiling(db, plan_id):
    if checkpoint(db, plan_id)['continuation_required']:
        raise ContinuationRequired('EXPLICIT_CONTINUATION_REQUIRED: five recorded failures; retain checkpoint and wait')


def decide(db, plan_id, expected_updated_at, expected_failure_count, decision_id, now):
    # Trusted caller must obtain the owner's explicit reply. IDs are fences, not identity proof.
    if (type(expected_failure_count) is not int or expected_failure_count < 5 or
            type(expected_updated_at) is not str or not 1 <= len(expected_updated_at) <= 80 or
            type(decision_id) is not str):
        raise ValueError('invalid_continuation_decision')
    try:
        if str(uuid.UUID(decision_id)) != decision_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError('invalid_continuation_decision') from None
    plan = db.execute('SELECT status,updated_at FROM plans WHERE id=?', (plan_id,)).fetchone()
    if plan is None or plan['status'] != 'paused' or plan['updated_at'] != expected_updated_at:
        raise ValueError('stale_or_inactive_continuation_decision')
    state = checkpoint(db, plan_id)
    if not state['continuation_required'] or state['recorded_failures'] != expected_failure_count:
        raise ValueError('stale_or_unneeded_continuation_decision')
    if db.execute("SELECT 1 FROM plan_steps WHERE plan_id=? AND status='running'", (plan_id,)).fetchone():
        raise ValueError('running_outcome_requires_reconciliation')
    if db.execute("SELECT 1 FROM tasks WHERE plan_step_id IN (SELECT id FROM plan_steps WHERE plan_id=?) AND status='running'", (plan_id,)).fetchone():
        raise ValueError('running_outcome_requires_reconciliation')
    db.execute('INSERT INTO plan_continuations VALUES(?,?,?,?)', (decision_id,plan_id,expected_failure_count,now))
    db.execute('UPDATE plans SET updated_at=? WHERE id=?', (now,plan_id))
    return {'plan_id':plan_id, 'decision_id':decision_id, 'decision':'continue',
            'approved_through':expected_failure_count, 'status':'paused', 'dispatched':False,
            'updated_at':now}
