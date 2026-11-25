"""Private, no-overwrite SQLite snapshots for the general plan/queue store only.

Copies through SQLite's backup API, not file bytes, so committed WAL content is
included. Restore never starts workers, changes task states or replays work.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

MAX_BYTES = 64 * 1024 * 1024
TABLES = {"plans", "plan_steps", "tasks"}
COLUMNS = {
    "plans": {"id", "title", "status", "created_at", "updated_at"},
    "plan_steps": {"id", "plan_id", "position", "task_type", "payload", "description", "status", "result", "last_error", "started_at", "completed_at"},
    "tasks": {"id", "task_type", "payload", "status", "attempts", "max_attempts", "run_at", "locked_at", "result", "last_error", "created_at", "updated_at", "plan_step_id"},
}


def _read_only(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def _validate(db, deadline):
    from agent.plan_store import _validate_step
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("Snapshot integrity check failed")
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if {name for kind, name in objects if kind == "table"} != TABLES:
        raise ValueError("Only the general plan and queue database is supported")
    if any(kind not in {"table", "index"} for kind, name in objects):
        raise ValueError("Views and triggers are not supported")
    for table in sorted(TABLES):
        if {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')} != COLUMNS[table]:
            raise ValueError("Unsupported database schema")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Snapshot has broken references")
    if db.execute("SELECT 1 FROM plan_steps s LEFT JOIN plans p ON p.id=s.plan_id WHERE p.id IS NULL LIMIT 1").fetchone():
        raise ValueError("Snapshot has orphaned plan steps")
    if db.execute("SELECT 1 FROM tasks t LEFT JOIN plan_steps s ON s.id=t.plan_step_id WHERE t.plan_step_id IS NOT NULL AND s.id IS NULL LIMIT 1").fetchone():
        raise ValueError("Snapshot has orphaned queue tasks")
    if db.execute("SELECT 1 FROM tasks WHERE plan_step_id IS NULL AND status='running' LIMIT 1").fetchone():
        raise ValueError("Standalone running task outcome must be reconciled before snapshot")
    for table, statuses in (("plans", {"queued", "running", "checkpointed", "paused", "completed"}),
                            ("plan_steps", {"pending", "running", "succeeded", "failed"}),
                            ("tasks", {"pending", "running", "succeeded", "failed"})):
        if any(row[0] not in statuses for row in db.execute("SELECT DISTINCT status FROM " + table)):
            raise ValueError("Unsupported stored lifecycle state")
    for table in ("tasks", "plan_steps"):
        for task_type, payload in db.execute("SELECT task_type,payload FROM " + table):
            if time.monotonic() > deadline:
                raise ValueError("Snapshot validation time limit exceeded")
            _validate_step({"task_type": task_type, "payload": json.loads(payload)})
    for step_id, task_id, task_type, payload in db.execute("SELECT plan_step_id,id,task_type,payload FROM tasks WHERE plan_step_id IS NOT NULL"):
        if time.monotonic() > deadline:
            raise ValueError("Snapshot validation time limit exceeded")
        import uuid
        step = db.execute("SELECT task_type,payload FROM plan_steps WHERE id=?", (step_id,)).fetchone()
        if task_id != str(uuid.uuid5(uuid.NAMESPACE_URL, "workspace-plan-step:" + step_id)) or step != (task_type, payload):
            raise ValueError("Snapshot execution identity mismatch")
    return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in sorted(TABLES)}


def _copy_database(source, destination, *, tables, validate):
    """Create a validated private copy at a NEW path, without modifying source.

    The destination is published atomically only after validation. Both backup
    and restore use this same operation. Existing paths, including broken links,
    are refused. Caller must keep the snapshot private: it contains task data.
    """
    source = Path(source).expanduser().absolute()
    destination = Path(destination).expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source must be an existing regular database, not a symlink")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Source exceeds snapshot size limit")
    if os.path.lexists(destination):
        raise FileExistsError("Destination already exists; live databases are never overwritten")
    if not destination.parent.is_dir():
        raise ValueError("Destination directory must already exist")
    fd, temporary = tempfile.mkstemp(prefix=".plan-snapshot-", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with closing(_read_only(source)) as incoming, closing(sqlite3.connect(temporary)) as outgoing:
            incoming.execute("PRAGMA trusted_schema=OFF")
            # Reject unrelated namespaces before making a copy.
            names = {row[0] for row in incoming.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names != tables:
                raise ValueError("Only the general plan and queue database is supported")
            pages = incoming.execute("PRAGMA page_count").fetchone()[0]
            size = incoming.execute("PRAGMA page_size").fetchone()[0]
            if pages * size > MAX_BYTES:
                raise ValueError("Database including WAL exceeds snapshot size limit")
            deadline = time.monotonic() + 10
            def progress(status, remaining, total):
                if time.monotonic() > deadline or total * size > MAX_BYTES:
                    raise ValueError("Snapshot time or size limit exceeded")
            incoming.backup(outgoing, pages=128, progress=progress, sleep=0.01)
            outgoing.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            outgoing.execute("PRAGMA trusted_schema=OFF")
            counts = validate(outgoing, deadline)
        if temporary.stat().st_size > MAX_BYTES:
            raise ValueError("Snapshot exceeds size limit")
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
            digest = hashlib.sha256(file.read()).hexdigest()
        # Unlike replace(), link() refuses a destination created by a race.
        os.link(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {"sha256": digest, "bytes": destination.stat().st_size, "rows": counts,
                "workers_started": False, "states_rewritten": False}
    finally:
        temporary.unlink(missing_ok=True)


def copy_database(source, destination):
    """Copy only the general plan/queue database with its fixed validator."""
    return _copy_database(source, destination, tables=TABLES, validate=_validate)


def main():
    parser = argparse.ArgumentParser(description="Private general plan/queue backup or restore to a new path only")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(json.dumps(copy_database(args.source, args.destination), sort_keys=True))


if __name__ == "__main__":
    main()
