# Read a saved local code quote report

Trusted local operator command, no model or public API:

```
python -m agent.code_report_cli --db /approved/local/plans.sqlite3 \
  --plan-id <plan-uuid> --step-id <step-uuid>
```

The command retrieves one completed local_code_report step and its deterministic
queue task. It requires matching successful task/step result bytes, validates the
bounded report/citation shape and exact quote digest, and returns only whitelisted
quote fields with source path/line/hash. It does not require current code context,
open source files, instantiate PlanStore, migrate a database, dispatch tasks,
create a missing database or change a checkpoint. sqlite mode=ro/query_only,
short busy timeout/progress budget and bounded result projection are used. This
is a trusted local DB read, not multi-user authorization or tamper-proof storage.
A malicious party able to rewrite both DB results and hashes can forge content.
There is no new encryption or permission boundary. SQLite/kernel I/O still has
no hard wall deadline. Legacy helper-only reports are not executed-plan results.

Output is ASCII JSON with terminal control characters escaped. Do not decode
escapes directly into a terminal or HTML; downstream renderers must independently
escape all strings. Static limitations are reconstructed instead of accepting
arbitrary stored explanations. This is a saved source snapshot and quote-only
result, not current file content, live-source authenticity or model understanding.
The local plaintext DB/backups retain approved excerpts under the existing policy.
Ownership of DB/path and authority for disclosure remain the local operator's.

Tests create a real code report through PlanStore, then delete source and read
same stored task without a worker. A fresh command process returns valid escaped
JSON. Missing file/pair, mismatched results and bad quote hash fail with one stable
code, without exposing paths/errors. This adds usable retrieval to the local
quote-only subset, not acceptance of the whole research/code-understanding stage.
