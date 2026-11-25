# No-create standalone task monitoring

The authenticated GET /api/tasks route no longer constructs TaskQueue. It reads
the existing default standalone queue through an encoded SQLite mode=ro URI,
query_only and trusted_schema=OFF, with a bounded query deadline and row limit.
A missing database returns the same empty list without creating a file/directory.
Existing rows keep the list_tasks shape and created_at-descending order. Legacy
rows without plan_step_id are readable without schema migration. Views, symlinks
and nonregular paths are refused. Store/JSON errors return a fixed503 response,
not raw stored content or paths. POST enqueue/run and MCP behavior are unchanged.

Status count reads also use encoded file URIs so literal question marks and
fragment characters in a filename are not interpreted as SQLite URI parameters.
No queue claim, lease recovery, worker dispatch or task-state mutation occurs.
The database parent directory is trusted; this is not hostile filesystem race
protection, multi-user authorization or encryption. SQLite may need its normal
WAL shared-memory/sidecar behavior; mode=ro is not an OS no-write sandbox. Existing
WAL committed rows remain visible. This does not redact task inputs/results from
the already authenticated task view or authorize sharing them elsewhere.

Memory monitoring is deliberately unchanged because MemoryStore reads have
separate ACL audit/migration semantics. The dashboard's whole-data-endpoint
read-only description is therefore not a claim that memory reads have no audit
writes. General plan and Adult monitoring retain their separate projections.
Tests cover actual HTTP auth/no-create/fixed errors, queue response compatibility,
legacy no-migration, file bytes/mtime, literal URI characters, WAL and no dispatch.
