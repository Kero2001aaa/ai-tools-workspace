# Stage 11 — Durable local task queue

The workspace now includes a small SQLite-backed task queue and a one-task worker.

## Supported task types

- `workspace_status`: records the Python version, current workspace path, and execution timestamp.
- `memory_recent`: reads a bounded number of recent local memories (1–20).

Task types are explicitly allowlisted in `agent/task_queue.py` and `agent/task_worker.py`. The worker does not execute arbitrary shell commands, Python snippets, model-generated code, or arbitrary URLs.

## Commands

Enqueue a workspace status check:

```bash
.venv/bin/python -m agent.task_worker enqueue workspace_status
```

Enqueue a bounded memory report:

```bash
.venv/bin/python -m agent.task_worker enqueue memory_recent --payload '{"limit":5}'
```

Execute at most one due task:

```bash
.venv/bin/python -m agent.task_worker run-once
```

List recent tasks:

```bash
.venv/bin/python -m agent.task_worker list
```

## Reliability and safety

- Queue data is stored in `.data/tasks.sqlite3`, separate from the memory database and ignored by Git.
- Claims use SQLite `BEGIN IMMEDIATE` transactions to prevent two workers from claiming the same pending task.
- Tasks have bounded attempts, delayed retries, and expiring worker leases.
- A crashed worker's expired task is made eligible for retry; exhausted tasks become failed.
- Results and errors are bounded before persistence.
- No task runs until explicitly enqueued and a worker is invoked.
- The initial worker executes one task and exits. It is not a daemon or autonomous scheduler.
- Do not enqueue sensitive data. The queue is local to the Codespace and is not automatically backed up.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The queue is a safe execution foundation. Future task types should be added individually, with narrow input schemas, explicit permissions, timeouts, audit logs, and approval gates for external side effects.
