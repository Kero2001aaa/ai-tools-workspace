# General plan-to-queue execution binding

This is an internal general-workspace foundation. It does not register new tools,
add task types, bridge Adult data, call a model or expose a remote endpoint.

PlanStore reuses TaskQueue in the same SQLite file. Existing queue databases gain
a nullable plan_step_id column and unique non-null index. Each plan step has a
stable UUIDv5 execution task ID derived from its existing step ID. Binding occurs
inside the plan transaction before dispatch; retries/restarts never mint a second
identity. Existing completed plan steps and their outputs are not rewritten.

Only workspace_status, memory_recent and explicitly configured local_code_report
are allowed. New local code report execution requires its trusted operator source allowlist
and matching configuration digest; queue binding grants no filesystem scope.
The plan's strict
operation-specific payload validator is reused before binding. Worker receives
the actual queue task, including its task ID and owning step ID. Queue output is
committed first and projected into the plan step. A restart after output commit
reconciles that output without calling the worker again. General claim_next and
generic complete/fail cannot dispatch or finalize plan-owned tasks. Targeted
claims atomically admit only pending tasks. Completion matches task ID, step ID,
attempt number and lock timestamp, refusing old claims.

Known failed work still requires explicit retry_failed_step. It resets the same
queue identity and only the matching failed plan step. Completed outputs stay
unchanged. Unknown running work is never automatically retried after a timeout.
This changes the previous ten-minute blind recovery behavior. Legacy running
steps without queue identity also wait for outcome reconciliation. There is no
automatic claim that such a task completed, failed or is safe to repeat. A future
operator reconciliation workflow is needed; no unsafe recovery button is added.

This is not exactly-once external-side-effect execution. The current workers are
read-only. A crash after a worker starts but before its result commits leaves an
unknown running task and blocks replay. Supporting write tasks would require its
own idempotency/authority boundary and must not be inferred from this change.

Tests use actual SQLite, real deterministic read-only workers or synthetic bounded
worker results. They cover stable identity, checkpoint/reopen, concurrent claims,
old-running no replay, committed-success recovery, lost completion response,
preclaim interruption, explicit failure retry, claim fencing, cross-plan retry,
legacy schema migration, allowed types and no Adult admission. Full roadmap4.1
acceptance remains conditional on its scope and phase3dependency. This local
patch is not publication or release readiness.
