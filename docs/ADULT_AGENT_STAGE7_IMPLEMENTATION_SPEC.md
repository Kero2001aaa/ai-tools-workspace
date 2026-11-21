# Adult Agent — Stage 7 Implementation Specification
## Text Pipeline, Deterministic Evaluation, and Policy Gates

**Status:** Approved for implementation  
**Branch:** feature/adult-agent-mvp  
**Parent:** Stage 6 commit 541ff5e089f37216ad40716f3daecc4ed72da7c1  
**Preconditions:** Stages 1–6 PASS; Stage 6 local validation confirmed on the parent branch  
**Scope:** One bounded text operation/attempt per pipeline invocation  
**Hard policy:** No paid or unknown-cost route; no provider call unless a task explicitly has a positive provider-call budget and the route has current free_verified evidence

---

## 1. Objective and completion boundary

Stage 7 provides the first executable Adult text path for these exact task types:

- `text.analyze`
- `text.generate`
- `text.rewrite`

The path consists of input-envelope validation, deterministic policy gates, exact capability routing, one tracked adapter invocation, deterministic output evaluation, and final artifact publication.

A successful Stage 7 implementation must prove that:

1. Raw text and instructions remain in the payload store or transient process memory; they never enter task metadata, checkpoints, attempt rows/events, normal logs, or exception messages.
2. Policy is checked before routing/dispatch and checked again against the model output before publication.
3. The router returns only a verified exact text-capable route that complies with the task's zero-cost budget.
4. AttemptTracker persists the attempt start before the adapter is invoked.
5. Model output remains an in-memory value until deterministic evaluation and output policy both accept it.
6. Only accepted text is written as a `text/plain` artifact and referenced by a successful attempt.
7. A rejected/failed output cannot be exposed as the task's final result.
8. A single pipeline invocation performs at most one adapter invocation. It never implements its own retry loop.
9. Offline mode is deterministic: if no eligible local route exists and no explicitly budgeted free_verified route is eligible, no adapter is invoked and no attempt is created.
10. All tests use fake adapters or mocked clients. CI and tests make no provider/network calls.

Stage 7 does not implement image operations, image adapters, a task Supervisor, a general Executor, run-until-done, background work, MCP/Gateway tools, AdultAgent submission facade, feedback publication, or live paid model routes. Stage 8 owns image execution. Stage 9 owns task orchestration, retries across pipeline invocations, checkpoint/resume/cancel, and workflow-level lease management.

## 2. Preconditions and protected boundaries

Before implementation, re-check Stage 1–6 specifications and the full test baseline. If a previous-stage defect is found, stop Stage 7 implementation, fix it in its owning stage, and rerun focused plus complete regression tests until green.

Protected boundaries:

- Never inspect, edit, stage, or replace `.vscode/`.
- Never read, edit, stage, or replace `.env`; only safe empty defaults may be added to `.env.example` when required.
- Never inspect, migrate, or modify live `.data/` databases or payload directories.
- All tests use isolated temporary directories and temporary SQLite databases.
- No package installation, provider account creation, billing activation, live endpoint call, or external model invocation in CI.
- Never merge the feature branch or PR #2 into `main`.
- Do not add Adult MCP tools or enable Adult MCP registration.
- Keep the global Stage 6 limits: `max_cost_microusd=0`, `ADULT_MAX_PROVIDER_CALLS=0` by default, paid/unknown routes rejected unconditionally.

## 3. Module ownership

| Module | Stage 7 responsibility | Explicit exclusion |
|---|---|---|
| `agent/adult/models.py` | Text evaluation and policy DTOs | Raw text persistence |
| `agent/adult/integrations/protocols.py` | TextRequest and ephemeral TextAdapterResult; retain image protocols | Image execution |
| `agent/adult/integrations/omniroute_text.py` | Text-only OmniRoute adapter using Adult-specific configuration | Live calls in tests/CI; paid route |
| `agent/adult/adult_policies.py` | Deterministic text input/output policy decisions | Model-based moderation |
| `agent/adult/adult_evaluator.py` | Deterministic text output checks | Semantic quality scoring by another model |
| `agent/adult/adult_text_pipeline.py` | One bounded routed/attempt-tracked text operation | Retry loop and workflow state machine |
| `agent/adult/payload_store.py` | Add safe discard for a saved-but-uncommitted output artifact | Caller-provided filesystem paths |
| `tests/test_adult_policies.py` | Policy allow/deny and bounds | Provider calls |
| `tests/test_adult_evaluator.py` | Deterministic output acceptance/rejection | Model reviewer |
| `tests/test_adult_text_pipeline.py` | Full fake-adapter text path, attempt outcome and artifact publication | Real network |
| `tests/test_adult_omniroute_text.py` | Mocked ChatOpenAI adapter mapping and failures | Live provider |

