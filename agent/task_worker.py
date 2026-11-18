"""Execute one queued workspace task from a strict allowlist."""
import argparse
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

from agent.memory_store import MemoryStore
from agent.task_queue import TaskQueue


def execute_task(task):
    payload = task["payload"]
    if task["task_type"] == "workspace_status":
        return {
            "python": platform.python_version(),
            "workspace": str(Path.cwd()),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    if task["task_type"] == "memory_recent":
        limit = payload.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("memory_recent limit must be an integer from 1 to 20")
        return {"memories": MemoryStore().recent(limit)}
    raise ValueError("Task type is not allowlisted")


def run_once(queue=None):
    queue = queue or TaskQueue()
    task = queue.claim_next()
    if task is None:
        return {"status": "idle"}
    try:
        result = execute_task(task)
        completed = queue.complete(task["id"], result)
        return {"status": completed["status"], "task_id": task["id"], "result": result}
    except Exception as exc:
        failed = queue.fail(task["id"], exc)
        return {
            "status": failed["status"], "task_id": task["id"],
            "attempts": failed["attempts"], "error": str(exc),
        }


def main():
    parser = argparse.ArgumentParser(description="Manage the local approved-task queue.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("enqueue", help="Add an allowlisted task")
    add.add_argument("task_type", choices=("workspace_status", "memory_recent"))
    add.add_argument("--payload", default="{}", help="JSON object")
    add.add_argument("--max-attempts", type=int, default=3)
    sub.add_parser("run-once", help="Claim and execute at most one due task")
    listing = sub.add_parser("list", help="List recent tasks")
    listing.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    queue = TaskQueue()
    if args.command == "enqueue":
        payload = json.loads(args.payload)
        result = queue.enqueue(args.task_type, payload, max_attempts=args.max_attempts)
    elif args.command == "run-once":
        result = run_once(queue)
    else:
        result = queue.list_tasks(args.limit)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
