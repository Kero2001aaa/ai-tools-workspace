# Phase 14 — Read-only control plane

The first dashboard milestone is deliberately read-only.

## Start

```bash
.venv/bin/python -m agent.control_plane
```

Open the forwarded port **8000** in Codespaces. Keep its visibility set to **Private**. The server binds only to `127.0.0.1`; it does not listen on public interfaces.

## Dashboard contents

- Workspace and Python runtime status
- Integration enablement flags (never credential values)
- Local memory and task record counts
- Recent task records and recent memory entries
- Explicit read-only security posture

JSON endpoints:

- `GET /api/status`
- `GET /api/tasks`
- `GET /api/memories`

All other paths return 404. POST requests return 405. No endpoint can enqueue, execute, delete, or modify tasks, memories, credentials, or integrations.

## Security boundaries

- Loopback-only listener (`127.0.0.1`); do not change it to `0.0.0.0`.
- Keep the Codespaces forwarded port private. Do not share its URL.
- The dashboard is a local development control surface, not an internet-facing production service.
- Memory contents may include personal project notes. Do not expose the forwarded port publicly.
- No authentication is implemented because the service is loopback-only and intended for a private Codespaces tunnel. If the deployment model changes, add authentication and CSRF protections before enabling access.
- The dashboard uses the standard library only; no new runtime package or hosted service is required.

The task queue still has only two allowlisted task types and is not an autonomous scheduler. A future write-capable dashboard must add explicit authentication, CSRF protection, audit logging, and per-action confirmation before enabling controls.
