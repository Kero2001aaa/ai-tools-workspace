# Adult Agent — Stage 6 Implementation Specification
## Capability Registry, Evidence Verification, Cost Policy, and Model Router

**Status:** Design approved; implementation contract  
**Branch:** feature/adult-agent-mvp  
**Parent specification:** docs/ADULT_AGENT_IMPLEMENTATION_SPEC.md  
**Precondition:** Stages 1–5 are PASS on the parent commit  
**Target operational schema:** adult_operational_store_v3  
**Implementation boundary:** Registry, capability evidence, eligibility, deterministic route selection, typed adapter contracts, and Adult-specific configuration isolation only

---

## 1. Objective and completion boundary

Stage 6 establishes a fail-closed source of truth for which exact Adult task operations an adapter/provider/model route can perform, under which input/output constraints, and under which zero-cost policy.

At completion:

1. Capability records are explicit, typed, versioned, disabled by default, and persisted in the existing Adult operational database.
2. Capability support and zero-cost eligibility have separate evidence. A model catalog entry is discovery metadata, not proof.
3. Registration, verification, enablement, disablement, revision, and revocation are distinct state transitions with append-only audit events.
4. The registry returns only currently enabled, verified, unexpired, operation-compatible routes.
5. The router filters by exact modality and input constraints, applies the task's cost budget, excludes previously attempted approaches, and returns a deterministic ordered list.
6. Local routes may be eligible without provider-call budget. A free_verified route requires an explicit positive per-task provider-call budget and current evidence of zero cost.
7. Paid and unknown-cost routes are always rejected in this MVP, regardless of credentials or catalog contents.
8. Adult model configuration is isolated from Workspace MODEL_* configuration.
9. Typed adapter contracts are defined; the Stage 6 tests use fakes/mocks and make no provider/network calls.
10. Existing Stages 2–5 data and behavior remain unchanged, and the full regression suite remains green.

Stage 6 does not implement task execution, automatic retries, the Supervisor or Executor, text/image processing pipelines, live provider inference, live OmniRoute model discovery, image HTTP endpoints, MCP registration, or Gateway integration.

## 2. Preconditions and protected boundaries

Before implementation, re-check all Stage 1–5 acceptance specifications and the current full-suite/CI evidence. If a previous-stage defect is found, stop Stage 6 implementation, fix the defect in its owning stage, then rerun focused tests and the complete suite until clean.

Protected paths and operations:

- Never inspect, edit, stage, or replace .vscode/.
- Never read, edit, stage, or replace .env, credentials, or secrets. Only .env.example may receive safe empty defaults.
- Never inspect, migrate, or modify the live .data/adult.sqlite3, .data/memory.sqlite3, or payload databases.
- All migration tests use independent temporary SQLite files.
- Do not install packages, deploy OmniRoute, create accounts, activate billing, purchase credits, or call provider endpoints.
- Do not add Adult MCP tools or expose registry mutation through MCP.
- Do not merge PR #2 or the feature branch into main.

The existing repository gate is 255 full-suite tests. Stage 6 adds focused registry/router/configuration tests and must continue running all earlier tests.

## 3. Ownership and module boundaries

| Module | Stage 6 responsibility | Explicit exclusion |
|---|---|---|
| agent/adult/models.py | Capability, evidence, query, cost-policy and route DTOs | Provider requests |
| agent/adult/capability_registry.py | Typed validation, lifecycle, evidence freshness and eligible-record queries | Network discovery or inference |
| agent/adult/adult_router.py | Exact capability filtering, cost filtering, approach exclusion and deterministic ordering | Executing a selected route |
| agent/adult/adult_task_store.py | Own v2→v3 migration, schema validation, transactions and private capability persistence primitives | Provider semantics |
| agent/adult/integrations/protocols.py | Text and image adapter Protocols and typed requests/results | Provider-specific execution |
| agent/adult/integrations/omniroute_chat.py | Adult-only OmniRoute configuration and lazy chat-client construction | Live invocation in Stage 6 |
| agent/model.py | Explicit-config ChatOpenAI factory while preserving Workspace loader behavior | Adult policy or registry |
| tests/test_adult_capability_registry.py | Registry, evidence, migration and audit tests | Live databases |
| tests/test_adult_router.py | Eligibility and ordering tests | Network |
| tests/test_adult_omniroute_adapters.py | Config isolation and mocked factory tests | Live provider calls |

The TaskStore remains the sole owner of the operational SQLite connection, schema ledger, migration transaction, and persistence operations. Registry code must not open a second database, create its own connection policy, or expose raw SQL.

## 4. Domain model and validation contracts

