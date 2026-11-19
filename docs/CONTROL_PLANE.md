# Phase 14 — Local control plane

The dashboard provides read-only monitoring plus a small set of authenticated task actions.

## Start

```bash
.venv/bin/python -m agent.control_plane
```

Open forwarded port **8000** in Codespaces and keep its visibility set to **Private**. The server binds only to `127.0.0.1`. On startup, it prints a randomly generated one-time control token. Enter that token in the dashboard; it is held only in the page's current memory and is not saved in browser storage. Restarting the server invalidates the previous token.

## Monitoring endpoints

- `GET /api/status`
- `GET /api/tasks`
- `GET /api/memories`

These endpoints are read-only. Memory contents may include personal project notes; do not expose the forwarded port publicly.

## Authenticated task actions

- `POST /api/tasks/enqueue`: queues only `workspace_status` or `memory_recent`.
- `POST /api/tasks/run-once`: executes at most one due task from the existing allowlist.

Both actions require the bearer token and JSON content type. The server rejects cross-origin requests, caps request bodies at 8 KiB, limits authenticated actions to 10 per minute, and records successful/failed action outcomes in `.data/control_audit.sqlite3` (override with `CONTROL_AUDIT_DB`). The UI asks for confirmation before enqueueing or executing. Enqueue and execution are separate operations.

No arbitrary shell, Python, URL, plugin, credential, memory-edit, or configuration actions are exposed. Task types remain allowlisted in `agent/task_queue.py`; the worker itself validates task payloads.

## Security boundaries

- Loopback-only listener (`127.0.0.1`); never change it to `0.0.0.0`.
- Keep the Codespaces forwarded port private. Do not share its URL or one-time token.
- The dashboard is a local development control surface, not an internet-facing production service.
- The token is generated at process startup and printed once to the terminal. Anyone with the token and access to the private forwarded endpoint can invoke the two permitted actions.
- The audit log records action, outcome, timestamp, and task ID; it deliberately does not record credentials or task payload contents.
- The dashboard uses the Python standard library only; no hosted service or additional runtime package is required.
