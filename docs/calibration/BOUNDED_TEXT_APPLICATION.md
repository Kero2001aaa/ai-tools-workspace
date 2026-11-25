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
failure separately, preserving results and preventing replay. Full ephemeral
restart restoration for this generic facade, image orchestration, list/attempts
facade and a complete AdultAgent contract remain unfinished.