The TaskStore continues to own task/step/checkpoint transactions. AttemptTracker remains the sole authority for durable attempt lifecycle and approach deduplication. AdultArtifactStore remains the owner of encrypted/transient payload and artifact bytes. Stage 7 must not introduce another database or persistence format.

## 4. Text input contract

### 4.1 Accepted task types and bounds

The input contract is fixed for this stage:

| Field | Rule |
|---|---|
| task_type | Exactly `text.analyze`, `text.generate`, or `text.rewrite` |
| source text | Required non-empty Unicode string; maximum 20,000 characters |
| instruction | Optional Unicode string; maximum 4,000 characters |
| input encoding | Strict UTF-8 |
| envelope size | Bounded by the configured Adult payload input limit |
| output | Strict UTF-8, plain text; maximum 20,000 characters and 80 KiB encoded bytes |

Generation still requires a non-empty user brief/source text. Instruction is an additional constraint, not a replacement for the source text. Analyze and rewrite also require source text. Empty/whitespace-only source text is rejected before routing.

The initial Stage 7 output contract is plain text. Do not add caller-defined JSON schemas, arbitrary response formats, or unbounded output controls in this stage.

### 4.2 Payload envelope

The submitter serializes source text and optional instruction into one bounded canonical JSON envelope, then passes the resulting bytes to `AdultArtifactStore.save_input`. The payload reference is the only input passed across persistence and adapter boundaries.

Logical envelope:

    {
      "schema_version": 1,
      "task_type": "text.analyze | text.generate | text.rewrite",
      "text": "<source text>",
      "instruction": "<optional instruction>"
    }

The actual stored bytes are UTF-8 JSON with sorted keys, compact separators, and `allow_nan=False`. On read, enforce exact keys, exact schema version, matching task_type, field types, field limits, strict UTF-8/JSON parsing, and encoded-size limits. Reject unknown keys and duplicate JSON keys.

Do not add text or instruction columns to SQLite. Do not copy the envelope into checkpoints or audit metadata. The envelope is loaded only for the duration of the pipeline call and then released from local references.

## 5. Typed adapter boundary

### 5.1 TextRequest

Refine the existing Stage 6 text protocol with a typed operation-aware request:

    @dataclass(frozen=True)
    class TextRequest:
        task_id: str
        step_id: str
        task_type: Literal["text.analyze", "text.generate", "text.rewrite"]
        input_payload_id: str
        max_output_chars: int = 20000

The DTO contains no raw source text, instruction, credential, URL, or filesystem path. Validate opaque IDs, exact operation enum, and a hard output bound of 20,000 characters.

### 5.2 TextAdapterResult

Use a text-specific result rather than the generic image-understanding AdapterResult:

    @dataclass(frozen=True)
    class TextAdapterResult:
        text: str = field(repr=False)
        usage: dict[str, int]
        cost_microusd: int | None = None

The text field exists only in memory. It must be excluded from repr/logging and must not have a generic `to_dict`, `to_json`, or persistence method. Validate type and bounded usage/cost metadata; the evaluator enforces the operation's exact output limits. Preserve the existing generic AdapterResult for image-understanding protocol compatibility.

The TextModelAdapter protocol becomes:

    class TextModelAdapter(Protocol):
        def invoke(self, request: TextRequest) -> TextAdapterResult: ...

### 5.3 OmniRoute text adapter

Add a text-only adapter that:

- Reads the input envelope through the injected AdultArtifactStore using task_id + payload_id ownership checks.
- Uses only AdultOmniRouteConfig and the already-verified RouteCandidate model ID.
- Maps each task type to a fixed server-owned system instruction; the user instruction remains untrusted user content.
- Sends no caller-controlled base URL, API key, provider name, or model name.
- Uses timeout 30 seconds and zero client-level automatic retries.
- Accepts only a string text result; non-text/multipart response content is a bounded adapter failure.
- Extracts only approved bounded usage counters and cost metadata; discards raw provider response objects.
- Never logs messages, prompts, responses, authorization headers, or exception bodies.

