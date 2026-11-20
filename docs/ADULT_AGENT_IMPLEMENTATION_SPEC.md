# Adult Agent MVP — Implementation Specification

**Status:** Draft for technical review  
**Target:** Single-user experimental release  
**Scope:** Text + Image; Video deferred  
**Branch:** feature/adult-agent-mvp  
**Base:** main; no merge to main is authorized by this specification  
**Repository baseline reviewed:** 2026-11-20

---

## 1. Purpose and normative rules

This document is the implementation contract for the first Adult Agent release. Code must conform to these interfaces, persistence rules, MCP contracts, and acceptance tests. Any proposed deviation must be documented before implementation; it must not be improvised silently while coding.

The implementation must:

1. Extend and reuse existing Workspace infrastructure where its behavior is suitable.
2. Add Adult Agent as an isolated package under agent/adult/.
3. Enforce Adult memory isolation in the storage/repository layer, not only in prompts.
4. Persist task state, ordered steps, attempts, selected route/tool, latest result references, budgets, and pending user decisions.
5. Never store raw sensitive task payloads in ordinary memory, attempt logs, event metadata, or plaintext checkpoints.
6. Continue execution only within explicit attempt, wall-time, provider-call, and cost budgets.
7. Stop after every five failed attempts and enter a durable user-approval state.
8. Use OmniRoute only through verified API capabilities; never assume every listed model supports every modality.
9. Keep image APIs/adapters separate from the existing text chat adapter.
10. Add no automatic paid fallback, billing activation, external deployment, or automatic package/model installation.
11. Keep the existing test suite passing after every implementation stage.
12. Leave .vscode/, .env, local credentials, and existing runtime data untouched.
13. Keep all Adult work on this branch until a reviewed pull request is explicitly approved for merge.

### 1.1 MVP feature boundary

Supported task types:

- text.analyze
- text.generate
- text.rewrite
- image.analyze
- image.generate
- image.edit

Not in MVP:

- Video generation or editing.
- Multi-user identity, multi-tenant accounts, or user-role administration.
- Public hosting or remote MCP exposure.
- Automatic discovery followed by installation/activation of models or tools.
- Arbitrary shell, arbitrary Python execution, unrestricted filesystem access, or unrestricted browser actions.

Adult-content support is an explicit product capability, subject to the non-bypassable policy layer and the capabilities/terms of the selected provider. A user attestation does not prove another person's consent and cannot override policy. Sexual content involving minors is prohibited. The MVP must not generate or edit sexual imagery of real people without explicit consent; an attestation alone is not sufficient evidence. Do not design a policy bypass.

---

## 2. Verified repository baseline and reuse decisions

| Existing component | Current contract | Adult Agent decision |
|---|---|---|
| agent/tool_agent.py | LangGraph tool-calling workflow and MCP server configuration | Keep as the general Workspace agent; register Adult MCP only when explicitly enabled |
| agent/model.py | Optional OpenAI-compatible ChatOpenAI adapter; 30-second timeout; zero SDK retries; offline mode | Reuse for text inference; do not turn it into an image client |
| agent/memory_store.py | SQLite MemoryStore, keyword recall, recent memories | Extend via additive migration and scoped access methods |
| agent/memory_mcp_server.py | Workspace remember, recall, recent_memories | Keep these tools Workspace-scoped; do not expose a namespace parameter to callers |
| agent/plan_store.py | SQLite plans, step leases, checkpoints, bounded run-until-done, explicit retry | Reuse lifecycle patterns and tests; do not widen its allowlist to arbitrary Adult actions |
| agent/plan_mcp_server.py | MCP interface for the current restricted planner | Leave its tool contract intact |
| agent/task_queue.py / agent/task_worker.py | Separate allowlisted deterministic queue and workers | Do not use as the general Adult execution engine |
| agent/specialist_worker.py / agent/supervisor_worker.py | Tool-less text specialists; max four model calls; no automatic retries | Reuse validation/role separation patterns, not their no-retry execution policy |
| agent/research_mcp_server.py | Optional Exa/Firecrawl research | Optional later capability; not required for core Text + Image MVP |
| agent/gateway_mcp_server.py | Loopback-only Streamable HTTP gateway; explicit tool allowlist | Add Adult tools only behind an explicit opt-in and strict allowlist |

### 2.1 Important constraints discovered

- The current planner only accepts workspace_status and memory_recent. Adult task types must therefore live in an Adult-specific task store/executor; they must not be smuggled into the general planner.
- Current memory rows have no namespace or access-control columns. A migration and storage-level scope checks are required.
- Current specialist supervision intentionally stops at the first failed call and does not retain attempt history. It is not the Adult retry supervisor.
- Current agent/model.py is a chat adapter. Image generation/editing require modality-specific adapters.
- Current Gateway proxies tools, not arbitrary filesystem contents. Image artifacts must be returned through bounded Adult tools; never expose arbitrary paths.
- MCP calls must carry a durable explicit task_id; process or transport session lifetime must not be treated as task state.

---

## 3. Target architecture

    External Host (ChatGPT / Claude / other supported host)
                |
         MCP / host adapter
                |
        Universal MCP Gateway
           explicit allowlist
                |
           Adult MCP Server
                |
             AdultAgent
                |
         AdultSupervisor
                |
           AdultExecutor
            /          \
    AdultModelRouter  AdultEvaluator
        |                   |
    Text Adapter       Text/Image checks
    Image Adapters     Policy validation
        |
    OmniRoute API / verified local image backend
        |
    External providers or local backends

    Shared Workspace Memory Infrastructure
        |-- workspace namespace
        |-- adult namespace (restricted)
        |-- curated feedback publication path

    Adult operational state: .data/adult.sqlite3
    Encrypted payload/artifact files: .data/adult_payloads/

### 3.1 Storage separation

- Workspace memories and Adult memories use the existing shared SQLite memory infrastructure and one repository API with enforced namespace permissions.
- Adult task state, steps, attempts, checkpoints, events, feedback proposals, and model capability records use a separate SQLite operational database: .data/adult.sqlite3.
- Sensitive task inputs and retained artifacts are stored as encrypted files under .data/adult_payloads/; SQLite stores only opaque identifiers and metadata. For ephemeral input, the persisted reference is only a cache handle; it contains no payload bytes and becomes unresolved after process restart.
- API keys and encryption master keys remain in the private environment/secret store. Never write them to SQLite, logs, artifacts, Git, or MCP output.

This is shared infrastructure, not shared unrestricted data access.

### 3.2 Process and trust boundary

MCP tools are the public boundary. MCP callers never choose a storage namespace, principal, SQL query, absolute filesystem path, provider API key, or arbitrary adapter implementation. The Adult MCP server fixes its internal principal and delegates to the application service.

SQLite does not provide native row-level security. The MVP's storage boundary is therefore the scoped repository API, fixed server identities, SQL permission predicates, restrictive filesystem permissions, and the absence of any public raw-SQL tool. This protects against cross-Agent access through the supported interfaces; it is not a security boundary against arbitrary malicious Python code running as the same operating-system user. A later multi-user deployment would require a stronger process/database isolation boundary.

---

## 4. Domain types and state machine

### 4.1 Enumerations

AttemptStatus also includes unknown_outcome for a provider call whose final result cannot be established after a process/connection failure. This is not success and must not be silently replayed.

    TaskType = Literal[
        "text.analyze", "text.generate", "text.rewrite",
        "image.analyze", "image.generate", "image.edit",
    ]

    TaskStatus = Literal[
        "queued", "analyzing", "running", "evaluating",
        "checkpointed", "waiting_user", "needs_input",
        "paused", "completed", "failed", "cancelled",
    ]

    AttemptStatus = Literal["running", "succeeded", "failed", "rejected", "timed_out", "unknown_outcome"]
    PayloadRetention = Literal["ephemeral", "encrypted"]
    Decision = Literal["continue", "cancel"]
    CostClass = Literal["local", "free_verified", "paid", "unknown"]

All enum values must be validated at the service boundary and again before database transitions. Unknown values fail closed.

### 4.2 Task status semantics

