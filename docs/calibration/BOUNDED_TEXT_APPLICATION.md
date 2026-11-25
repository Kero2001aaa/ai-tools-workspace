# Internal bounded text application

This internal application facade composes the existing executor/supervisor
without provider-specific routing, SQL or new execution logic. It is not AdultAgent
or a public approval endpoint. One trusted operator injects the real executor,
fixed configuration fingerprint and task budget. Call arguments never select a
principal, model endpoint, adapter implementation, state path or credentials.

Submission defaults to ephemeral input and only stages/checkpoints. Explicit
retention uses existing encryption validation. Run accepts a bounded segment and
cannot run waiting_user. Resume accepts only continue/cancel. For waiting_user,
continue persists the existing task-store approval then invokes the existing
continue_after_approval method for one segment. A checkpointed continue uses the
existing resume transaction then one segment without inventing an approval count.
No paused correction, changed replacement input or terminal resume is allowed.
A response-loss error after the decision commit stops before dispatch; the caller
can inspect the durable queued state and explicitly run it. It must never silently
replay an uncertain attempt.

The authenticated transport must verify real user authority before calling any
approval method. This facade cannot prove who called it and does not claim to.
No MCP/HTTP route, gateway allowlist or model-agent registration changes here.
Deterministic local tests are software authorization simulations only, not user
consent or proof of live AI service availability.

Implemented methods: zero-call submit_text_task; safe get_task; bounded run_task;
explicit resume_task continue/cancel; cancel_task; task-owned get_result_chunk.
The facade is intentionally text-only and internal. It has no default provider,
server factory, environment configuration or transport registration. The injected
executor owns its existing fixed adapter mapping and route registry. Five-failure
approval tests use only deterministic local adapters and software-simulated
operator decisions. Completed/cancelled input cleanup is input-only and reports
failure separately, preserving results and preventing replay. Image orchestration, list/attempts
facade and a complete AdultAgent contract remain unfinished.

The existing executor now checks an ephemeral cache reference under its normal
lease before creating/running a model step. Missing bytes release to needs_input
with zero attempts rather than being reported as a provider failure. Encrypted
key/integrity failures retain their existing behavior. The internal facade's
restore_input_and_resume uses the same narrow exact-input restoration transaction
as the fixed service, accepts only a zero-attempt needs_input task, and runs one
explicit bounded segment. Changed source/instruction are rejected. Tests exercise
cache loss, exact restore, one result/no replay, and an actual separate process
observing missing input with zero attempts. Post-attempt ephemeral loss still
cannot be repaired through this narrow method and is not claimed supported.

A zero-attempt loss between the leased input check and pipeline input loading
also returns needs_input. The execution step remains pending until begin()
commits an attempt, so exact restoration can rerun it without a fabricated
lease-recovery checkpoint or a forbidden step reset. A committed unknown
attempt is still never replayed.

The internal facade supports safe bounded task paging and attempt history.
It refuses mixed-lane task lists rather than exposing or silently skipping
image tasks. These metadata reads do not run cleanup or change task state.
A cancellation that fences finalization is returned as durable cancellation
with input cleanup; an ordinary nonterminal exception is still surfaced.
