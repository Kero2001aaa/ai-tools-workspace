"""Durable SQLite queue for explicitly allowlisted workspace tasks."""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(".data") / "tasks.sqlite3"
ALLOWED_TASK_TYPES = {"workspace_status", "memory_recent"}


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat()


class TaskQueue:
    """Small local queue with atomic claims, bounded retries, and lease recovery."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path or DEFAULT_DB).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    run_at TEXT NOT NULL,
                    locked_at TEXT,
                    result TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(status, run_at, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, locked_at)")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, task_type, payload=None, *, run_at=None, max_attempts=3):
        if task_type not in ALLOWED_TASK_TYPES:
            raise ValueError(f"Unsupported task type: {task_type}")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        data = payload or {}
        if not isinstance(data, dict):
            raise ValueError("Task payload must be a JSON object")
        encoded = json.dumps(data, separators=(",", ":"), sort_keys=True)
        if len(encoded) > 4000:
            raise ValueError("Task payload must be 4,000 characters or fewer")
        now = _now()
        scheduled = run_at or now
        if scheduled.tzinfo is None:
            raise ValueError("run_at must include a timezone")
        task_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                """INSERT INTO tasks
                (id, task_type, payload, status, attempts, max_attempts, run_at,
                 created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                (task_id, task_type, encoded, max_attempts, _stamp(scheduled),
                 _stamp(now), _stamp(now)),
            )
        return self.get(task_id)

    def get(self, task_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._as_dict(row) if row else None

    def list_tasks(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def claim_next(self, *, lease_seconds=300, now=None):
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        current = now or _now()
        cutoff = _stamp(current - timedelta(seconds=lease_seconds))
        current_stamp = _stamp(current)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            stale = db.execute(
                "SELECT id, attempts, max_attempts FROM tasks "
                "WHERE status = 'running' AND locked_at < ?", (cutoff,)
            ).fetchall()
            for row in stale:
                status = "pending" if row["attempts"] < row["max_attempts"] else "failed"
                db.execute(
                    "UPDATE tasks SET status=?, locked_at=NULL, last_error=?, updated_at=? WHERE id=?",
                    (status, "Worker lease expired", current_stamp, row["id"]),
                )
            row = db.execute(
                "SELECT id FROM tasks WHERE status='pending' AND run_at<=? "
                "ORDER BY run_at, created_at LIMIT 1", (current_stamp,)
            ).fetchone()
            if row is None:
                db.commit()
                return None
            changed = db.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, locked_at=?, updated_at=? "
                "WHERE id=? AND status='pending'",
                (current_stamp, current_stamp, row["id"]),
            ).rowcount
            db.commit()
            return self.get(row["id"]) if changed else None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def complete(self, task_id, result):
        encoded = json.dumps(result, separators=(",", ":"), sort_keys=True, default=str)
        if len(encoded) > 20000:
            raise ValueError("Task result must be 20,000 characters or fewer")
        return self._transition(
            task_id, "succeeded", result=encoded, error=None, run_at=None
        )

    def fail(self, task_id, error, *, retry_delay_seconds=30):
        if not 0 <= retry_delay_seconds <= 3600:
            raise ValueError("retry_delay_seconds must be between 0 and 3600")
        task = self.get(task_id)
        if task is None or task["status"] != "running":
            raise ValueError("Only a running task can be failed")
        retry = task["attempts"] < task["max_attempts"]
        next_run = _stamp(_now() + timedelta(seconds=retry_delay_seconds)) if retry else None
        return self._transition(
            task_id, "pending" if retry else "failed",
            result=None, error=str(error)[:2000], run_at=next_run,
        )

    def _transition(self, task_id, status, *, result, error, run_at):
        now = _stamp(_now())
        with self._connect() as db:
            changed = db.execute(
                """UPDATE tasks SET status=?, result=?, last_error=?, run_at=COALESCE(?, run_at),
                   locked_at=NULL, updated_at=? WHERE id=? AND status='running'""",
                (status, result, error, run_at, now, task_id),
            ).rowcount
        if not changed:
            raise ValueError("Task does not exist or is not currently running")
        return self.get(task_id)

    @staticmethod
    def _as_dict(row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item
