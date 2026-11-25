"""Explicit existing-plan cancellation, no database creation or migration."""
import os
from pathlib import Path
import sqlite3
import uuid
from agent.plan_store import PlanStore


class _ExistingCancelStore(PlanStore):
    """Only inherited cancel/get methods are used, neither needs a queue object."""
    def __init__(self, path):
        self.db_path=path

    def _connect(self):
        db=sqlite3.connect(self.db_path.as_uri()+'?mode=rw',uri=True,timeout=2,isolation_level=None)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        try:
            from agent.plan_recovery import TABLES, COLUMNS
            objects=db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if {row[0] for row in objects if row[1]=='table'}!=TABLES or any(row[1] not in {'table','index'} for row in objects):
                raise ValueError('plan_schema_unavailable')
            for table in TABLES:
                if {row[1] for row in db.execute('PRAGMA table_info('+table+')')}!=COLUMNS[table]:
                    raise ValueError('plan_schema_unavailable')
        except Exception:
            db.close()
            raise
        return db


def cancel_existing_plan(plan_id, expected_updated_at):
    try:
        if type(plan_id) is not str or str(uuid.UUID(plan_id))!=plan_id:
            raise ValueError()
        if type(expected_updated_at) is not str or not 1<=len(expected_updated_at)<=80:
            raise ValueError()
    except (ValueError,AttributeError):
        raise ValueError('invalid_plan_cancel_request') from None
    path=Path(os.environ.get('WORKSPACE_PLAN_DB','.data/plans.sqlite3')).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('plan_cancellation_unavailable')
    result=_ExistingCancelStore(path).cancel_plan(plan_id,expected_updated_at)
    # The inherited transaction returns the full private plan. HTTP only needs
    # its lifecycle receipt, never payloads/results/title/source paths.
    return {'plan_id':result['id'],'status':result['status'],'updated_at':result['updated_at']}