### 4.1 Exact task/modality mapping

The supported task types remain those already defined by Adult models:

| task_type | Required input modalities | Required output modality |
|---|---|---|
| text.analyze | text | text |
| text.generate | text | text |
| text.rewrite | text | text |
| image.analyze | image | text |
| image.generate | text | image |
| image.edit | text + image | image |

A capability record is specific to one exact task_type. Similar task names, model names, or a generic "vision" label do not imply support for another operation.

### 4.2 ModelCapability

Use a frozen typed DTO with these logical fields:

- capability_id: opaque generated identifier.
- adapter_id and adapter_version.
- provider_id and model_id, optional only for a genuinely local adapter; model_version when supplied.
- task_type: one exact existing Adult task type.
- input_modalities and output_modalities: non-empty, allowlisted modality sets matching the task mapping.
- accepted_mime_types: explicit MIME allowlist for binary inputs, restricted to PNG/JPEG/WebP; SVG and wildcard MIME types are rejected.
- max_input_bytes, max_output_bytes, max_width, max_height: positive bounded constraints where applicable; irrelevant text-only constraints are null.
- cost_class: local, free_verified, paid, or unknown.
- enabled: false on registration.
- verification_status: unverified, verified, or revoked.
- capability_version: canonical SHA-256 version of the capability contract and verification evidence; no secret material.
- capability_verified_at and capability_expires_at.
- capability_evidence_source and capability_evidence_ref.
- cost_verified_at, cost_expires_at, cost_evidence_source and cost_evidence_ref for free_verified routes.
- state_version, created_at and updated_at.

The typed DTO rejects booleans where integers are required, unsupported enum values, duplicate modalities/MIME values, wildcards, oversized identifiers, invalid timestamps, impossible size bounds, credentials in evidence references, and unknown JSON keys.

### 4.3 CapabilityEvidence and cost evidence

Capability evidence and price evidence are separate facts:

- CapabilityEvidence carries the exact adapter ID/version, provider ID, model ID/version, task_type, and input/output modality contract; the registry rejects evidence whose subject differs from the registered capability.
- Cost evidence carries an explicit `cost_is_zero` boolean. A `free_verified` route is eligible only when it is exactly `true`; a pricing URL by itself is not proof of zero cost.

Allowed source kinds are a strict enum, not arbitrary descriptive text. Evidence references are bounded non-secret identifiers or sanitized HTTPS documentation references; reject URL user-info, query strings, fragments, local paths, tokens, and credentials. Do not store copied provider response bodies or raw documents.

For a provider route, a model-catalog listing or GET /models response is discovery only and cannot by itself verify capability or cost. free_verified requires separate current cost evidence. For a local route, registration does not prove that an adapter is installed or usable; the router also requires a positive result from an injected, side-effect-free adapter-availability check.

Evidence subject, source, reference, verification/expiry timestamps, and zero-cost claim participate in the capability version hash. Persisted records fail closed when the version hash no longer matches the stored contract/evidence. Verification expiry is mandatory. An expired capability or zero-cost record is ineligible until explicitly re-verified. No silent expiry extension or background refresh is permitted.

### 4.4 CapabilityQuery and CostPolicy

CapabilityQuery is a validated immutable request containing:

- exact task_type;
- required input modalities and output modality;
- optional input MIME type, byte count, width and height;
- keyed input fingerprint and non-secret configuration fingerprint needed to calculate the same approach key used by Stage 5.

CostPolicy is constructed from the task's validated TaskBudget, not from MCP/user-supplied policy fields:

- max_cost_microusd must equal 0;
- max_provider_calls is bounded 0–20;
- local routes are permitted only when the adapter-availability check passes;
- free_verified routes are permitted only when max_provider_calls > 0;
- paid and unknown are never permitted.

A task whose max_provider_calls is the TaskBudget default (0) is offline with respect to remote inference. Enabling a free_verified route requires an explicit per-task budget greater than zero. This does not permit cost above zero.

## 5. Capability lifecycle and registry API

Registration never enables a route. Verification never enables a route. Enablement is a distinct explicit operation and uses optimistic state-version checks.

The registry public application contract is:

    class ModelCapabilityRegistry:
        def register(self, capability: ModelCapabilitySpec) -> ModelCapability: ...
        def verify(
            self, capability_id: str, evidence: CapabilityEvidence,
            expected_state_version: int,
        ) -> ModelCapability: ...
        def revise(
            self, capability_id: str, replacement: ModelCapabilitySpec,
            expected_state_version: int,
        ) -> ModelCapability: ...
        def enable(
            self, capability_id: str, expected_state_version: int,
        ) -> ModelCapability: ...
        def disable(
            self, capability_id: str, reason_code: str,
            expected_state_version: int,
        ) -> ModelCapability: ...
        def revoke(
            self, capability_id: str, reason_code: str,
            expected_state_version: int,
        ) -> ModelCapability: ...
        def list_eligible(
            self, query: CapabilityQuery, cost_policy: CostPolicy,
        ) -> list[RouteCandidate]: ...

