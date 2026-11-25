"""No-create read-only general plan lifecycle projection for local monitoring."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import uuid
import time
from datetime import datetime


def _id(value):
    if str(uuid.UUID(value)) != value:
        raise ValueError("invalid_stored_id")


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("invalid_stored_timestamp")


def plan_snapshot(limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('invalid_limit')
    path = Path(os.environ.get('WORKSPACE_PLAN_DB', '.data/plans.sqlite3')).expanduser().absolute()
    if not path.exists():
        return {'available': False, 'plans': []}
    if path.is_symlink() or not path.is_file():
        raise ValueError('unsafe_plan_database')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN')
        objects = dict(db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('plans','plan_steps','tasks')"))
        if objects != {'plans': 'table', 'plan_steps': 'table', 'tasks': 'table'}:
            raise ValueError('plan_schema_unavailable')
        rows = db.execute('SELECT id,status,created_at,updated_at FROM plans ORDER BY created_at DESC,id LIMIT ?', (limit + 1,)).fetchall()
        plans = []
        for row in rows[:limit]:
            _id(row['id'])
            _timestamp(row['created_at'])
            _timestamp(row['updated_at'])
            if row['status'] not in {'queued', 'running', 'checkpointed', 'paused', 'completed'}:
                raise ValueError('invalid_plan_state')
            steps = db.execute('SELECT id,position,task_type,status FROM plan_steps WHERE plan_id=? ORDER BY position LIMIT 11', (row['id'],)).fetchall()
            if len(steps) > 10:
                raise ValueError('plan_step_bound_exceeded')
            projected = []
            for step in steps:
                _id(step['id'])
                if (step['task_type'] not in {'workspace_status', 'memory_recent', 'local_code_report'}
                        or step['status'] not in {'pending', 'running', 'succeeded', 'failed'}
                        or type(step['position']) is not int or not 1 <= step['position'] <= 10):
                    raise ValueError('invalid_plan_step')
                task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'workspace-plan-step:' + step['id']))
                task = db.execute('SELECT id,status,attempts,plan_step_id FROM tasks WHERE id=?', (task_id,)).fetchone()
                if task is not None and (task['plan_step_id'] != step['id']
                        or task['status'] not in {'pending', 'running', 'succeeded', 'failed'}
                        or type(task['attempts']) is not int or task['attempts'] < 0):
                    raise ValueError('invalid_plan_execution')
                bound = task is not None
                unknown = step['status'] == 'running' and (not bound or task['status'] == 'running')
                projected.append({'step_id': step['id'], 'position': step['position'],
                                  'task_type': step['task_type'], 'status': step['status'],
                                  'task_id': task['id'] if bound else None,
                                  'execution_status': task['status'] if bound else None,
                                  'attempts': task['attempts'] if bound else 0,
                                  'outcome_reconciliation_required': unknown})
            plans.append({**dict(row), 'steps': projected,
                          'completed_steps': sum(s['status'] == 'succeeded' for s in projected),
                          'total_steps': len(projected)})
        return {'available': True, 'plans': plans, 'has_more': len(rows) > limit,
                'read_only': True, 'execution_enabled_by_read': False}