| Status | Meaning | Permitted next action |
|---|---|---|
| queued | Accepted and awaiting execution | Claim and run |
| analyzing | Task requirements/policy are being analyzed | Continue bounded analysis |
| running | One executor lease is active | Complete/checkpoint or recover expired lease |
| evaluating | A produced result is being checked | Complete or record failure |
| checkpointed | Durable progress saved; more work remains | Explicit resume call |
| waiting_user | Five failed attempts reached; explicit user decision required | Continue or cancel |
| needs_input | Original ephemeral payload is unavailable after process restart | User re-supplies input or cancels |
| paused | Non-retryable failure, exhausted budget, or operator stop | Explicit corrective action only |
| completed | Output passed evaluation and was committed | Read result/artifacts |
| failed | Task cannot proceed or total budget exhausted | Terminal |
| cancelled | User cancelled the task | Terminal |

Terminal states: completed, failed, cancelled.

### 4.3 Required transitions

- queued -> analyzing -> running -> evaluating
- evaluating -> completed on accepted result.
- evaluating -> running only after recording a failed attempt and selecting a distinct eligible approach.
- Every completed step writes a checkpoint before another step starts.
- Every fifth failed attempt transitions to waiting_user, writes a checkpoint, releases the lease, and returns control to the caller. There is no automatic continuation.
- An explicit continue decision from waiting_user authorizes the next batch of at most five failures; it does not remove total budgets.
- A cancel decision transitions to cancelled.
- Expired execution leases are recoverable; an already-running provider call is not assumed to have completed. Use idempotency keys where the provider supports them and record uncertain outcomes.
- If an ephemeral payload has been lost after process restart, transition to needs_input; do not claim that full execution can resume without the original input.

---

## 5. Execution limits and retry policy

### 5.1 Defaults

| Limit | MVP default | Hard maximum |
|---|---:|---:|
| Failed attempts before asking user | 5 | fixed at 5 |
| Total attempts per task | 20 | 20 |
| Wall time per task | 600 seconds | 600 seconds |
| Model/provider calls per task | 20 | 20 |
| Concurrent active Adult tasks | 1 | 1 |
| Image input size | 15 MiB decoded | 15 MiB |
| Image output size | 20 MiB decoded | 20 MiB |
| MCP artifact chunk | 256 KiB decoded | 256 KiB |
| Task list page | 20 | 50 |
| Attempt history response | 20 records | 50 |

Defaults are configurable downward. Raising hard maxima requires a new spec revision and tests.

### 5.2 Zero-cost policy

- Default ADULT_ALLOW_PAID=false.
- Default task cost budget is 0 USD micro-units.
- Eligible routes must be local or free_verified; paid and unknown are rejected by default.
- A free-tier provider is not considered unlimited. Store the date and source of its cost/eligibility verification.
- Never enable a payment method, purchase credits, or silently route to a paid fallback.
- If provider cost cannot be determined or the configured route is not explicitly approved as zero-cost, stop before dispatch.
- Record provider-reported token usage and cost when available. Do not invent cost figures when the provider does not report them.

### 5.3 Approach de-duplication

Each attempted route receives an approach_key derived from:

- task type;
- adapter and provider identifiers;
- model identifier;
- canonical non-secret configuration fingerprint;
- input fingerprint;
- relevant environment/capability version.

A previously failed identical approach is skipped. It can be retried only if a material configuration/environment/input change is recorded with a non-empty retry_reason. Secrets must never be included in fingerprints or serialized configuration.

### 5.4 Failure classification

- Transient: timeout, connection reset, provider 429, provider 5xx, temporary local backend unavailable.
- Capability mismatch: provider/model lacks the required modality/operation; mark approach ineligible.
- Permanent request failure: invalid schema, unsupported input, corrupt image, invalid configuration.
- Policy rejection: stop; do not route around the policy result.
- Budget exhaustion: stop and persist paused or failed according to whether explicit user action can make progress.
- Unknown errors: sanitize, record a stable error code, and stop or try one distinct verified route only when classified as potentially recoverable.

No retry may bypass an upstream provider's safety controls. Policy rejections are terminal and do not count as retryable provider failures. Failed, timed_out, and unknown_outcome attempts increment the five-failure counter; unknown outcomes count conservatively because the provider may have processed the request. A policy rejection is not routed around. If the total-attempt/provider-call/time/cost cap is exhausted on the fifth failure, the task becomes terminal failed rather than asking for approval that cannot authorize more work.

---

## 6. Class and interface contracts

Public method signatures below are contractual. Internal helper names may change only if behavior and tests remain equivalent.

### 6.1 Data models — agent/adult/models.py

- AdultTaskRequest: task_type, instruction, payload_ref, input_fingerprint, retention_policy, requested_options.
- AdultTaskSnapshot: task_id, task_type, status, created_at, updated_at, attempt_count, failed_attempts_since_approval, current_step, selected_adapter, selected_model, selected_tool, last_error_code, latest_artifact_ids, user_action_required.
- TaskBudget: max_total_attempts, max_wall_seconds, max_provider_calls, max_cost_microusd, failure_checkpoint_interval.
- RouteCandidate: adapter_id, adapter_version, provider_id, model_id, model_version, tool_id, tool_version, task_type, cost_class, capability_version, verified_at.
- AttemptResult: attempt_id, status, output_ref, error_code, sanitized_error, usage, cost_microusd, elapsed_ms.
- EvaluationResult: accepted, score, checks, reason_codes, needs_revision.
- CheckpointSnapshot: versioned JSON-safe state; may contain identifiers/fingerprints/references, never plaintext sensitive payloads or credentials.

Use dataclasses or Pydantic models consistently with existing project conventions. All external JSON is validated before constructing these objects.

### 6.2 AdultAgent — agent/adult/adult_agent.py

Application facade. It must not contain provider-specific HTTP or SQL logic.

    class AdultAgent:
        def submit_text_task(
            self, task_type: TextTaskType, text: str, instruction: str = "",
            *, retain_payload: bool = False
        ) -> AdultTaskSnapshot: ...

        def submit_image_task(
            self, task_type: ImageTaskType, prompt: str,
            image_bytes: bytes | None = None, mime_type: str | None = None,
            *, retain_payload: bool = False
        ) -> AdultTaskSnapshot: ...

        def run_task(self, task_id: str, *, max_seconds: int = 300) -> SupervisorOutcome: ...
        def get_task(self, task_id: str) -> AdultTaskSnapshot: ...
        def list_tasks(self, status: str | None = None, limit: int = 20) -> list[AdultTaskSnapshot]: ...
        def get_attempts(self, task_id: str, limit: int = 20) -> list[AttemptSummary]: ...
        def resume_task(self, task_id: str, decision: Decision,
                        replacement_payload: bytes | str | None = None) -> SupervisorOutcome | AdultTaskSnapshot: ...
        def cancel_task(self, task_id: str) -> AdultTaskSnapshot: ...

It composes policy, payload storage, task store, executor, supervisor, and artifact store. The MCP server injects a single configured instance. It never accepts a caller-supplied principal, namespace, API key, filesystem path, model endpoint, or arbitrary tool name.

### 6.3 AdultTaskStore — agent/adult/adult_task_store.py

Owns all task/step/checkpoint/event transactions in .data/adult.sqlite3.

    class AdultTaskStore:
        def create_task(self, request: AdultTaskRequest, budget: TaskBudget) -> AdultTaskSnapshot: ...
        def get_task(self, task_id: str) -> AdultTaskSnapshot | None: ...
        def list_tasks(self, status: str | None, limit: int, cursor: str | None = None) -> TaskPage: ...
        def claim_task(self, task_id: str, worker_id: str, lease_seconds: int) -> TaskLease: ...
        def renew_lease(self, lease: TaskLease, lease_seconds: int) -> TaskLease: ...
        def save_step(self, task_id: str, step: StepRecord) -> None: ...
        def transition(self, task_id: str, expected: set[str], new_status: str, event: dict) -> None: ...
        def save_checkpoint(self, task_id: str, snapshot: CheckpointSnapshot, reason: str) -> Checkpoint: ...
        def set_waiting_user(self, task_id: str, snapshot: CheckpointSnapshot) -> None: ...
        def resume(self, task_id: str, decision: Decision) -> AdultTaskSnapshot: ...
        def request_cancel(self, task_id: str) -> AdultTaskSnapshot: ...
        def recover_expired_leases(self) -> int: ...

Requirements:

- Use atomic transactions for claim/state transitions/checkpoint creation.
- Compare expected current status to prevent stale writers.
- Only one active task lease is allowed in this single-user MVP.
- Lease owner and expiry are persisted.
- Never overwrite a newer checkpoint with an older snapshot.
- Keep task event metadata sanitized and size-bounded.
- Validate every transition against the state machine.

