# General plan monitoring in the existing local control plane

Authenticated GET /api/plans?limit=20uses the existing bearer/Host/Origin/Fetch-Site
protections. Limit1-50, no other query fields, no write/run/resume behavior.
WORKSPACE_PLAN_DBselects the operator-configured path; no request can choose it.
The reader does not construct PlanStore, create/migrate missing DBs or run workers.
Only existing matching tables are queried, under one read transaction. Query
work is bounded and database errors fail closed. Not a remote owner identity or
new public service. Keep existing loopback/private-port restrictions.

Projection contains plan IDs/status/timestamps, bounded steps, type/status,
stable bound task ID, attempts and completed counts. It excludes titles,
descriptions, payloads, results, paths and stored errors. Stored ID/lifecycle/type
fields are validated rather than treating arbitrary database text as safe.
Unknown running work is visibly marked as needing outcome reconciliation, never
resubmitted. A durable queue success not yet projected remains visible as such;
this endpoint does not perform the reconciliation. Existing bounded supervisor
is still the only general execution path.

This read route connects actual plan/queue state to existing controls. It does
not yet add a UI panel, user request entry, mobile journey or10.1acceptance.

The existing dashboard now includes a General plans panel using the authenticated
route in its regular refresh. Text-only rendering shows counts and binding state,
with unavailable/empty/wait messages. Changing/clearing the local token or any
monitoring error clears the panel with other private fields. No plan action
buttons added. Desktop and390pxviewport panel previews inspected with synthetic
real SQLite projection, not a physical-phone acceptance. Entire live integrated
page behavior remains covered by HTTP plus JavaScript rendering checks, not a
claim of remote deployment or10.1/10.3completion.
