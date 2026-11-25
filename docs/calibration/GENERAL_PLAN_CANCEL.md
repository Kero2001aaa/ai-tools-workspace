# Explicit cancellation of inactive general plans

The trusted local planner MCP exposes `cancel_plan(plan_id, expected_updated_at)`.
Read the plan first and provide its exact `updated_at`. A stale value refuses with
no state changes. This is an explicit destructive lifecycle decision, not a model
or dashboard automatic action. The operation is not exposed as a dashboard button.

One SQLite transaction verifies the plan is queued, checkpointed or paused and all
steps and deterministic bound tasks have inactive known states and matching task
identity/payload. Running plans, running steps, unknown outcomes, conflicting task
bindings and unprojected successful tasks refuse. Reconcile those outcomes first;
this API does not invent a reconciliation decision or interrupt execution.

Only pending/failed remaining steps and their pending/failed bound tasks become
cancelled. Completed steps, saved results, error history and attempt counts remain.
It does not delete evidence, roll back effects or kill a process. There is no
uncancel operation. A cancelled plan cannot run, supervise or retry. To intentionally
repeat work, a trusted operator must create a new plan after considering prior effects.

Read-only monitoring/context and validated private backup/restore accept the added
cancelled lifecycle state. Old readers that do not know this state may refuse a
new snapshot; older code is not a downgrade route for cancelled plans. No DB tables
or columns were added. No provider, source scope or execution type was expanded.

Qualification includes actual local stdio MCP checkpoint/cancel/process-restart,
backup/restore, stale-state and unknown-outcome refusals. These are local lifecycle
tests, not whole Stage 4.3 acceptance or a running-task cancellation guarantee.