### 6.4 AdultAttemptTracker — agent/adult/adult_attempt_tracker.py

    class AdultAttemptTracker:
        def begin(self, task_id: str, step_id: str, route: RouteCandidate,
                  input_fingerprint: str, config_fingerprint: str) -> AttemptHandle: ...
        def succeed(self, handle: AttemptHandle, output_ref: str | None,
                    usage: dict | None, cost_microusd: int | None) -> AttemptResult: ...
        def fail(self, handle: AttemptHandle, error_code: str,
                 sanitized_error: str, retryable: bool) -> AttemptResult: ...
        def find_prior_approach(self, task_id: str, approach_key: str) -> AttemptSummary | None: ...
        def failures_since_approval(self, task_id: str) -> int: ...
        def total_attempts(self, task_id: str) -> int: ...

Must record start and terminal result in separate durable transactions so crashes do not erase evidence that an attempt began. A reclaimed uncertain attempt is marked timed_out or unknown_outcome; it is not silently counted as success.

### 6.5 AdultSupervisor — agent/adult/adult_supervisor.py

    class AdultSupervisor:
        def run_until_done(self, task_id: str, *,
                           max_attempts: int = 5,
                           max_seconds: int = 300) -> SupervisorOutcome: ...
        def continue_after_approval(self, task_id: str) -> SupervisorOutcome: ...

Responsibilities:

1. Load and validate persisted task state.
2. Check cancellation, deadline, total-attempt, provider-call, and cost budgets before each dispatch.
3. Ask the Executor for one bounded step/attempt.
4. Persist result and checkpoint after each finished step.
5. Evaluate failures and select a distinct eligible approach.
6. At exactly five failed attempts since the last approval, persist waiting_user, checkpoint, release lease, and return. It must not make a sixth attempt in the same call.
7. Continue only after an explicit resume/continue tool call.
8. Stop at the total cap, deadline, or budget limit.
9. Never spin in an unbounded loop or run in the background.

max_attempts passed to one invocation cannot override the five-failure approval threshold or the configured total cap.

### 6.6 AdultExecutor — agent/adult/adult_executor.py

    class AdultExecutor:
        def analyze_task(self, task: AdultTaskSnapshot) -> ExecutionPlan: ...
        def execute_next(self, task_id: str) -> StepOutcome: ...
        def execute_text(self, task_id: str, step: StepRecord) -> StepOutcome: ...
        def execute_image(self, task_id: str, step: StepRecord) -> StepOutcome: ...

The executor performs one bounded step at a time. It does not decide to exceed budgets, override policy, or install tools. Every provider call is wrapped by AttemptTracker. Outputs are staged as artifacts and committed only after evaluation passes.

### 6.7 AdultModelRouter and capability registry — agent/adult/adult_router.py

    class ModelCapabilityRegistry:
        def register(self, capability: ModelCapability) -> None: ...
        def list_eligible(self, task_type: str, cost_policy: CostPolicy) -> list[RouteCandidate]: ...
        def verify(self, provider_id: str, model_id: str, task_type: str,
                   evidence: CapabilityEvidence) -> ModelCapability: ...

    class AdultModelRouter:
        def choose_routes(self, task_type: str, input_metadata: dict,
                          excluded_approach_keys: set[str]) -> list[RouteCandidate]: ...
        def run_text(self, candidate: RouteCandidate, request: TextRequest) -> AdapterResult: ...
        def analyze_image(self, candidate: RouteCandidate, request: ImageAnalysisRequest) -> AdapterResult: ...
        def generate_image(self, candidate: RouteCandidate, request: ImageGenerationRequest) -> AdapterResult: ...
        def edit_image(self, candidate: RouteCandidate, request: ImageEditRequest) -> AdapterResult: ...

A model is eligible only when its explicit capability record covers the exact operation, input/output modality, size constraints, and cost policy. A name appearing in a model catalog is not proof of capability.

### 6.8 Adapter protocols — agent/adult/integrations/

    class TextModelAdapter(Protocol):
        def invoke(self, request: TextRequest) -> AdapterResult: ...

    class ImageUnderstandingAdapter(Protocol):
        def analyze(self, request: ImageAnalysisRequest) -> AdapterResult: ...

    class ImageGenerationAdapter(Protocol):
        def generate(self, request: ImageGenerationRequest) -> ArtifactResult: ...

    class ImageEditingAdapter(Protocol):
        def edit(self, request: ImageEditRequest) -> ArtifactResult: ...

Required implementations:

- OmniRouteChatAdapter: wraps/reuses existing agent.model.load_chat_model behavior.
- OmniRouteImageAdapter: typed HTTP adapter for verified image-generation/edit endpoints. It uses bearer authentication from server-side configuration only.
- LocalImageAdapter: optional adapter interface for a verified local image backend; disabled unless installed and explicitly configured. No local package installation is performed by Adult Agent.

OmniRoute integration contract:

- Configured API root ends in /v1; API key is server-side.
- Chat: POST /chat/completions through the base URL.
- Image generation: POST /images/generations.
- Image edit: POST /images/edits using multipart form data where supported.
- Model discovery: GET /models.
- Do not embed keys in URLs or log authorization headers.
- Capability and request support must be checked against the deployed OmniRoute version and the specific provider/model. The live model catalog and verified capability records are authoritative.
- Do not implement video endpoints in MVP.

### 6.9 AdultEvaluator — agent/adult/adult_evaluator.py

    class AdultEvaluator:
        def evaluate_text(self, request: TextRequest, result: AdapterResult) -> EvaluationResult: ...
        def evaluate_image(self, request: ImageRequest, artifact: ArtifactResult) -> EvaluationResult: ...

Deterministic checks are mandatory and run before any optional model-based reviewer:

- Text: non-empty output, output bounds, required format/structure, policy result, and task-specific acceptance checks.
- Image: decodable image, allowed MIME, bounded bytes/dimensions/pixel count, non-empty artifact, expected operation, and policy result.
- A provider HTTP 200 is not by itself task success.
- Optional model-based review counts against model-call and cost budgets.
- Evaluation failure becomes a recorded attempt outcome; it does not silently publish an artifact as final.

