# Explicit inactive-plan cancellation through the local control plane

POST /api/plans/cancel accepts exactly plan_id and expected_updated_at. It requires
the existing Bearer token, Host allowlist, Origin/Fetch-Site protections, JSON body
limit and action rate limit. Duplicate JSON keys refuse. The caller first reads
the current plan snapshot and explicitly chooses this destructive lifecycle
action. No dashboard button, model-driven cancellation or automatic invocation
is added by this change. Local/private binding rules are unchanged.

The route opens only an existing regular configured WORKSPACE_PLAN_DB through an
encoded mode=rw URI; it does not construct/migrate PlanStore or TaskQueue. Exact
current table/column schema is required; views/triggers refuse. It then reuses
the reviewed cancel_plan transaction with its same-state timestamp guard and
inactive/known-outcome checks. Running/unknown/unprojected/conflicting states
refuse, preserving existing results and attempt history. No worker is started,
no process interrupted and no completed result rolled back. There is no uncancel.

A success receipt returns only plan_id, status and updated_at. A lifecycle/store
refusal returns fixed409 without private title, payload, result or path. Shape
errors for framing, a non-object body, missing/extra fields or duplicate keys
return400. Invalid field values (including UUID, case, type, null or length)
follow the same fixed409 refusal as lifecycle/store failures. If the response
is lost, reload current state before retrying;
a completed cancellation may already have committed. There is no exactly-once
transport guarantee or new audit database. The plan's cancelled state/timestamp
are its durable outcome. Schema checks do not authenticate hostile databases or
remove trusted-parent/concurrent-filesystem caveats. SQLite shared-memory and
sidecar behavior is not an OS no-write sandbox. The existing transaction owns
claim/cancel race ordering, not the HTTP layer.

Actual HTTP tests cover auth/cross-origin/body refusal, missing no-create,
legacy no-migration, trigger refusal, stale/running refusal and checkpointed
cancellation preserving the completed step and task. Earlier underlying-store
race and stdio tests remain applicable. These are local lifecycle tests, not full
Stage4.3/10.3 acceptance, live user cancellation or a new execution grant.
