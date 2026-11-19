"""Persistent, bounded plans for explicitly allowlisted read-only workspace tasks."""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.task_queue import ALLOWED_TASK_TYPES

DEFAULT_DB = Path(".data") / "plans.sqlite3"
MAX_STEPS = 10
MAX_TITLE = 120
MAX_DESCRIPTION = 240
LEASE_MINUTES = 10


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def _validate_step(step):
    if not isinstance(step, dict) or set(step) - {"task_type", "payload", "description"}:
        raise ValueError("Each step must contain only task_type, payload, and description")
    task_type = step.get("task_type")
    if task_type not in ALLOWED_TASK_TYPES:
        raise ValueError(f"Unsupported plan task type: {task_type}")
    payload = step.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("Step payload must be a JSON object")
    if task_type == "workspace_status" and payload:
        raise ValueError("workspace_status does not accept a payload")
    if task_type == "memory_recent":
        limit = payload.get("limit", 5)
        if set(payload) - {"limit"} or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("memory_recent accepts only an integer limit from 1 to 20")
    description = step.get("description", task_type)
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise ValueError(f"Step description must be 1 to {MAX_DESCRIPTION} characters")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(encoded) > 4000:
        raise ValueError("Step payload exceeds 4,000 characters")
    return task_type, payload, description.strip()


