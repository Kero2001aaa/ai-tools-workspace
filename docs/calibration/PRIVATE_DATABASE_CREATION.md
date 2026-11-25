# Owner-only creation of new general SQLite stores

TaskQueue and PlanStore now pre-create a missing database with O_CREAT/O_EXCL
and mode0600 before SQLite opens it. This does not use a process-wide umask,
replace an existing file or chmod an existing database. Existing regular files
retain their mode and data. A pre-existing symlink, directory, FIFO or other
non-regular leaf is refused. A competing ordinary creator is inspected as an
existing file rather than overwritten.

This only protects the initial mode of new general queue/plan files. Existing
broad permissions are not remediated, and no claim is made about an existing
store's privacy. Parent directories and concurrent filesystem writers remain
trusted. The path-based SQLite open after pre-creation is not a defense against
an attacker replacing the path or a parent between operations. No filesystem
sandbox, encryption, DLP, retention change or source-access grant is added.

On POSIX, native SQLite journal/WAL sidecars normally inherit database access
mode. This change does not alter SQLite's journaling or encrypt any sidecar.
Operators still need a private directory and must protect the whole database,
its sidecars and its backups. A newly created empty file may remain after failed
initialization; it is not deleted automatically because another process may use it.

Tests use real subprocesses under permissive umasks, concurrent creation,
existing mode/data preservation and non-regular-file refusals, followed by a
normal bound-plan execution. This is not a complete host-security audit.