The adapter does not verify or enable a capability. The Stage 6 Registry and Router remain authoritative.

## 6. Policy contract

Implement a mandatory, deterministic `AdultPolicies` text gate:

    class AdultPolicies:
        def validate_text_task(
            self, task_type: str, instruction: str, text: str,
        ) -> PolicyDecision: ...

        def validate_text_output(
            self, task_type: str, output_text: str,
        ) -> PolicyDecision: ...

PolicyDecision is immutable and contains `allowed: bool` plus bounded normalized `reason_codes`.

Required baseline rules:

1. Only the three exact text task types are accepted.
2. Source text must be non-empty after trimming and within 20,000 characters.
3. Instruction is optional and limited to 4,000 characters.
4. Source/instruction must encode as strict UTF-8 and must not contain NUL or prohibited control characters.
5. Output is checked independently; policy approval of input does not imply output approval.
6. Missing policy dependency, malformed decision, unknown reason code, or policy exception fails closed.
7. Model output cannot modify/disable policy settings.

This baseline is an operation/input/output boundary policy. It does not claim semantic content moderation or perform a model-based safety review. No semantic moderation model is introduced in Stage 7. Any broader policy category/rule set requires a separately reviewed policy specification rather than hidden keyword heuristics.

Policy rejection before adapter dispatch creates no attempt. Policy rejection after adapter dispatch is recorded as a terminal rejected attempt using the existing AttemptTracker policy-rejection class; no artifact is published.

## 7. Deterministic evaluator

Add immutable EvaluationResult:

    @dataclass(frozen=True)
    class EvaluationResult:
        accepted: bool
        score: float
        checks: tuple[str, ...]
        reason_codes: tuple[str, ...]
        needs_revision: bool

No model-based reviewer is called in Stage 7. Evaluation must be deterministic and bounded.

Mandatory checks:

- output is a string and strict UTF-8 encodable;
- output is not empty/whitespace-only;
- output has no NUL or prohibited control characters;
- output is at most 20,000 characters;
- UTF-8 output is at most 80 KiB;
- operation matches the request and is one of the three supported text operations;
- output policy decision is allowed.

Return normalized reason codes only. Do not include rejected output text or provider details in the result, events, exception messages, or logs. The score is a deterministic fraction of mandatory checks passed; it is not a semantic quality score. `needs_revision` is true when the output fails a retryable deterministic quality check; policy rejection is terminal and does not request revision.

A successful HTTP/model response is not sufficient for acceptance.

## 8. One-attempt AdultTextPipeline

### 8.1 Public contract

The pipeline executes exactly one attempt per call:

    class AdultTextPipeline:
        def run_one_attempt(
            self,
            task: AdultTaskSnapshot,
            step_id: str,
            task_request: AdultTaskRequest,
            config_fingerprint: str,
            excluded_approach_keys: set[str],
        ) -> TextPipelineOutcome: ...

The pipeline is constructed with validated instances of AdultArtifactStore, AdultTaskStore, AdultModelRouter, AdultAttemptTracker, AdultPolicies, AdultEvaluator, and an explicit adapter registry keyed by exact adapter_id/version.

Preconditions:

- The caller has already claimed the task and holds a current active lease in AdultTaskStore.
- The supplied step is the task's current step and has operation matching task.task_type.
- The task and task_request refer to the same task type, payload ID, and keyed input fingerprint.
- The caller owns workflow-level state transitions and lease renewal. The pipeline does not claim/release/renew leases or change task status.

### 8.2 Execution order