class PlanStore:
    """SQLite plan store with atomic one-step claims and explicit failed-step retry."""

    def __init__(self, db_path=None):
        configured = os.getenv("WORKSPACE_PLAN_DB", "").strip()
        self.db_path = Path(db_path or configured or DEFAULT_DB).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS plan_steps (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    task_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    last_error TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(plan_id, position)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_plan_steps_status ON plan_steps(plan_id, status, position)")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_plan(self, title, steps):
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise ValueError(f"Plan title must be 1 to {MAX_TITLE} characters")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError(f"A plan must contain 1 to {MAX_STEPS} steps")
        validated = [_validate_step(step) for step in steps]
        now = _stamp(_now())
        plan_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO plans(id,title,status,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                (plan_id, title.strip(), now, now),
            )
            for position, (task_type, payload, description) in enumerate(validated, start=1):
                db.execute(
                    """INSERT INTO plan_steps
                    (id,plan_id,position,task_type,payload,description,status)
                    VALUES(?,?,?,?,?,?,'pending')""",
                    (str(uuid.uuid4()), plan_id, position, task_type,
                     json.dumps(payload, separators=(",", ":"), sort_keys=True), description),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_plan(plan_id)

    def get_plan(self, plan_id):
        with self._connect() as db:
            plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                return None
            steps = db.execute(
                "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY position", (plan_id,)
            ).fetchall()
        return {
            **dict(plan),
            "steps": [
                {**dict(step), "payload": json.loads(step["payload"]),
                 "result": json.loads(step["result"]) if step["result"] else None}
                for step in steps
            ],
        }

    def list_plans(self, limit=20):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,title,status,created_at,updated_at FROM plans ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_next_step(self, plan_id):
        """Execute exactly one next step; a stale running step is recovered after its lease."""
        now = _now()
        now_stamp = _stamp(now)
        stale_before = _stamp(now - timedelta(minutes=LEASE_MINUTES))
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not plan:
                raise ValueError("Plan not found")
            db.execute(
                """UPDATE plan_steps SET status='pending',started_at=NULL
                WHERE plan_id=? AND status='running' AND started_at<?""",
                (plan_id, stale_before),
            )
            failed_count = db.execute(
                "SELECT COUNT(*) FROM plan_steps WHERE plan_id=? AND status='failed'", (plan_id,)
            ).fetchone()[0]
            if failed_count:
                db.execute("UPDATE plans SET status='paused',updated_at=? WHERE id=?", (now_stamp, plan_id))
                db.commit()
                return {"status": "paused", "plan_id": plan_id, "step": None,
                        "reason": "A failed step must be explicitly retried before continuing"}
            running_count = db.execute(
                "SELECT COUNT(*) FROM plan_steps WHERE plan_id=? AND status='running'", (plan_id,)
            ).fetchone()[0]
            if running_count:
                db.execute("UPDATE plans SET status='running',updated_at=? WHERE id=?", (now_stamp, plan_id))
                db.commit()
                return {"status": "running", "plan_id": plan_id, "step": None,
                        "reason": "Another step is already running"}
            step = db.execute(
                "SELECT * FROM plan_steps WHERE plan_id=? AND status='pending' ORDER BY position LIMIT 1",
                (plan_id,),
            ).fetchone()
            if step is None:
                failed = db.execute(
                    "SELECT COUNT(*) FROM plan_steps WHERE plan_id=? AND status='failed'", (plan_id,)
                ).fetchone()[0]
                pending = db.execute(
                    "SELECT COUNT(*) FROM plan_steps WHERE plan_id=? AND status IN ('pending','running')",
                    (plan_id,),
                ).fetchone()[0]
                status = "paused" if failed else ("completed" if pending == 0 else "running")
                db.execute("UPDATE plans SET status=?,updated_at=? WHERE id=?", (status, now_stamp, plan_id))
                db.commit()
                return {"status": status, "plan_id": plan_id, "step": None}
            db.execute(
                "UPDATE plan_steps SET status='running',started_at=? WHERE id=? AND status='pending'",
                (now_stamp, step["id"]),
            )
            db.execute("UPDATE plans SET status='running',updated_at=? WHERE id=?", (now_stamp, plan_id))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        from agent.task_worker import execute_task
        task = {"task_type": step["task_type"], "payload": json.loads(step["payload"])}
        try:
            result = execute_task(task)
            encoded = json.dumps(result, separators=(",", ":"), sort_keys=True, default=str)
            if len(encoded) > 20000:
                raise ValueError("Plan step result exceeds 20,000 characters")
        except Exception as exc:
            with self._connect() as db:
                db.execute(
                    "UPDATE plan_steps SET status='failed',last_error=?,completed_at=? WHERE id=? AND status='running'",
                    (str(exc)[:2000], _stamp(_now()), step["id"]),
                )
                db.execute("UPDATE plans SET status='paused',updated_at=? WHERE id=?", (_stamp(_now()), plan_id))
            return {"status": "paused", "plan_id": plan_id, "step_id": step["id"], "error": str(exc)}

        with self._connect() as db:
            db.execute(
                "UPDATE plan_steps SET status='succeeded',result=?,last_error=NULL,completed_at=? WHERE id=? AND status='running'",
                (encoded, _stamp(_now()), step["id"]),
            )
            remaining = db.execute(
                "SELECT COUNT(*) FROM plan_steps WHERE plan_id=? AND status!='succeeded'", (plan_id,)
            ).fetchone()[0]
            status = "completed" if remaining == 0 else "queued"
            db.execute("UPDATE plans SET status=?,updated_at=? WHERE id=?", (status, _stamp(_now()), plan_id))
        return {"status": "step_succeeded", "plan_status": status, "plan_id": plan_id,
                "step_id": step["id"], "position": step["position"], "result": result}

    def run_until_done(self, plan_id, max_steps=3, max_seconds=60):
        """Run a bounded batch, checkpointing after every step for later resume."""
        import time

        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 10:
            raise ValueError("max_steps must be an integer from 1 to 10")
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or not 1 <= max_seconds <= 300:
            raise ValueError("max_seconds must be an integer from 1 to 300")
        if self.get_plan(plan_id) is None:
            raise ValueError("Plan not found")

        started = time.monotonic()
        executed = []
        while len(executed) < max_steps:
            if time.monotonic() - started >= max_seconds:
                break
            outcome = self.run_next_step(plan_id)
            if outcome.get("status") == "step_succeeded":
                executed.append({
                    "step_id": outcome["step_id"],
                    "position": outcome["position"],
                    "status": "succeeded",
                })
                if outcome.get("plan_status") == "completed":
                    return {
                        "status": "completed",
                        "plan_id": plan_id,
                        "steps_executed": executed,
                        "remaining_steps": 0,
                    }
                continue
            if outcome.get("status") in {"completed", "paused", "running"}:
                current = self.get_plan(plan_id)
                remaining = sum(
                    step["status"] in {"pending", "running"}
                    for step in current["steps"]
                )
                return {
                    "status": outcome["status"],
                    "plan_id": plan_id,
                    "steps_executed": executed,
                    "remaining_steps": remaining,
                    "reason": outcome.get("reason") or outcome.get("error"),
                }

        current = self.get_plan(plan_id)
        remaining = sum(
            step["status"] in {"pending", "running"}
            for step in current["steps"]
        )
        final_status = "completed" if current["status"] == "completed" else "checkpointed"
        return {
            "status": final_status,
            "plan_id": plan_id,
            "plan_status": current["status"],
            "steps_executed": executed,
            "remaining_steps": remaining,
            "reason": "Per-run step or time budget reached; invoke again to resume" if remaining else None,
        }

    def retry_failed_step(self, plan_id, step_id):
        now = _stamp(_now())
        with self._connect() as db:
            changed = db.execute(
                """UPDATE plan_steps SET status='pending',last_error=NULL,result=NULL,
                started_at=NULL,completed_at=NULL WHERE id=? AND plan_id=? AND status='failed'""",
                (step_id, plan_id),
            ).rowcount
            if not changed:
                raise ValueError("No failed step with that ID exists in this plan")
            db.execute("UPDATE plans SET status='queued',updated_at=? WHERE id=?", (now, plan_id))
        return self.get_plan(plan_id)