Lifecycle invariants:

1. register inserts a new disabled/unverified record. Duplicate identities are rejected; they are not silently overwritten.
2. verify checks evidence against the exact capability contract, records evidence timestamps/references, calculates a new capability_version, and leaves enabled=false.
3. enable succeeds only for verified, unexpired evidence and requires expected_state_version to match.
4. revise replaces only the capability contract, increments state_version, computes a new capability_version, clears prior evidence, and forces enabled=false/unverified. Re-verification and explicit enablement are required.
5. disable makes the record ineligible without deleting its history.
6. revoke is terminal for that capability record; re-registration under a new capability ID is required.
7. Every successful lifecycle mutation and every rejected stale-version transition is handled without partial writes. Successful mutations append one capability audit event in the same transaction.
8. list_eligible is read-only. It never registers, verifies, enables, refreshes, or contacts a provider.

Use bounded normalized reason codes in audit metadata. Never place raw prompts, payloads, images, provider bodies, keys, authorization headers, environment values, or local paths in the registry or its events.

## 6. AdultTaskStore schema v3

The current operational schema is v2 (Stage 5). Stage 6 adds one atomic additive migration to v3 in the same .data/adult.sqlite3 database. Do not create a separate capability database.

### 6.1 adult_model_capabilities

The new table stores the fields defined in Section 4.2 with SQL CHECK constraints for:

- exact task_type and cost_class enum values;
- enabled in (0,1);
- verification_status in (unverified, verified, revoked);
- bounded identifiers and JSON object size;
- valid 64-character lowercase hexadecimal capability_version;
- non-negative state_version and required timestamps;
- verified records require capability evidence fields;
- free_verified records require independent cost evidence fields before they can be enabled.

Use a unique expression index over adapter_id, normalized provider_id, normalized model_id and task_type so NULL provider/model identifiers cannot bypass uniqueness.

### 6.2 adult_capability_events

Add a separate append-only audit table with opaque event_id, capability_id, event_type, from/to verification and enablement states, previous/new capability versions, normalized reason_code, bounded sanitized metadata_json, and server-generated created_at.

Event types are limited to registered, verified, revised, enabled, disabled and revoked. Add UPDATE/DELETE prevention triggers for this table and a DELETE prevention trigger for capability records. There is no public delete operation.

### 6.3 Migration requirements

- Define schema version 3 and a unique migration name.
- New databases initialize through v1, v2 and v3 in one transaction.
- Existing v1 databases advance v1→v2→v3 in one BEGIN IMMEDIATE transaction.
- Existing v2 databases advance v2→v3 in one BEGIN IMMEDIATE transaction.
- Existing v3 databases validate only; the migration is idempotent.
- Preserve every existing task, step, checkpoint, task event, attempt, attempt event, index and trigger.
- Update expected table/index/trigger sets and migration-history validation.
- A failure at any DDL or validation point rolls the entire migration back to the prior committed schema.
- Unsupported histories, unexpected tables, missing v2 objects, or tampered v3 objects fail closed.
- No migration test may point to .data/ or any user runtime path.

The migration tests must explicitly seed a populated v2 database containing tasks, steps, checkpoints, task events, attempts and attempt events, then prove all rows and immutable history remain byte/value-equivalent after v3.

## 7. RouteCandidate and Model Router

RouteCandidate remains the validated Stage 5 DTO. The registry maps a verified, eligible ModelCapability into RouteCandidate, including the exact adapter/provider/model versions, task_type, cost_class, capability_version and verified_at.

The router contract is:

    class AdultModelRouter:
        def choose_routes(
            self, query: CapabilityQuery, cost_policy: CostPolicy,
            excluded_approach_keys: set[str],
        ) -> list[RouteCandidate]: ...

The router must use the exact Stage 5 approach-key calculation. Do not duplicate or subtly alter its canonical JSON/hash algorithm. Inject a small typed approach-key builder at construction so AdultModelRouter can calculate each candidate's approach key from task_type, RouteCandidate, input_fingerprint and config_fingerprint without importing provider code or creating a circular dependency.

Eligibility rules are applied before sorting:

