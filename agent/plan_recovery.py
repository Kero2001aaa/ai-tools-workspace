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
TABLES = {"plans", "plan_steps", "tasks", "task_attempts", "plan_continuations"}
JOURNAL_TABLES = TABLES - {"plan_continuations"}
LEGACY_TABLES = JOURNAL_TABLES - {"task_attempts"}
SUPPORTED_TABLES = (TABLES, JOURNAL_TABLES, LEGACY_TABLES)
from agent.task_attempts import COLUMNS as ATTEMPT_COLUMNS
COLUMNS = {
    "task_attempts": ATTEMPT_COLUMNS,
    "plan_continuations": {"decision_id", "plan_id", "failure_count", "created_at"},
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
    present = {name for kind,name in objects if kind == "table"}
    if present not in SUPPORTED_TABLES:
        raise ValueError("Only the general plan and queue database is supported")
    if any(kind not in {"table", "index"} for kind, name in objects):
        raise ValueError("Views and triggers are not supported")
    for table in sorted(present):
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
    for table, statuses in (("plans", {"queued", "running", "checkpointed", "paused", "completed", "cancelled"}),
                            ("plan_steps", {"pending", "running", "succeeded", "failed", "cancelled"}),
                            ("tasks", {"pending", "running", "succeeded", "failed", "cancelled"})):
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
    if 'task_attempts' in present:
        from agent.task_attempts import fingerprints, digest
        for row in db.execute('SELECT task_id,attempt_no,plan_step_id,method_fingerprint,input_fingerprint,claim_stamp,started_at,finished_at,status,failure_class,retryable,error_code,result_sha256,retry_reason FROM task_attempts'):
            if time.monotonic()>deadline:
                raise ValueError('Snapshot validation time limit exceeded')
            task=db.execute('SELECT task_type,payload,plan_step_id,attempts,status,result FROM tasks WHERE id=?',(row[0],)).fetchone()
            if task is None or row[2]!=task[2] or not task[2] or type(row[1]) is not int or not 1<=row[1]<=task[3]:
                raise ValueError('Snapshot attempt identity mismatch')
            if (row[3],row[4])!=fingerprints(task[0],json.loads(task[1])):
                raise ValueError('Snapshot attempt fingerprint mismatch')
            if row[8] not in {'running','succeeded','failed'} or row[10] not in {0,1}:
                raise ValueError('Snapshot attempt state invalid')
            if row[8]=='running' and (row[7] is not None or row[9] is not None or row[11] is not None or row[12] is not None):
                raise ValueError('Snapshot running attempt finalized')
            if row[8]=='succeeded' and (not row[7] or row[9] is not None or row[10]!=0 or row[11] is not None or not isinstance(row[12],str) or len(row[12])!=64):
                raise ValueError('Snapshot success evidence invalid')
            if row[8]=='failed' and (not row[7] or row[9] not in {'permission','transient','storage','input_or_state','unknown'} or row[12] is not None or not row[11]):
                raise ValueError('Snapshot failure evidence invalid')
            if row[9]!='transient' and row[10]:
                raise ValueError('Snapshot failure retry class invalid')
            if row[13] is not None and (not isinstance(row[13],str) or not 1<=len(row[13])<=240 or any(ord(c)<32 for c in row[13])):
                raise ValueError('Snapshot retry reason invalid')
            if row[1]==task[3] and row[8]=='succeeded' and task[4]=='succeeded' and row[12]!=digest(task[5]):
                raise ValueError('Snapshot result evidence mismatch')
    if 'plan_continuations' in present:
        import uuid
        from datetime import datetime
        for decision_id,plan_id,count,created_at in db.execute('SELECT decision_id,plan_id,failure_count,created_at FROM plan_continuations'):
            if time.monotonic()>deadline:
                raise ValueError('Snapshot validation time limit exceeded')
            failures=db.execute("SELECT COUNT(*) FROM task_attempts a JOIN plan_steps s ON s.id=a.plan_step_id WHERE s.plan_id=? AND a.status='failed'",(plan_id,)).fetchone()[0]
            if type(count) is not int or count<5 or count>failures:
                raise ValueError('Snapshot continuation count invalid')
            try:
                if str(uuid.UUID(decision_id))!=decision_id or datetime.fromisoformat(created_at).tzinfo is None:
                    raise ValueError()
            except (ValueError,TypeError,AttributeError):
                raise ValueError('Snapshot continuation metadata invalid') from None
            if not db.execute('SELECT 1 FROM plans WHERE id=?',(plan_id,)).fetchone():
                raise ValueError('Snapshot orphan continuation')
        for plan_id, in db.execute('SELECT DISTINCT plan_id FROM plan_continuations'):
            counts=[row[0] for row in db.execute('SELECT failure_count FROM plan_continuations WHERE plan_id=? ORDER BY failure_count',(plan_id,))]
            if any(b-a<5 for a,b in zip([0]+counts,counts)):
                raise ValueError('Snapshot continuation sequence invalid')
    return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in sorted(LEGACY_TABLES)}


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
            if names != tables and not (tables == TABLES and names in SUPPORTED_TABLES):
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
