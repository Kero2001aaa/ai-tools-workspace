# Control plane: read-only Adult task status

Authenticated GET routes (same bearer token, Host, Origin and Sec-Fetch checks as other `/api/` routes):

- `GET /api/adult/tasks?limit=1..50&status=<task status>` lists tasks newest first with the latest safe event and checkpoint summary.
- `GET /api/adult/tasks/<task_id>` shows one task with up to 50 safe events and its latest checkpoint summary.

Data boundary: only allowlisted lifecycle fields. No payloads, prompts, artifact ids, fingerprints, local paths, lease or worker ids, or event metadata. The event `reason_code` appears only when it belongs to an explicit lifecycle-code allowlist. `has_result` is a boolean, not an artifact id.

The store is opened only if the database already exists (`ADULT_STATE_DB` or `.data/adult.sqlite3`). The store uses SQLite mode=ro and query_only, validates current schema without migrations or directory creation, and rejects outdated schema. A status read never creates it; absent store returns `{"available": false, "tasks": []}`. No POST/cancel/resume routes exist for Adult tasks.