### 6.10 AdultMemory — agent/adult/adult_memory.py

    class AdultMemory:
        def remember_private(self, content: str, memory_type: str,
                             task_id: str | None = None, metadata: dict | None = None) -> MemoryRef: ...
        def recall_private(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...

This is a scoped facade. It must not expose arbitrary namespace selection. Private Adult memory receives only concise reusable strategies/outcomes, not raw prompts or image bytes by default.

### 6.11 AdultPolicies — agent/adult/adult_policies.py

    class AdultPolicies:
        def validate_text_task(self, task_type: str, instruction: str, text: str) -> PolicyDecision: ...
        def validate_image_task(self, task_type: str, prompt: str,
                                mime_type: str | None, image_metadata: dict | None) -> PolicyDecision: ...
        def validate_publication(self, sanitized_feedback: dict) -> PolicyDecision: ...

Policy checks are invoked before routing and again before final artifact publication. Policies are independent of provider routing and cannot be disabled by model output, user-provided parameters, or MCP arguments.

### 6.12 Feedback publication — agent/adult/adult_feedback.py

    class AdultFeedback:
        def build_proposal(self, task_id: str) -> FeedbackProposal | None: ...
        def sanitize(self, proposal: FeedbackProposal) -> SanitizedFeedback: ...
        def publish(self, feedback_id: str) -> PublishResult: ...

build_proposal reads only the minimum task/attempt metadata needed to extract a generic lesson. sanitize removes raw task content, image data, identifying details, secrets, and local paths. publish uses only the injected feedback_publisher_scope and writes shareable Workspace facts after schema/policy validation. Provider/tool installation remains a proposal and is never part of publish.

### 6.13 Payload and artifact storage

PayloadCipher and AdultArtifactStore are separate from memory:

    class PayloadCipher:
        def encrypt(self, task_id: str, payload_id: str, plaintext: bytes) -> EncryptedPayload: ...
        def decrypt(self, task_id: str, payload_id: str, ciphertext: bytes) -> bytes: ...

    class TransientPayloadCache:
        def put(self, task_id: str, payload: bytes) -> str: ...
        def get(self, task_id: str, payload_ref: str) -> bytes | None: ...
        def discard(self, task_id: str) -> None: ...

    class AdultArtifactStore:
        def save_input(self, task_id: str, data: bytes, mime_type: str,
                       *, retain_encrypted: bool) -> PayloadRef | None: ...
        def load_input(self, payload_id: str, task_id: str) -> bytes: ...
        def save_output(self, task_id: str, attempt_id: str, data: bytes,
                        mime_type: str) -> ArtifactRef: ...
        def read_chunk(self, artifact_id: str, offset: int, max_bytes: int = 262144) -> ArtifactChunk: ...
        def delete_expired(self) -> int: ...

TransientPayloadCache holds non-retained input only in process memory. It is never serialized to SQLite or logs. A task submitted with ephemeral retention can resume while the cache survives; after process restart, a missing payload transitions to needs_input.

Encryption requirements:

- Use a maintained cryptographic library; do not implement cryptographic primitives.
- Use authenticated encryption (AES-GCM or an equivalent vetted construction) with a random nonce.
- Derive separate encryption and fingerprint keys from a master key using a standard KDF with distinct context labels.
- Authenticate task_id, payload_id, and schema version as associated data.
- Store key identifier/version, never the key, in SQLite.
- Verify authentication before returning plaintext.
- If encryption is enabled but the key is missing/invalid, fail before accepting a persistent task.
- Default retain_payload=false: never persist the original sensitive input. The process may keep it transiently while executing.
- retain_payload=true requires explicit request opt-in and a valid local master key. Retained input is encrypted at rest and removed at task completion or expiry according to the retention setting.
- Output artifacts needed by the user are encrypted at rest and retained for a bounded default of 24 hours; longer retention must be explicit. Persisted output requires the same configured master key. When no key is configured, output bytes may be held in the transient process cache for immediate bounded artifact retrieval, but are not persisted across process restarts. Never write unencrypted output files to simulate persistence.
- On ephemeral payload loss after restart, mark needs_input and request re-upload. Do not fabricate a resumed task.

---

## 7. SQLite data model

### 7.1 Databases

- Existing memory database: .data/memory.sqlite3, configured by WORKSPACE_MEMORY_DB.
- New Adult operational database: .data/adult.sqlite3, configured by ADULT_STATE_DB.
- Encrypted payload/artifact root: .data/adult_payloads/, configured by ADULT_PAYLOAD_DIR.

Enable foreign keys on every connection. Use parameterized SQL only. Use explicit transactions for state transitions and checkpoint writes. Set a bounded busy timeout. WAL mode may be enabled only after the test suite verifies the Codespaces filesystem and backup/restore behavior; do not assume WAL files are disposable. Backups must account for the main DB and WAL state.

### 7.2 Memory migration

Existing memories(id, content, tags, created_at) rows must remain readable to existing Workspace APIs after migration.

Add the following logical columns using an additive versioned migration:

| Column | Type | Default | Purpose |
|---|---|---|---|
| namespace | TEXT NOT NULL | workspace | Data ownership boundary |
| memory_type | TEXT NOT NULL | legacy | fact/strategy/outcome/etc. |
| visibility | TEXT NOT NULL | internal | internal/shareable |
| sensitivity | TEXT NOT NULL | unknown | normal/sensitive/unknown |
| task_id | TEXT NULL | NULL | Optional originating Adult task |
| content_hash | TEXT NULL | NULL | Optional keyed fingerprint |
| metadata_json | TEXT NOT NULL | {} | Sanitized metadata |
| owner_agent | TEXT NOT NULL | workspace | Writer identity |

Indexes: (namespace, created_at DESC), (namespace, memory_type, created_at DESC), and (task_id).

Legacy rows are migrated to namespace='workspace', visibility='internal', sensitivity='unknown', memory_type='legacy'. They must not become shareable to Adult Agent automatically. Existing Workspace remember/recall/recent operations continue to work within the Workspace namespace.

Legacy live-data migration is never triggered automatically on import, application startup, or MemoryStore construction. A version-0 existing database raises MemoryMigrationRequired without modification. The only supported live migration path is the explicit command .venv/bin/python -m agent.memory_migrations backup-and-migrate; it requires --confirm, creates and validates a non-overwriting SQLite online backup before migration, and verifies preserved rows/integrity afterward. The full operational contract is in ADULT_AGENT_STAGE2_IMPLEMENTATION_SPEC.md.

Create memory_namespace_acl with columns:

- principal TEXT NOT NULL
- namespace TEXT NOT NULL
- can_read INTEGER NOT NULL CHECK(can_read IN (0,1))
- can_write INTEGER NOT NULL CHECK(can_write IN (0,1))
- can_read_shareable INTEGER NOT NULL CHECK(can_read_shareable IN (0,1))
- primary key (principal, namespace)

Seed fixed policies:

| Principal | Namespace | Read | Write | Shareable read |
|---|---|---:|---:|---:|
| workspace_agent | workspace | 1 | 1 | 0 |
| workspace_agent | adult | 0 | 0 | 0 |
| adult_agent | adult | 1 | 1 | 0 |
| adult_agent | workspace | 0 | 0 | 1 |
| feedback_publisher | workspace | 0 | 1 | 0 |
| feedback_publisher | adult | 0 | 0 | 0 |

The Adult MCP server fixes principal adult_agent; existing Workspace memory MCP fixes workspace_agent; only the internal feedback publisher can write curated shareable facts. Existing MemoryStore direct callers must resolve to the fixed Workspace scope for backward compatibility; Adult code receives only its injected scoped facade.

Storage interfaces are refined by the dedicated [Stage 2 implementation specification](ADULT_AGENT_STAGE2_IMPLEMENTATION_SPEC.md):

    class MemoryStore:
        def workspace_scope(self) -> ScopedMemoryStore: ...
        def adult_scope(self) -> ScopedMemoryStore: ...
        def feedback_publisher_scope(self) -> FeedbackPublisherStore: ...

    class ScopedMemoryStore:
        def remember(self, content: str, tags: list[str] | None = None,
                      memory_type: str = "fact", task_id: str | None = None,
                      metadata: dict | None = None) -> MemoryRef: ...
        def recall(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent(self, limit: int = 10) -> list[MemoryRecord]: ...
        def recall_owned(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent_owned(self, limit: int = 10) -> list[MemoryRecord]: ...
        def recall_shareable_workspace(self, query: str, limit: int = 5) -> list[MemoryRecord]: ...
        def recent_shareable_workspace(self, limit: int = 10) -> list[MemoryRecord]: ...

    class FeedbackPublisherStore:
        def publish_sanitized_feedback(self, feedback: SanitizedFeedback) -> MemoryRef: ...

FeedbackPublisherStore is a separate write-only, schema-constrained capability. It must not expose generic remember, recall, recent, or raw SQL operations. This avoids turning a general-purpose remember(content) method into an unrestricted shareable-publication path.

Scope factories are internal application APIs. The MCP layer receives a pre-scoped instance; it cannot request another scope or change visibility/namespace. No public method or MCP schema accepts a caller-selected principal or namespace.

Every memory read/write query must apply the ACL predicate in SQL and validate the requested operation. Adult workspace recall returns only visibility='shareable' rows. Existing MemoryStore direct callers remain Workspace-scoped and retain their legacy return shapes. The existing Workspace memory MCP tool names and argument contracts do not change. AdultMemory receives only adult_scope(); feedback code receives only feedback_publisher_scope(). Do not expose a generic from-string principal factory to MCP callers.

Add memory_access_audit(id, principal, operation, namespace, memory_id, created_at). Audit metadata must not contain content or query text.

### 7.3 Adult operational tables

The following is the logical full-MVP schema. Names and fields are mandatory; implementation may add non-sensitive operational columns. The stage-specific DDL, additional constraints, migration ordering, and TaskStore transaction rules in [Stage 3 implementation specification](ADULT_AGENT_STAGE3_IMPLEMENTATION_SPEC.md) are normative for Stage 3. Attempt, payload, artifact, capability, and feedback tables are created only in their assigned later stages.

    CREATE TABLE adult_tasks (
      task_id TEXT PRIMARY KEY,
      task_type TEXT NOT NULL CHECK(task_type IN ('text.analyze','text.generate','text.rewrite','image.analyze','image.generate','image.edit')),
      status TEXT NOT NULL CHECK(status IN ('queued','analyzing','running','evaluating','checkpointed','waiting_user','needs_input','paused','completed','failed','cancelled')),
      input_fingerprint TEXT NOT NULL,
      instruction_fingerprint TEXT,
      payload_id TEXT,
      retention_policy TEXT NOT NULL CHECK(retention_policy IN ('ephemeral','encrypted')),
      current_step_id TEXT,
      selected_adapter TEXT,
      selected_provider TEXT,
      selected_model TEXT,
      selected_tool TEXT,
      latest_result_artifact_id TEXT,
      failed_attempts_since_approval INTEGER NOT NULL DEFAULT 0,
      total_attempts INTEGER NOT NULL DEFAULT 0,
      provider_calls INTEGER NOT NULL DEFAULT 0,
      approvals_granted INTEGER NOT NULL DEFAULT 0,
      max_total_attempts INTEGER NOT NULL,
      max_wall_seconds INTEGER NOT NULL,
      max_provider_calls INTEGER NOT NULL,
      max_cost_microusd INTEGER NOT NULL DEFAULT 0,
      spent_cost_microusd INTEGER NOT NULL DEFAULT 0,
      deadline_at TEXT NOT NULL,
      lease_owner TEXT,
      lease_expires_at TEXT,
      last_error_code TEXT,
      user_action_required INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE adult_steps (
      step_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      sequence_no INTEGER NOT NULL,
      operation TEXT NOT NULL,
      status TEXT NOT NULL,
      adapter_id TEXT,
      provider_id TEXT,
      model_id TEXT,
      tool_id TEXT,
      input_payload_id TEXT,
      output_artifact_id TEXT,
      config_fingerprint TEXT,
      started_at TEXT,
      completed_at TEXT,
      UNIQUE(task_id, sequence_no)
    );

    CREATE TABLE adult_attempts (
      attempt_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      step_id TEXT REFERENCES adult_steps(step_id),
      attempt_no INTEGER NOT NULL,
      approach_key TEXT NOT NULL,
      adapter_id TEXT NOT NULL,
      adapter_version TEXT,
      provider_id TEXT,
      model_id TEXT,
      model_version TEXT,
      tool_id TEXT,
      tool_version TEXT,
      input_fingerprint TEXT NOT NULL,
      config_fingerprint TEXT NOT NULL,
      environment_fingerprint TEXT,
      status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','rejected','timed_out','unknown_outcome')),
      retryable INTEGER NOT NULL DEFAULT 0,
      retry_reason TEXT,
      error_code TEXT,
      sanitized_error TEXT,
      output_fingerprint TEXT,
      output_artifact_id TEXT,
      output_summary TEXT,
      usage_json TEXT NOT NULL DEFAULT '{}',
      cost_microusd INTEGER,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      elapsed_ms INTEGER,
      UNIQUE(task_id, attempt_no)
    );

    CREATE TABLE adult_checkpoints (
      checkpoint_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      revision INTEGER NOT NULL,
      reason TEXT NOT NULL,
      snapshot_json TEXT NOT NULL,
      failed_attempts_since_approval INTEGER NOT NULL,
      user_action_required INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      UNIQUE(task_id, revision)
    );

    CREATE TABLE adult_payloads (
      payload_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      payload_kind TEXT NOT NULL,
      storage_key TEXT NOT NULL UNIQUE,
      mime_type TEXT NOT NULL,
      byte_length INTEGER NOT NULL,
      plaintext_hmac TEXT NOT NULL,
      encryption_version TEXT NOT NULL,
      key_id TEXT NOT NULL,
      expires_at TEXT,
      created_at TEXT NOT NULL
    );

    CREATE TABLE adult_artifacts (
      artifact_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      attempt_id TEXT REFERENCES adult_attempts(attempt_id),
      artifact_kind TEXT NOT NULL,
      storage_key TEXT NOT NULL UNIQUE,
      mime_type TEXT NOT NULL,
      byte_length INTEGER NOT NULL,
      sha256 TEXT NOT NULL,
      encrypted INTEGER NOT NULL,
      encryption_version TEXT,
      key_id TEXT,
      expires_at TEXT,
      created_at TEXT NOT NULL
    );

    CREATE TABLE adult_task_events (
      event_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      event_type TEXT NOT NULL,
      from_status TEXT,
      to_status TEXT,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL
    );

    CREATE TABLE adult_feedback_proposals (
      feedback_id TEXT PRIMARY KEY,
      task_id TEXT REFERENCES adult_tasks(task_id) ON DELETE RESTRICT,
      feedback_type TEXT NOT NULL,
      sanitized_summary TEXT NOT NULL,
      recommendation_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL,
      source_fingerprint TEXT NOT NULL,
      created_at TEXT NOT NULL,
      published_at TEXT
    );

    CREATE TABLE adult_model_capabilities (
      capability_id TEXT PRIMARY KEY,
      provider_id TEXT NOT NULL,
      model_id TEXT NOT NULL,
      task_type TEXT NOT NULL,
      capabilities_json TEXT NOT NULL,
      cost_class TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0,
      verified_at TEXT,
      evidence_uri TEXT,
      UNIQUE(provider_id, model_id, task_type)
    );

    CREATE INDEX idx_adult_tasks_status_updated ON adult_tasks(status, updated_at DESC);
    CREATE UNIQUE INDEX idx_adult_single_active_task ON adult_tasks((1))
      WHERE status IN ('analyzing','running','evaluating');
    CREATE INDEX idx_adult_attempts_task_no ON adult_attempts(task_id, attempt_no);
    CREATE INDEX idx_adult_attempts_approach ON adult_attempts(task_id, approach_key);
    CREATE INDEX idx_adult_checkpoints_task_revision ON adult_checkpoints(task_id, revision DESC);
    CREATE INDEX idx_adult_artifacts_task ON adult_artifacts(task_id, created_at);
    CREATE INDEX idx_adult_events_task ON adult_task_events(task_id, created_at);

### 7.4 Checkpoint contents

snapshot_json must be versioned and must preserve:

- task ID, task type, status, and active step;
- input fingerprint and payload reference (encrypted record ID or opaque in-memory cache handle; never plaintext);
- current plan and completed/pending step IDs;
- last successful result/artifact reference;
- last selected adapter, provider, model, tool, and sanitized configuration fingerprint;
- attempted approach_key values and exclusions;
- failure count since approval and total attempts;
- provider calls, recorded cost, deadline, and remaining budgets;
- last error code and sanitized summary;
- pending user decision and allowed decisions;
- checkpoint schema version.

Checkpoint write and task status transition to waiting_user must be one transaction. Raw prompts, image bytes, authorization headers, API keys, and arbitrary provider request bodies are prohibited from checkpoint JSON.

### 7.5 Migration and integrity rules

- Use a migration ledger with monotonically increasing schema versions.
- Take a consistent backup before migrating existing memory data.
- Apply migrations in transactions where SQLite permits.
- Never drop/recreate the existing memories table.
- Verify row counts, IDs, tags, timestamps, and Workspace recall before/after migration.
- Enable foreign keys on every connection.
- Validate JSON at application boundaries and use parameterized SQL.
- Add integrity checks for foreign keys and database corruption to the test suite.
- Do not delete old memories or task history as part of schema migration.

---

## 8. MCP interface contracts

The MCP server module is agent/adult/adult_mcp_server.py, registered by build_workspace_server_config only when ENABLE_ADULT_AGENT=true. Default is disabled.

Tool names after Gateway prefixing use adult__. The external caller does not get direct access to MemoryStore, AdultTaskStore SQL, provider credentials, or filesystem paths.

### 8.1 adult_submit_text_task

Input schema:

    {
      "type": "object",
      "additionalProperties": false,
      "required": ["task_type", "text"],
      "properties": {
        "task_type": {
          "type": "string",
          "enum": ["text.analyze", "text.generate", "text.rewrite"]
        },
        "text": {"type": "string", "minLength": 1, "maxLength": 20000},
        "instruction": {"type": "string", "maxLength": 4000, "default": ""},
        "retain_payload": {"type": "boolean", "default": false}
      }
    }

Behavior: validates size and policy, fingerprints content, applies retention rules, creates a queued task, and returns a redacted task snapshot. It does not run an unbounded workflow inside the submission call.

### 8.2 adult_submit_image_task

Input schema:

    {
      "type": "object",
      "additionalProperties": false,
      "required": ["task_type", "prompt"],
      "properties": {
        "task_type": {
          "type": "string",
          "enum": ["image.analyze", "image.generate", "image.edit"]
        },
        "prompt": {"type": "string", "maxLength": 4000, "default": ""},
        "image_base64": {"type": "string", "maxLength": 20971520},
        "image_mime_type": {
          "type": "string",
          "enum": ["image/png", "image/jpeg", "image/webp"]
        },
        "retain_payload": {"type": "boolean", "default": false}
      }
    }

Validation rules:

- image.analyze and image.edit require a valid input image.
- image.generate does not accept an input image.
- image.generate and image.edit require a non-empty prompt; image.analyze may use an empty prompt when the caller only requests a visual description.
- Decode base64 strictly; enforce 15 MiB decoded-size limit.
- Verify actual file signature and successful image decoding; do not trust the declared MIME type.
- Reject SVG, executable/polyglot payloads, malformed files, and dimensions/pixel counts above configured bounds.
- Return task ID and redacted metadata, never echo the base64 payload.

### 8.3 adult_run_task

Input: task_id and max_seconds (1–300; default 300).

Behavior: starts a queued task or continues a checkpointed task only when the caller has explicitly initiated that run. It invokes one bounded AdultSupervisor segment and returns the resulting task snapshot. It stops at completion, the five-failure approval point, or any configured budget. It never starts background execution. A waiting_user task cannot be run by this tool; it requires adult_resume_task with decision=continue.

### 8.4 adult_get_task

Input: task_id UUID.

Output: task ID/type/status, timestamps, counters, current operation, selected adapter/model/tool, latest artifact IDs, safe error code, and whether user action/input is required. Must not return raw text, image bytes, private memory, secrets, or encrypted payload data.

### 8.5 adult_list_tasks

Input: optional status, limit (1–20), optional opaque cursor. Output: compact task summaries only. No payload content.

### 8.6 adult_get_attempts

Input: task_id, optional limit (1–20). Output: attempt number, adapter/model/tool identifiers, status, elapsed time, sanitized error code/summary, cost/usage when known, and whether an approach was skipped as duplicate. No raw prompt, content, image, credentials, or provider request body.

### 8.7 adult_resume_task

Input:

    {
      "type": "object",
      "additionalProperties": false,
      "required": ["task_id", "decision"],
      "properties": {
        "task_id": {"type": "string"},
        "decision": {"type": "string", "enum": ["continue", "cancel"]},
        "replacement_text": {"type": "string", "maxLength": 20000},
        "replacement_image_base64": {"type": "string", "maxLength": 20971520},
        "replacement_image_mime_type": {
          "type": "string",
          "enum": ["image/png", "image/jpeg", "image/webp"]
        }
      }
    }

Rules:

- Only tasks in waiting_user, checkpointed, paused (where a defined correction is allowed), or needs_input may be resumed.
- continue is an explicit authorization for one bounded continuation segment, not unlimited execution. The AdultAgent must call AdultSupervisor.continue_after_approval and return that bounded outcome; it must not merely change a database flag and leave execution semantics implicit. The MCP tool returns the supervisor outcome (including updated task snapshot); cancel returns the cancelled task snapshot without dispatch.
- If state is needs_input, require the missing payload again and verify task type/MIME/size.
- cancel transitions to cancelled; it does not call a model.
- Never resume a completed, failed-terminal, or cancelled task.

### 8.8 adult_cancel_task

Input: task ID. Sets a cancellation request atomically. An in-flight provider call may not be cancellable; after it returns, the executor must discard/unpublish output when cancellation has been requested and mark the task cancelled.

### 8.9 adult_get_artifact_chunk

Input: artifact_id, offset (integer >= 0), max_bytes (1–262144, default 262144).

Output: base64 chunk, decoded byte count, total byte count, next offset, MIME type, artifact SHA-256, and completion flag. It supports both text/plain output artifacts and validated image output artifacts. Validate artifact ownership, expiry, and range before reading. Never accept a path from the caller. This chunk contract avoids unbounded MCP responses and allows an external host to reconstruct the artifact.

### 8.10 MCP output and error contract

Every successful tool call returns bounded structured JSON with a stable status and IDs. Validation/policy failures return a clear structured error and do not create an attempt unless provider execution actually began. Provider attempts always receive durable attempt records. Never return stack traces, environment variables, secrets, local absolute paths, or raw sensitive content.

Adult MCP tools are stateful through explicit IDs, not implicit MCP sessions. MCP tool input/output schemas must be strict and have additionalProperties=false where supported. The Gateway allowlist must expose only the nine named Adult tools: adult_submit_text_task, adult_submit_image_task, adult_run_task, adult_get_task, adult_list_tasks, adult_get_attempts, adult_resume_task, adult_cancel_task, and adult_get_artifact_chunk. No generic memory, SQL, filesystem, terminal, or arbitrary provider management tool is added.

---

## 9. Text and image data flows

### 9.1 Text

1. MCP caller submits a text task.
2. AdultAgent validates size and AdultPolicies.
3. Create HMAC fingerprint of original input using a dedicated derived key.
4. If retain_payload=false, keep original input only in transient process memory; persist only metadata/fingerprint. If true, require encryption key and save encrypted payload.
5. AdultTaskStore creates task and first step.
6. AdultSupervisor acquires lease and checks all budgets.
7. AdultExecutor constructs operation-specific request.
8. AdultModelRouter selects a verified text-capable route.
9. AttemptTracker writes attempt-start record before provider dispatch.
10. Adapter invokes provider; raw request/response bodies are not logged.
11. AttemptTracker records sanitized outcome and usage.
12. AdultEvaluator validates output.
13. Persist encrypted output artifact if required; save checkpoint and update task.
14. On success, return artifact/result summary. On failure, exclude duplicate approach and continue only within budget.
15. At five failures, save checkpoint, set waiting_user, and return control.

### 9.2 Image

1. MCP caller submits image task and optional input image.
2. Strict base64, file signature, decode, MIME, dimensions, pixel, and byte checks run locally.
3. AdultPolicies validates task type and context before routing.
4. Retention policy is applied; image bytes never enter normal memory or attempt/event logs.
5. ModelCapabilityRegistry supplies candidates with the exact required image capability.
6. AdultModelRouter selects an image understanding/generation/edit adapter.
7. AttemptTracker records an attempt and input fingerprint.
8. Adapter calls the verified route or configured local backend.
9. ArtifactStore validates, encrypts, and stages output.
10. AdultEvaluator checks decodability, MIME, size, dimensions, and task-specific success.
11. Only accepted output is committed and made available through artifact chunk retrieval.
12. Failure is recorded and routed to a distinct eligible approach, subject to limits and the five-failure prompt.

### 9.3 Workspace feedback

1. Adult task ends or produces a reusable failure/success lesson.
2. Feedback builder extracts a minimal structured lesson: capability gap, error class, adapter/model version, sanitized remediation, and source fingerprint.
3. Remove user text, image contents, recognizable person data, provider secrets, local paths, and unnecessary unique identifiers.
4. Validate the result against a strict feedback schema.
5. Store an Adult-private feedback record and a publication proposal.
6. Only sanitized generic lessons may be published via the fixed feedback_publisher capability into Workspace's shareable namespace.
7. New provider/model/tool recommendations remain proposals. Installation, enabling, or credential configuration requires a separate explicit user action.
8. Other Workspace Agents can read only published shareable facts, not private Adult task/attempt records.

---

## 10. Feedback schema

A shareable lesson must contain only:

- category: capability_gap / transient_failure / quality_issue / compatibility_issue / strategy;
- component_id: non-secret normalized adapter/provider/model identifier;
- version: if known;
- operation: task type;
- failure_code: normalized code;
- lesson: concise generic explanation;
- recommended_action: bounded text;
- verification_status: observed / reproduced / unverified;
- source_fingerprint: keyed source/task fingerprint;
- created_at.

Never publish original task request, source image, generated image, raw provider response, user-identifying details, or unredacted logs.

---

## 11. Configuration contract

New environment variables (document in .env.example; never put values in Git):

| Variable | Default | Meaning |
|---|---|---|
| ENABLE_ADULT_AGENT | false | Register Adult MCP server |
| ADULT_STATE_DB | .data/adult.sqlite3 | Adult operational database |
| ADULT_PAYLOAD_DIR | .data/adult_payloads | Encrypted payload/artifact root |
| ADULT_PAYLOAD_ENCRYPTION_KEY | empty | Local master encryption key; required for retained inputs and persisted outputs |
| ADULT_ALLOW_PAID | false | Paid routes disabled by default |
| ADULT_MAX_TOTAL_ATTEMPTS | 20 | Per-task hard attempt cap |
| ADULT_MAX_WALL_SECONDS | 600 | Per-task wall-time cap |
| ADULT_MAX_PROVIDER_CALLS | 20 | Per-task provider-call cap |
| ADULT_MAX_COST_MICROUSD | 0 | Per-task cost cap |
| ADULT_FAILURES_BEFORE_APPROVAL | 5 | Fixed five-failure checkpoint interval; startup rejects any value other than 5 |
| ADULT_OUTPUT_RETENTION_HOURS | 24 | Default encrypted output retention |
| ADULT_MAX_IMAGE_INPUT_BYTES | 15728640 | Maximum decoded input image bytes |
| ADULT_MAX_IMAGE_OUTPUT_BYTES | 20971520 | Maximum decoded output image bytes |
| ADULT_OMNIROUTE_BASE_URL | empty | Verified OmniRoute API root ending in /v1 |
| ADULT_OMNIROUTE_API_KEY | empty | Server-side OmniRoute key; do not expose to tools |

Rules:

- Empty optional model configuration means offline mode, not a crash during import.
- ADULT_PAYLOAD_ENCRYPTION_KEY is optional for non-retained ephemeral tasks; when set, it protects retained input and persisted output. If it is absent, retain_payload=true is rejected and output artifacts are memory-only.
- Incomplete OmniRoute configuration fails closed only when an OmniRoute route is selected.
- No key values are accepted through MCP tools.
- No paid route may run merely because an API key is configured.
- Capabilities are configured/verified explicitly; GET /v1/models is discovery, not proof of task capability.
- .env.example contains variable names and safe defaults only.

---

## 12. File layout and ownership

New modules:

    agent/adult/
      __init__.py
      models.py
      adult_agent.py
      adult_mcp_server.py
      adult_task_store.py
      adult_attempt_tracker.py
      adult_supervisor.py
      adult_executor.py
      adult_router.py
      adult_evaluator.py
      adult_memory.py
      adult_policies.py
      adult_feedback.py
      payload_store.py
      capability_registry.py
      integrations/
        __init__.py
        protocols.py
        omniroute_chat.py
        omniroute_image.py
        local_image.py
      tools/
        text/
          __init__.py
          text_operations.py
        image/
          __init__.py
          image_operations.py

    tests/
      test_adult_models.py
      test_adult_task_store.py
      test_adult_attempt_tracker.py
      test_adult_supervisor.py
      test_adult_executor.py
      test_adult_memory.py
      test_adult_router.py
      test_adult_omniroute_adapters.py
      test_adult_payload_store.py
      test_adult_policies.py
      test_adult_mcp_server.py
      test_adult_end_to_end.py

    docs/
      ADULT_AGENT_IMPLEMENTATION_SPEC.md
      ADULT_AGENT_STAGE2_IMPLEMENTATION_SPEC.md
      ADULT_AGENT_STAGE3_IMPLEMENTATION_SPEC.md
      ADULT_AGENT_SETUP.md
      ADULT_AGENT_TESTING.md

Responsibilities must remain separate:

- MCP server: validate transport input, bind internal services, return structured output.
- AdultAgent: application facade.
- TaskStore: operational persistence and transitions.
- AttemptTracker: append-only attempt evidence.
- Supervisor: bounded retry/approval/checkpoint logic.
- Executor: one task step at a time.
- Router/Registry: verified capability and cost-aware selection.
- Adapters: provider-specific wire contracts.
- Evaluator: acceptance checks.
- Memory: scoped long-lived knowledge only.
- PayloadStore: encrypted transient/retained inputs and artifacts.
- Policies: independent gate before execution and publication.
- Feedback: sanitization and explicit publication path.

---

## 13. Implementation stages and mandatory gates

No stage may begin until the previous stage's review gate passes. If any test or review finds a regression, fix it in the current stage, rerun the focused tests and the complete suite, and repeat until green. Do not skip, waive, or relabel a failing test as a warning.

### Stage 0 — Baseline and branch

- Confirm branch is based on main.
- Confirm working-tree state in Codespace before local operations.
- Run and record the existing full unit test suite.
- Record dependency/security workflow baseline.
- Do not modify .vscode/, .env, or existing user data.

**Gate:** clean branch baseline, test output recorded, and no unexpected diff.

### Stage 1 — Specification

- Commit this specification only.
- Review every interface, state, schema, security boundary, and acceptance test.
- Check Markdown formatting, internal file references, SQL syntax/consistency, enum consistency, and MCP schema validity.
- Run repository CI if triggered by the documentation commit; inspect every job.

**Gate:** spec document verified from the branch and CI green. No implementation files changed.

### Stage 2 — Memory migration and isolation

The complete execution contract is maintained in [ADULT_AGENT_STAGE2_IMPLEMENTATION_SPEC.md](ADULT_AGENT_STAGE2_IMPLEMENTATION_SPEC.md). It defines the MemoryStore and scoped-store interfaces, additive migration ledger, ACL matrix and SQL predicates, write-only sanitized feedback publisher, audit schema, data flows, compatibility rules, and MEM-* acceptance tests.

Implementation order: typed records/validation; temporary-database migration; ACL/audit schema; fixed scope factories; backward-compatible Workspace wrappers/MCP; AdultMemory; migration/isolation/audit/regression tests.

**Gate:** every MEM-* acceptance test (including MEM-MIG-11/12), all unchanged Workspace memory tests, the full unit suite, protected-path scan, and CI pass. The live .data/memory.sqlite3 remains untouched until the explicitly confirmed backup-and-migrate command is run and its verified backup/restore procedure is complete.

### Stage 3 — Adult operational schema and TaskStore

The complete execution contract is maintained in [ADULT_AGENT_STAGE3_IMPLEMENTATION_SPEC.md](ADULT_AGENT_STAGE3_IMPLEMENTATION_SPEC.md). It defines the Adult operational DB and migration ledger, exact Stage 3 tables and constraints, immutable task history, DTOs, task state machine, optimistic state versions, lease fencing, atomic checkpoints, cancellation, resume semantics, expired-lease recovery, and TASK-* acceptance tests.

Implementation order: DTO validation; DB connection/migration; operational schema; create/get/list; state transitions; lease claim/renew/release; checkpoints/waiting-user/resume/cancel/recovery; concurrency/restart/privacy tests.

**Gate:** every TASK-* test, all Stage 2 MEM-* tests, and the full suite pass. Stage 3 must not make model/provider calls, register Adult MCP tools, persist payload plaintext, implement AttemptTracker, or add encrypted payload/artifact storage. Those belong to later stages.

### Stage 4 — Encrypted payload and artifact storage

- Add authenticated encryption, key derivation, strict file permissions, atomic file writes, retention and deletion.
- Default raw input retention remains off.
- Test tampering, wrong key, missing key, partial writes, path traversal, expiry, and restart.

**Gate:** security tests and full suite green; no plaintext payload appears in DB, logs, or checkpoints.

### Stage 5 — AttemptTracker and retry de-duplication

- Persist attempt start/completion/failure.
- Implement stable approach keys, failure classification, and retry audit.
- Test identical retry suppression and justified retries after material change.

**Gate:** attempt records survive restart and every attempt has an auditable terminal outcome or recoverable uncertain state.

### Stage 6 — Capability registry and Model Router

- Add explicit capability records and cost classes.
- Add OmniRoute chat adapter reuse and separately typed image adapter interfaces.
- Add fake/mock provider tests; no paid live route.
- Model discovery does not auto-enable capability.

**Gate:** unsupported modalities and paid/unknown routes are rejected; valid mock/local/verified routes pass; full suite green.

### Stage 7 — Text pipeline and evaluator

- Implement analyze/generate/rewrite operations with policy gate and bounded output.
- Implement deterministic text checks.
- Test offline behavior and model failures.

**Gate:** complete text workflow with mock adapters; full suite green.

### Stage 8 — Image pipeline and evaluator

- Implement image analyze/generate/edit adapters and artifact validation.
- Verify MIME/signature/dimensions and provider capability.
- Test malformed images, oversized images, unsupported provider operations, output corruption, and artifact retrieval.

**Gate:** image workflow passes using deterministic fakes/local test fixture; no paid API required; full suite green.

### Stage 9 — Executor and Supervisor

- Join task store, router, evaluator, attempt tracker and payload store.
- Implement bounded run-until-done.
- Implement exact five-failure checkpoint, durable wait, explicit resume, and cancellation.
- Test crash/restart with encrypted and ephemeral payload policies.

**Gate:** all workflow and recovery scenarios pass; no sixth failed attempt before explicit user decision; full suite green.

### Stage 10 — Workspace feedback

- Implement sanitizer, structured proposals, and fixed publisher capability.
- Test that original text, image bytes, identifiers, paths, secrets and raw provider bodies cannot be published.

**Gate:** only schema-valid sanitized generic lessons can enter Workspace shareable memory; full suite green.

### Stage 11 — MCP and Gateway integration

- Add Adult MCP server registration behind ENABLE_ADULT_AGENT=false by default.
- Add exact Adult allowlist to Gateway only when enabled.
- Add task, attempts, resume, cancel, list and bounded artifact-chunk tools.
- Test tool discovery, argument schemas, redaction, stable task IDs, and disabled-by-default behavior.

**Gate:** MCP integration test passes; no private memory/SQL/filesystem/provider credentials exposed; full suite green.

### Stage 12 — Final integration, docs and release review

- Run the entire unit/integration test suite from a fresh process.
- Run lint/type/security/dependency checks configured by the repository.
- Verify migrations against a copy of a real legacy memory DB; never test migrations on the only copy of user data.
- Verify clean diff and no secret leakage.
- Update README, setup, testing, and configuration docs.
- Open one draft PR from feature/adult-agent-mvp to main.
- Do not merge without explicit approval.

**Gate:** all CI jobs green, tests recorded, PR reviewed, no protected files changed.

---

## 14. Acceptance test matrix

| ID | Scenario | Required result |
|---|---|---|
| MEM-01 | Workspace writes memory | Stored in Workspace namespace |
| MEM-02 | Adult writes private memory | Stored in Adult namespace |
| MEM-03 | Workspace principal recalls Adult private record | No result / access denied |
| MEM-04 | Adult recalls Workspace memory | Only explicitly shareable records |
| MEM-05 | Legacy migration | IDs/content/tags/timestamps retained and marked internal/unknown |
| MEM-06 | Feedback publisher reads Adult namespace | Denied |
| TASK-01 | Create text task | Queued task ID returned |
| TASK-02 | Create image task with invalid base64 | Rejected before provider attempt |
| TASK-03 | Task state transition with stale expected state | Rejected; latest state preserved |
| TASK-04 | Two simultaneous claims | At most one active lease |
| CP-01 | Successful step | Checkpoint persisted before next step |
| CP-02 | Five failures | waiting_user, checkpointed, no sixth attempt |
| CP-03 | Explicit continue | New bounded segment starts |
| CP-04 | Explicit cancel | No further provider dispatch |
| CP-05 | Process restart with encrypted payload | Resume from checkpoint |
| CP-06 | Process restart with ephemeral payload | needs_input; no false claim of full resume |
| ATT-01 | Same failed approach repeated | Skipped |
| ATT-02 | Same adapter after meaningful config change | Allowed with retry reason and new key |
| ATT-03 | Crash after attempt start | Attempt recovered as uncertain/timeout, not success |
| ROUTE-01 | Text task to text-capable verified model | Eligible |
| ROUTE-02 | Image task to text-only model | Rejected before dispatch |
| ROUTE-03 | Paid/unknown model with zero cost budget | Rejected |
| ROUTE-04 | Model listed but capability unverified | Not eligible |
| TXT-01 | Valid text output | Evaluated and completed |
| TXT-02 | Empty/oversized text output | Rejected or retried within policy |
| IMG-01 | PNG/JPEG/WebP valid input | Accepted within limits |
| IMG-02 | MIME spoof/SVG/oversized/corrupt image | Rejected before provider call |
| IMG-03 | Corrupt provider output | Not published as success |
| IMG-04 | Artifact retrieval chunks | Correct ordered bytes/hash and bounded response |
| SEC-01 | MCP caller supplies namespace/principal | Schema rejects extra fields |
| SEC-02 | MCP response inspection | No payload plaintext, secrets, or local absolute paths |
| SEC-03 | Feedback sanitizer | No raw user text/image/secrets published |
| MCP-01 | Adult disabled | Adult server/tools absent |
| MCP-02 | Adult enabled | Only named allowlisted tools present |
| MCP-03 | Restart between tool calls | Task retrieved/resumed by task_id |
| REG-01 | Existing project tests | No regressions |
| REG-02 | Protected path scan | No changes under .vscode/ or .env |

---

## 15. Observability, errors, and privacy

Stable error codes (minimum):

- POLICY_REJECTED
- INVALID_INPUT
- INVALID_IMAGE
- CAPABILITY_UNAVAILABLE
- ROUTE_NOT_VERIFIED
- COST_POLICY_BLOCKED
- PROVIDER_TIMEOUT
- PROVIDER_RATE_LIMIT
- PROVIDER_ERROR
- EVALUATION_FAILED
- DUPLICATE_APPROACH
- ATTEMPT_BUDGET_EXHAUSTED
- WALL_TIME_BUDGET_EXHAUSTED
- PAYLOAD_KEY_MISSING
- PAYLOAD_DECRYPT_FAILED
- PAYLOAD_EXPIRED
- USER_APPROVAL_REQUIRED
- INPUT_REUPLOAD_REQUIRED
- TASK_CANCELLED

Logs may include task ID, attempt ID, provider/model identifier, operation, elapsed time, status, normalized error code, and cost/usage metadata. They must not include raw prompt/input text, image bytes, image URLs containing credentials, authorization headers, API keys, full request/response bodies, or private Adult memory contents.

---

## 16. Definition of Done

Adult Agent MVP is complete only when all conditions hold:

1. Text analyze/generate/rewrite workflows work end to end.
2. Image analyze/generate/edit workflows work end to end through verified adapters.
3. Adult memory is isolated from general Workspace memory through storage-layer authorization.
4. Legacy Workspace memory survives migration unchanged in content and remains internal by default.
5. Every attempt is durably recorded and duplicate failed approaches are not blindly repeated.
6. Task checkpoints preserve all non-sensitive execution state and encrypted payload references.
7. Every five failed attempts causes a persisted wait for explicit user decision before any further attempt.
8. Restart/resume works for retained encrypted payloads; ephemeral payload loss becomes needs_input.
9. Time, attempts, provider calls, and cost budgets are enforced before dispatch.
10. Paid/unknown routes are blocked by default.
11. Workspace receives only sanitized, explicitly shareable feedback.
12. Adult MCP is disabled by default and exposes only the documented allowlist when enabled.
13. Existing Workspace behavior and all existing tests remain intact.
14. Full CI/security checks pass and documentation is complete.
15. Changes remain on feature/adult-agent-mvp; no merge to main occurs without explicit approval.

---

## 17. Reference links

Repository baseline:

- [Workspace tool agent](https://github.com/Kero2001aaa/ai-tools-workspace/blob/main/agent/tool_agent.py)
- [Existing model adapter](https://github.com/Kero2001aaa/ai-tools-workspace/blob/main/agent/model.py)
- [Existing memory store](https://github.com/Kero2001aaa/ai-tools-workspace/blob/main/agent/memory_store.py)
- [Existing plan store](https://github.com/Kero2001aaa/ai-tools-workspace/blob/main/agent/plan_store.py)
- [Existing supervisor worker](https://github.com/Kero2001aaa/ai-tools-workspace/blob/main/agent/supervisor_worker.py)

External API/protocol contracts to re-verify during implementation:

- [OmniRoute API Reference](https://github.com/cheaper-inference/OmniRoute/wiki/API-Reference)
- [MCP Tools Specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP Streamable HTTP Transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [SQLite Database File Format and WAL](https://www.sqlite.org/fileformat.html)

Provider/model capabilities, pricing, retention, and terms can change. Re-verify against the deployed versions and official provider terms before enabling a route.
