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

All three require the same bearer token as actions. The page initially shows a locked shell, without private data requests. Enter the token and select **Load private data**. Successful reads then refresh every 30 seconds while the token remains present. Editing or clearing the token clears displayed monitoring data; it is not stored across reloads. Memory contents may include personal project notes; do not expose the forwarded port publicly.

Every GET and POST checks the Host against exact local authorities for the bound port. In Codespaces, the exact forwarded authority is derived from CODESPACE_NAME and GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN at startup. No arbitrary Host, wildcard domain, X-Forwarded-Host, or Forwarded header is trusted. If a different reverse proxy is used, it must be separately designed and tested, not enabled by disabling Host checks. API reads also reject cross-origin Origin and Sec-Fetch-Site requests. The HTML shell contains no token or private snapshots.

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


## Dashboard Agent (opt-in)

The dashboard can submit one user-authored request to the existing LangGraph tool agent.

- POST /api/agent/run accepts exactly one JSON field, request, with 1–4,000 characters.
- It requires the same one-time bearer token, shares the authenticated action rate limit, and asks for browser confirmation before submission.
- The response is capped at 20,000 characters. The audit log records only the action and outcome, never the prompt or answer.
- It is disabled by default. To enable it, set ENABLE_DASHBOARD_AGENT=true in the private .env file and restart the dashboard.
- Each request may call the configured model and approved MCP tools. Provider charges, free-tier quotas, and external-service usage depend on the configured provider. MODEL_READY confirms configuration only; it does not establish that the provider is free or that a request will succeed.
- Do not enable this feature until you have verified that the configured model route is within your zero-cost budget. Keep the forwarded Codespaces port private.

This endpoint does not expose arbitrary shell or code execution. It invokes the existing LangGraph agent, whose available tools are the workspace's configured MCP allowlist.