1. exact task_type match;
2. required input modalities are all supported;
3. required output modality is supported;
4. MIME, byte-size and dimension constraints pass;
5. record is enabled, verified and not expired;
6. current local availability or free_verified policy passes;
7. paid and unknown are excluded unconditionally;
8. calculated approach_key is not in excluded_approach_keys.

Ordering is deterministic and stable: local routes first, then free_verified routes; within each class sort lexicographically by adapter_id, provider_id, model_id and capability_id. Do not use random selection, model-name popularity, provider-reported rankings, or implicit fallbacks.

The router returns a list only. It does not dispatch, retry, call an adapter, mutate task state, or spend a provider-call budget. AttemptTracker remains the only authority that reserves a durable attempt and enforces the task's provider-call/attempt budgets before any later-stage adapter invocation.

## 8. Adapter protocols and OmniRoute configuration

### 8.1 Typed protocols

Define separate typed request/result contracts for:

- TextModelAdapter.invoke(TextRequest) -> AdapterResult
- ImageUnderstandingAdapter.analyze(ImageAnalysisRequest) -> AdapterResult
- ImageGenerationAdapter.generate(ImageGenerationRequest) -> ArtifactResult
- ImageEditingAdapter.edit(ImageEditRequest) -> ArtifactResult

The protocols make cross-modality misuse fail at validation/type boundaries. Stage 6 provides protocol definitions and fake implementations for tests; actual image HTTP adapters and pipeline invocations remain Stage 8 work. Text pipeline invocation remains Stage 7 work.

### 8.2 Workspace-compatible model factory

Add an explicit-config ChatOpenAI construction helper in agent/model.py:

    build_chat_model(
        *, api_key: str, model: str, base_url: str | None = None,
        timeout: int = 30, max_retries: int = 0,
    )

Keep load_chat_model() backward-compatible: it continues to call load_dotenv(), read only MODEL_API_KEY/MODEL_NAME/MODEL_BASE_URL, return None when all are blank, and preserve the existing 30-second timeout and zero automatic retries.

Adult must not call load_chat_model() or inherit Workspace MODEL_* values. OmniRouteChatAdapter receives an Adult-specific configuration object built only from ADULT_OMNIROUTE_BASE_URL and ADULT_OMNIROUTE_API_KEY; the selected model_id comes from the verified RouteCandidate. Mark the API key field repr=False and exclude it from equality, serialization, fingerprints, logs and audit.

An incomplete Adult configuration means no Adult OmniRoute route is available; it does not crash imports or affect Workspace. The client factory is lazy and construction tests patch ChatOpenAI. Stage 6 must not invoke the returned client or make HTTP requests.

### 8.3 Configuration defaults

| Setting | Default | Rule |
|---|---|---|
| ADULT_ALLOW_PAID | false | Paid routes remain unconditionally rejected in this MVP |
| ADULT_MAX_PROVIDER_CALLS | 0 | Offline remote-inference default; per-task positive budget is explicit opt-in |
| ADULT_MAX_COST_MICROUSD | 0 | Hard zero-cost policy |
| ADULT_OMNIROUTE_BASE_URL | empty | No configured Adult remote route |
| ADULT_OMNIROUTE_API_KEY | empty | Server-side secret only; never accepted through MCP |

The Workspace MODEL_* configuration is not an Adult fallback. An Adult API key does not imply a free route or enable a capability.

## 9. Security and privacy invariants

- No capability, evidence, registry event, log or exception contains provider credentials, raw prompts, images, base64, provider request/response bodies, private memory, or absolute local paths.
- No API key is stored in SQLite, capability JSON, fingerprints, RouteCandidate, repr output, logs or MCP data.
- Registry/evidence references are sanitized and bounded.
- JSON is canonicalized with sorted keys, compact separators and allow_nan=False before hashing.
- SQL is parameterized; writes and audit events share one transaction; state_version prevents stale concurrent updates.
- No registry API accepts raw SQL, database paths, principal/namespace, provider implementation objects, or caller-selected cost overrides.
- Catalog discovery is not capability proof. Discovery cannot mutate enablement.
- No Stage 6 test reaches the network, provider API, live OmniRoute server, live database or billing service.

## 10. Acceptance test matrix

### Migration and persistence

- CAP-MIG-01: new temporary DB initializes directly at schema v3.
- CAP-MIG-02: populated v2 DB migrates additively to v3.
- CAP-MIG-03: v1 DB migrates sequentially to v3 in one transaction.
- CAP-MIG-04: repeated initialization at v3 is idempotent.
- CAP-MIG-05: injected migration failure rolls back all v3 DDL and ledger changes.
- CAP-MIG-06: all v2 task/step/checkpoint/task-event/attempt/attempt-event rows and required indexes/triggers survive unchanged.
- CAP-MIG-07: unexpected schema/history or tampered required object fails closed.
- CAP-MIG-08: duplicate normalized local/provider/model/task identities are rejected.

