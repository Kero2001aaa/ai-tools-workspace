# Fixed-free text application composition

FixedFreeTextService composes the existing TaskStore, encrypted ArtifactStore,
Intake, CapabilityRegistry, AttemptTracker, ModelRouter, TextPipeline,
OmniRouteTextAdapter, fenced executor and supervisor. No replacement architecture
or automatic certification is added. It is a library, not registered or deployed
as an agent tool, gateway endpoint or always-running service.

The caller must supply matching encryption stores/key and Adult-only configuration
for exactly https://openrouter.ai/api/v1. The only accepted registry route has
provider openrouter, model openrouter/free and cost_class free_verified. Existing
registry evidence must already be independently verified and enabled. The service
never registers, verifies, enables or relaxes a route. Evidence is rechecked at
dispatch. Missing, disabled or wrong-model routes result in zero-call pause.

submit(task_type,text,instruction,budget=...) requires explicit positive provider
call budget and stages encrypted input without constructing/invoking a client.
run(task_id) makes at most one call in this fixed-provider service; max_attempts
other than1 is rejected. The broader local supervisor does not authorize repeated
probes of this provider. Unknown provider errors and rate-limit responses are not
silently retried here. Live availability remains a separate precondition for a
real request, and the current unavailable-provider state is not changed by tests.

The existing build_chat_model/free_route_guard remain the wire authority: fixed
endpoint/model, zero-price policy, data collection denied, no fallback, redirects,
tool extensions, streaming or SDK retries. Adult configuration never inherits the
Workspace MODEL variables. Output acceptance remains deterministic/structural,
not a semantic quality guarantee. Existing deadlines, retention and budgets hold.

A narrow DTO correction allows only the exact literal openrouter/free in model_id
and selected_model fields, alongside previously valid opaque values. Task IDs,
adapter IDs, payload IDs, arbitrary slash models, paths and URLs remain rejected.
This resolves a real mismatch between the registry DTO and the sole permitted
wire model. No normalized-model substitution or broad identifier relaxation is
used. Persisted checkpoints now retain selected route metadata. Supervisor budget
terminal transitions use the existing generic state_transition API, avoiding an
invalid reserved-event call exposed by the first real provider-budget test.

Tests use synthetic evidence and a patched fake client only. They prove zero-call
submission, fake-client completion and reopen/no duplicate, persistent model-ID
roundtrip, missing/disabled/wrong-model no-call, fixed endpoint and explicit budget,
wrong-key state preservation, and rejection of multi-attempt requests. Synthetic
URLs are test fixtures, not source evidence or production certification. No live
provider request was made. No live AI, image generation or installation is claimed.

The fixed service now injects an optional internal route validator into the
existing attempt tracker. It revalidates the exact candidate against its current
registry row within the same BEGIN IMMEDIATE transaction as attempt creation:
enabled/verified state, contract/evidence version, task/model identity, text
modalities, capability expiry and zero-cost evidence expiry. A disable that lands
between selection and attempt creation produces zero attempts and provider calls.
The default tracker path stays compatible for existing explicit callers. This
fences pre-commit revocation, not changes after an attempt is committed or a call
is in flight. It does not certify availability or alter the transport policy.

Input staging reuses the executor's retention-aware stage method. The low-level
service keeps its encrypted default for compatibility; the application passes an
explicit boolean and defaults to ephemeral process-local input. Missing ephemeral
input on a checkpointed task yields needs_input with zero attempts. The configured
service fingerprint/output key is still required. No replacement resume or MCP
registration is included in this slice.