1. Validate task/request/step identity, task type, input payload reference, and config fingerprint.
2. Load the input envelope by task_id + payload_id and validate it strictly.
3. Call AdultPolicies.validate_text_task. On denial, stop before routing and before AttemptTracker.begin.
4. Build a text-only CapabilityQuery and CostPolicy from the validated task budget. The pipeline must use the task's persisted max_provider_calls and max_cost_microusd; it cannot accept a caller override.
5. Ask AdultModelRouter for deterministic eligible routes, passing excluded approach keys.
6. Resolve the first eligible route to an adapter with exact adapter_id and adapter_version. Missing/mismatched adapter is a capability failure; never fall back to Workspace MODEL_* settings.
7. Call AttemptTracker.begin before invoking the adapter. This durably reserves the attempt and provider-call budget.
8. Invoke exactly one adapter. Do not loop over additional candidates or automatically retry.
9. Call AdultEvaluator on the in-memory TextAdapterResult.
10. If output policy rejects, finalize the attempt as policy-rejected; do not save any artifact.
11. If deterministic quality checks reject, finalize the attempt with a bounded retryable model-output error; do not save any artifact.
12. If accepted, encode strict UTF-8 and call AdultArtifactStore.save_output with MIME `text/plain` and the current attempt ID.
13. Finalize the attempt through AttemptTracker.succeed using the saved artifact ID, bounded usage, and cost. Only now return a published artifact reference.
14. If finalization fails after a temporary artifact was saved, discard that uncommitted artifact through a safe task/attempt-bound cleanup method. If cleanup cannot be confirmed, do not expose its ID; retention cleanup remains the bounded fallback.
15. Drop local references to input/output text before returning. Never persist plaintext outside the ArtifactStore's approved encrypted/transient behavior.

The pipeline returns a bounded TextPipelineOutcome containing task/step IDs, route identifiers, AttemptResult, EvaluationResult, and ArtifactRef only on accepted success. It never returns raw output text.

### 8.3 Attempt and failure mapping

- Input policy denial: no attempt; stable policy error.
- No eligible route: no attempt; stable offline/capability-unavailable outcome.
- Adapter timeout: `PROVIDER_TIMEOUT`, retryable.
- Provider/local adapter exception: stable `PROVIDER_ERROR` or `LOCAL_BACKEND_UNAVAILABLE`, retryable only where classified by the approved AttemptTracker mapping. Never include exception text.
- Invalid/empty/oversized text output: `MODEL_OUTPUT_INVALID`, retryable as a distinct approach (the Stage 5 tracker may classify it as unknown, but retryability remains explicit).
- Output policy denial: `POLICY_REJECTED`, non-retryable and terminal for the task.
- Output storage failure: stable `OUTPUT_STORAGE_FAILED`, bounded and classified without exposing paths or storage details.
- Attempt finalization failure: no success response; safely discard or leave an unreachable expiring artifact, and preserve the original tracker error.

AttemptTracker remains the only authority for attempt budgets, provider-call budgets, retry deduplication, failure classification, and durable success/failure outcomes. The pipeline must not calculate or mutate those counters itself.

### 8.4 Offline and zero-cost behavior

- Default TaskBudget has max_provider_calls=0 and max_cost_microusd=0.
- Local route can run only when the injected local adapter availability check returns exactly true.
- free_verified route is not eligible at provider-call budget 0.
- free_verified route requires explicit positive per-task max_provider_calls and current capability + zero-cost evidence.
- paid and unknown routes remain rejected regardless of credentials or adapter configuration.
- No route means no adapter call, no AttemptTracker.begin, and no output artifact.

## 9. Output artifact handling

Text output is stored as `text/plain`. The evaluator and output policy execute before AdultArtifactStore.save_output.

Add a narrow `discard_output(artifact_id, task_id, attempt_id)` operation to AdultArtifactStore for uncommitted outputs only. It must:
- validate opaque artifact/task/attempt IDs;
- verify the stored record belongs to the exact task, is kind=output, and has source_attempt_id equal to the supplied attempt;
- delete only that artifact's metadata and derived payload file/reference;
- accept no path from callers;
- safely discard transient-only output references;
- be idempotent for an already-absent artifact and fail closed on ownership mismatch.

Never delete successful artifacts as part of normal pipeline cleanup. Do not modify or migrate the payload-store schema for this capability.

## 10. Security and privacy invariants

- Raw text, instruction, generated text, provider messages, and provider response bodies exist only in bounded local variables and the explicitly in-memory TextAdapterResult.
- TextAdapterResult repr must not reveal text; no serialization helper is provided.
- No raw text in AdultTaskRequest metadata, TaskEvent, CheckpointSnapshot, AttemptSummary, attempt error summaries, logs, or exceptions.
- Only keyed fingerprints and opaque payload/artifact IDs cross persistence boundaries.
- The text adapter cannot choose provider/model/base URL/API key from request content.
- User instructions are untrusted data and never become system/developer instructions.
- The pipeline makes at most one adapter invocation per call and has no retry loop.
- All artifact retrieval remains task/ownership-scoped and uses existing chunk APIs.
- No live provider/network operation occurs during tests or CI.

## 11. Acceptance test matrix