### Registry and evidence

- CAP-REG-01: registration creates disabled/unverified record.
- CAP-REG-02: duplicate identity is rejected without overwriting.
- CAP-REG-03: catalog discovery alone cannot verify or enable capability.
- CAP-REG-04: exact task/modality evidence verifies; mismatched operation or model evidence is rejected.
- CAP-REG-05: capability verification does not enable the record.
- CAP-REG-06: enable requires verified, unexpired evidence and matching state_version.
- CAP-REG-07: stale state_version cannot enable, revise, disable or revoke.
- CAP-REG-08: revision changes capability_version, clears evidence and disables the route.
- CAP-REG-09: revoked records cannot be re-enabled.
- CAP-REG-10: expired capability evidence is ineligible.
- CAP-REG-11: free_verified requires separate current zero-cost evidence.
- CAP-REG-12: expired/missing/invalid cost evidence excludes the route.
- CAP-REG-13: audit events are atomic, append-only and contain no secrets/payloads.
- CAP-REG-14: capability rows cannot be deleted through SQL triggers.

### Router and cost policy

- CAP-ROUTE-01: exact text task/capability is eligible.
- CAP-ROUTE-02: text-only route cannot satisfy image.analyze or image.edit.
- CAP-ROUTE-03: image generation and image editing are distinct operations.
- CAP-ROUTE-04: unsupported MIME, oversized bytes or dimensions are rejected.
- CAP-ROUTE-05: disabled, unverified, expired and revoked records are ineligible.
- CAP-ROUTE-06: local route is eligible only when local adapter availability returns true.
- CAP-ROUTE-07: free_verified route is rejected when max_provider_calls=0.
- CAP-ROUTE-08: free_verified route is eligible only with explicit positive provider-call budget and current zero-cost evidence.
- CAP-ROUTE-09: paid and unknown routes are rejected regardless of API key/configuration.
- CAP-ROUTE-10: excluded Stage 5 approach keys are never returned.
- CAP-ROUTE-11: capability version changes produce a different Stage 5 approach key.
- CAP-ROUTE-12: deterministic sort order is stable across repeated calls and insertion order.
- CAP-ROUTE-13: registry queries and route selection perform no writes or network calls.

### Adapter/configuration isolation

- CAP-ADAPTER-01: Workspace load_chat_model retains existing MODEL_* behavior.
- CAP-ADAPTER-02: explicit-config factory forwards API key/model/base URL/timeout/max_retries correctly to a mocked ChatOpenAI.
- CAP-ADAPTER-03: Adult config reads ADULT_* only and does not inherit MODEL_* values.
- CAP-ADAPTER-04: missing Adult config leaves Adult offline and does not affect Workspace.
- CAP-ADAPTER-05: Adult API key is absent from repr, serialization, equality and captured logs.
- CAP-ADAPTER-06: no test performs a network request or invokes a real model.
- CAP-ADAPTER-07: image protocol rejects text adapter/request cross-wiring.

### Regression

- CAP-REGRESSION-01: all existing MEM-*, TASK-*, CP-*, ATT-* and payload security tests remain green.
- CAP-REGRESSION-02: full repository unit suite passes from a fresh process.
- CAP-REGRESSION-03: MCP schema scan, Python dependency audit and optional browser dependency audit pass.
- CAP-REGRESSION-04: protected-path and diff review confirms no .env, .vscode, credential or runtime DB changes.

## 11. Required implementation order and gate

Implement in this order:

1. Add Stage 6 typed DTOs and validators; add focused model tests.
2. Add v2→v3 migration and expected schema validation using temporary DB fixtures; run all Stage 2–5 tests.
3. Add registry persistence/lifecycle and append-only audit; run focused registry/migration tests.
4. Add capability/evidence eligibility and cost policy; run focused registry tests.
5. Add deterministic Router and Stage 5 approach-key builder integration; run router and AttemptTracker tests.
6. Add typed adapter protocols, explicit-config model factory and Adult OmniRoute config isolation; run adapter and existing Workspace model tests.
7. Review complete diff and protected paths.
8. Run every focused Adult test, then the full repository unit suite from a fresh process.
9. Push/commit only to feature/adult-agent-mvp and inspect the resulting CI jobs/logs.
10. Record Stage 6 PASS only when all CAP-* tests, previous-stage tests, full suite, security scans and CI pass.

No Stage 7 work begins until the Stage 6 gate is recorded as PASS.
