"""No-create standalone queue monitoring. No migrations, claims or dispatch."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from agent.task_queue import DEFAULT_DB, TaskQueue


def task_snapshot(limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_task_limit')
    path=Path(DEFAULT_DB).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('unsafe_task_database')
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError('unsafe_task_database')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        deadline=time.monotonic()+2
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        row=db.execute("SELECT type FROM sqlite_master WHERE name='tasks'").fetchone()
        if row is None or row[0]!='table':
            raise ValueError('task_schema_unavailable')
        rows=db.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?',(limit,)).fetchall()
        return [TaskQueue._as_dict(row) for row in rows]
