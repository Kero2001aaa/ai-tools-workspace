# Private creation for new memory databases

MemoryStore reuses the reviewed general SQLite precreation helper before its
existing initialization transaction. The existed-before-open snapshot is taken
first, so precreation does not turn a new database into a pre-existing empty file
or bypass the legacy migration gate. New normal filesystem databases start with
owner-only mode0600 without changing the process umask.

Current-schema validation, explicit backup-first legacy migration, ACLs, namespace
factories and audit behavior are unchanged. Existing regular-file modes and data
are not silently modified. Pre-existing empty databases still refuse, including
the original bounded wait for a concurrent creator. Symlink leaves and file: URI
names refuse before initialization. Existing callers relying on symlink database
paths must select the actual trusted filesystem path instead.

This is not encryption, a retention or sharing change, a hostile-filesystem
sandbox or remediation of broad permissions on an existing database. Trusted
parent directories and concurrent filesystem writers remain assumptions. The
MemoryStore path resolver still owns its original path semantics; this patch
does not add SQLite in-memory support or a URI interface to MemoryStore. Exact
`:memory:` is explicitly refused before disk creation: MemoryStore historically
resolved it to a literal disk filename, not a true in-memory database. The
TaskQueue/PlanStore exact in-memory semantics remain unchanged.

Tests exercise real subprocesses under umask000/022, both scope factories,
existing records/mode preservation, pre-existing empty and symlink refusal plus
all existing migration and delayed-concurrent-initialization regressions.