### DTOs and input envelope
- TXT-MODEL-01: TextRequest accepts exactly text.analyze/text.generate/text.rewrite.
- TXT-MODEL-02: rejects image task types, invalid IDs, invalid fingerprints, and output limits above hard cap.
- TXT-MODEL-03: TextAdapterResult hides output text from repr and validates usage/cost.
- TXT-INPUT-01: canonical envelope round-trips text + optional instruction.
- TXT-INPUT-02: rejects duplicate/unknown JSON keys, malformed UTF-8/JSON, wrong schema/task type, NUL/control chars, empty text, and over-limit source/instruction.
- TXT-INPUT-03: no raw text enters SQLite task metadata, checkpoints, attempt rows/events, or logs.

### Policy and evaluator
- TXT-POLICY-01: unsupported task type is denied before routing.
- TXT-POLICY-02: source text and instruction bounds are enforced.
- TXT-POLICY-03: policy exception or malformed decision fails closed.
- TXT-POLICY-04: output policy runs after adapter and before artifact storage.
- TXT-EVAL-01: empty/whitespace output is rejected.
- TXT-EVAL-02: malformed Unicode, NUL/control chars, oversized characters/bytes are rejected.
- TXT-EVAL-03: accepted bounded text returns deterministic checks and score.
- TXT-EVAL-04: evaluator does not invoke a model or make network calls.

### Pipeline and attempts
- TXT-PIPE-01: each of analyze/generate/rewrite can complete one full fake-adapter path.
- TXT-PIPE-02: Task type mismatch between task, request, step, and route is rejected.
- TXT-PIPE-03: policy rejection before dispatch creates no attempt.
- TXT-PIPE-04: no eligible route in offline mode invokes no adapter and creates no attempt.
- TXT-PIPE-05: free_verified route is blocked at provider-call budget 0.
- TXT-PIPE-06: explicit positive provider-call budget permits only a current free_verified mock route.
- TXT-PIPE-07: AttemptTracker.begin occurs before adapter invocation.
- TXT-PIPE-08: model timeout/error records a sanitized terminal attempt outcome.
- TXT-PIPE-09: invalid model output is recorded as a failed retryable attempt; no artifact is published.
- TXT-PIPE-10: output policy rejection is a terminal policy attempt; no artifact is published.
- TXT-PIPE-11: accepted output is saved only after evaluation, then the attempt succeeds with the artifact ID.
- TXT-PIPE-12: storage/finalization failure never reports success and cleans up uncommitted output when possible.
- TXT-PIPE-13: duplicate approach keys are excluded; there is no internal retry loop.
- TXT-PIPE-14: cancellation/lease loss prevents final artifact publication.

### Regression and security
- TXT-REG-01: all MEM-*, TASK-*, CP-*, ATT-*, CAP-*, and payload tests remain green.
- TXT-REG-02: focused Adult tests and complete repository unit suite pass from a fresh process.
- TXT-REG-03: MCP schema scan and Python/optional browser OSV audits pass.
- TXT-REG-04: diff review confirms no .env, .vscode, .data, credential, or live database changes.
- TXT-REG-05: Stage 7 tests perform no live model/provider/network calls.

## 12. Required implementation order and gate

Implement in this order:

1. Re-read Stages 1–6 specs and rerun the complete Stage 6 baseline before editing.
2. Add/validate TextRequest, TextAdapterResult, PolicyDecision, and EvaluationResult DTOs; add unit tests.
3. Add strict text input envelope encoding/decoding and deterministic AdultPolicies baseline; add policy tests.
4. Add AdultEvaluator deterministic output checks and evaluator tests.
5. Add safe uncommitted text-artifact discard to AdultArtifactStore and isolated tests.
6. Add the mocked OmniRoute text adapter boundary; verify operation mapping, timeout, no retries, and no Workspace MODEL_* fallback.
7. Add AdultTextPipeline single-attempt routing/attempt/evaluation/publication flow; use fake adapters only.
8. Run focused tests for each changed module and all existing Adult tests. Fix any previous-stage defect immediately and rerun.
9. Review the complete diff and protected paths.
10. Run the full repository suite from a fresh process, then inspect CI tests, MCP scan, and both OSV audits.
11. Record Stage 7 PASS only when every TXT-* test, previous-stage regression, full suite, security scan, and CI pass.

No Stage 8 work begins until Stage 7 gate is recorded as PASS. No Stage 9 Executor/Supervisor work begins as part of Stage 7.
