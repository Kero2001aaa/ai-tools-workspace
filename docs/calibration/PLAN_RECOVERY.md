# Private general plan/queue backup and restore

Use the existing general PlanStore database, which now contains plans, plan_steps
and queue tasks. This feature does not cover the Adult database, standalone memory,
artifacts, credentials or a full-workspace backup. Roadmap12.2is not complete.

```sh
python -m agent.plan_recovery backup .data/plans.sqlite3 /private/backups/plans.sqlite3
python -m agent.plan_recovery restore /private/backups/plans.sqlite3 /private/recovery/plans.sqlite3
```

Both operations require an existing destination directory and a NEW destination.
They refuse overwrite, including symlinks, and never replace a live database.
They use SQLite's backup API to include committed WAL pages in one snapshot,
check integrity, foreign-key/orphan references and plan execution identity, and
refuse unrelated namespaces, unknown schemas, views/triggers or unapproved task
types. Snapshots are mode0600and contain private task inputs and outputs. Keep
backup folders private too. Copying is not encryption, redaction or permission
to upload. The SHA-256receipt is a corruption comparison aid, not authentication
of an untrusted snapshot. Use only trusted local backups. A restored database
retains inputs/results as stored; no archive extraction or external path reads.

Copy limit64MiB, bounded backup/validation runtime10seconds. Source is read-only;
no workers start, task state changes or automatic replay. Restore success means
a validated file exists, not that the application switched to it. Stop the old
local supervisor before intentionally configuring WORKSPACE_PLAN_DBto the new
path. Do not run old and restored copies concurrently as independent producers.
Standalone running queue tasks are refused because their old generic lease-recovery
behavior could replay them after restore. Reconcile or finish them before backup.
Completed outputs and durable queue identities survive. A running task whose
outcome is unknown stays running and needs reconciliation. This operation does
not pretend it is safe to retry unknown work. Known failed tasks remain paused
until the existing explicit retry path is used. Only the existing read-only
workspace_status, memory_recent and explicitly configured local_code_report workers are supported; local_code_report requires
the trusted operator source allowlist and matching configuration digest before
new execution. Saved completed results can be restored without reopening source.
This feature grants no new execution or file-disclosure authority.

For a real recovery drill: checkpoint a two-step plan; back it up; restore to a
new clean path; reopen PlanStore at that path; run the remaining step; confirm
that the first result and queue identity remain unchanged and only one dispatch
occurs. Automated tests additionally cover WAL snapshots, unknown running waits,
committed-success projection without replay, corrupt/mixed databases, target
races, restrictive permissions and temporary cleanup. No live user database is
used in tests.
