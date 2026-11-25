# Operator-only whole memory database recovery

The migration manager already provides a backup for legacy migration. It does not
back up an already-current database or restore one. This operation reuses its
current-schema and fixed-ACL validators plus stored-row decoding. The same
private no-overwrite SQLite copy primitive used by plan recovery provides WAL
snapshotting, bounded size/time, cleanup and atomic publication.

```sh
python -m agent.memory_recovery backup .data/memory.sqlite3 /private/backups/memory.sqlite3
python -m agent.memory_recovery restore /private/backups/memory.sqlite3 /private/recovery/memory.sqlite3
```

Destination directory must exist. Destination file must not exist. Only current
schema version1is supported. Legacy data still uses the existing explicit
backup-and-migrate command. Future schema versions, mixed databases, altered ACLs,
invalid stored records, views and triggers are refused, never repaired silently.

This is a trusted operator file-level disaster-recovery operation, not a Workspace
agent export/read capability. It is not registered in MCP or HTTP. The snapshot
contains BOTH private namespaces and audit records, under their original IDs and
ACLs. It is not encrypted. Keep the folder and file private, do not upload or put
it in source control. Receipts expose counts and digest, never memory text.
A digest does not authenticate a backup. Only restore trusted backups.

No scoped read, feedback publication, memory merge or namespace transfer happens.
After restore, Workspace still cannot read Adult records. Adult can read its own
records and only explicitly shareable Workspace records under existing policy.
Audit and ACL rows survive unchanged. Restoring never starts a task or model.

Do not overwrite or hot-swap a live database. Stop the old local processes, restore
to a new path, then explicitly configure WORKSPACE_MEMORY_DBfor that path. Keep
old data until the recovery is verified. Memory and plans are independently
snapshotted and are not a cross-database atomic backup. This is not a full
Workspace or Adult-artifact recovery package, so roadmap12.2remains incomplete.
