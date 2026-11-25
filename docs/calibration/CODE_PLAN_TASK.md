# Explicit local-code-report plan task, candidate

`local_code_report` reuses existing PlanStore, TaskQueue and stable plan-step
execution identity. No database table, migration or parallel journal is added.
No code task works by default. Trusted operator must set WORKSPACE_CODE_CONTEXT
to JSON containing an absolute trusted root and1-20exact reviewed approved_files.
This is a filesystem disclosure grant, not a value the model chooses. Never set
it from user/model task data. Payload permits only question(up to1000chars),
1-2exact path/line selections(up to50lines each), and config_sha256. The canonical
JSON config hash must match current operator config at execution; restore onto a
different config fails rather than silently redirecting files. A hash is binding,
not proof of authorization. Existing authenticated local queue/plan tools enforce
the execution request; knowledge of a hash alone is not approval to read files.

The worker invokes fixed trusted Python report code in a child, not source code
from approved files. Child env omits credentials, task cannot set root/env/code
or run commands. Pipe output capped128KiB; select deadline5seconds from launch
completion, then child killed/reaped. Config/payload validation and bounded local
temporary input staging precede launch; process launch, kernel uninterruptible
I/O and OS cleanup do not have a hard wall-time guarantee. No sandbox or network
isolation is claimed: trusted child has host process permissions but fixed reader
logic. Underlying reader's trust/secrecy/hardlink/snapshot limits still apply.
Source errors/child stderr are not returned. No network/provider/model is used.

Report is quote-only, not code understanding. Actual quote report is persisted
as existing queue result and plan-step result. This is a deliberate retention
change from pure helpers: approved excerpts and question enter the local plaintext
plan DB and its private backups under existing retention; no encryption or new
retention policy is implied. Operator approval must cover these excerpts. It is
not stored in shared memory/Adult. Monitoring returns fixed statuses and IDs,
never question/path/quotes/config root. get_plan remains a local authorized full
result API; no new public interface is created. Existing standalone queue retry
semantics apply to this read-only operation; plans pause after known failure and
unknown-running state is not automatically replayed.

Whole-DB recovery validates the new task payload with the same allowlist contract.
Completed report survives restart/backup restore even if source is gone, and is
not rerun. Pending task after config change fails closed. No source-change binding
before execution is claimed: read hashes identify the snapshot actually read.
Question, quotes and paths remain untrusted and must be escaped by any UI.

Tests use temporary explicit grants, actual child processes and actual MCP stdio
planner restart; report persisted at checkpoint and same plan resumed without
rereading completed source. Controlled output cap/timeout/launch-error tests,
monitor projection, missing source pause, config/payload override refusal and
backup restore are included. This proves a local quote-only durable task subset,
not Stage5.1/5.3acceptance or live model/research qualification.
