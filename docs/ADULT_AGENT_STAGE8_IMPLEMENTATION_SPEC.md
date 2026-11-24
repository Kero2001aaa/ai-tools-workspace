# Adult Agent — Stage 8 Implementation Specification
## Image Input Validation, Image Adapters, Evaluator, and One-Attempt Pipeline

**Status:** Code acceptance pending exact final-head CI after targeted privacy fixes. ChatGPT/Hindsight workflow verification remains open separately; it is not an IMG code gate. See the evidence reconciliation below.
**Branch:** `feature/adult-agent-mvp`  
**Parent specification:** `docs/ADULT_AGENT_IMPLEMENTATION_SPEC.md`  
**Precondition:** Stages 1–7 are PASS on the current branch head  
**Scope:** `image.analyze`, `image.generate`, and `image.edit`  
**Implementation authorization:** Approved by the user on 2026-11-21; implementation remains gated step-by-step.

---

## 1. Objective and completion boundary

Stage 8 adds a bounded, privacy-preserving image pipeline alongside the completed Stage 7 text pipeline.

At completion:

1. `image.analyze` accepts one validated image and produces bounded text.
2. `image.generate` accepts a bounded text prompt and produces one validated image.
3. `image.edit` accepts one validated image plus a bounded text prompt and produces one validated image.
4. Image bytes and prompts remain ephemeral in memory or encrypted payload storage; they never enter TaskStore metadata, checkpoints, attempt metadata, events, logs, or exceptions.
5. The pipeline verifies actual image format/signature, structural integrity, dimensions, pixel count, byte limits, task ownership, and route capability before dispatch.
6. Output is validated before it is persisted or published as a successful artifact.
7. Routing uses the existing Stage 6 capability registry/router and the task's persisted zero-cost budget.
8. Each pipeline invocation starts at most one attempt and makes at most one adapter call. There is no retry loop.
9. Tests use deterministic fixtures and fake adapters only; no provider/network call is permitted.
10. All Stages 1–7 tests, full repository tests, security scans, and CI remain green.

Stage 8 does not implement Stage 9 Executor/Supervisor orchestration, cross-invocation retry policy, checkpoint/resume workflow, Adult MCP/Gateway integration, live provider discovery, paid routes, or feedback publishing.

## 2. Preconditions and protected boundaries

Before implementation:

1. Re-read the Stage 1–7 specifications and relevant implementation.
2. Verify the current branch head, latest full test suite, and CI/security evidence.
3. Run the full existing suite as the Stage 8 baseline before editing.
4. If any earlier-stage defect is found, stop Stage 8, fix it in its owning stage, and rerun focused plus full regression tests until clean.

Protected boundaries:

- Never inspect, edit, stage, or replace `.vscode/`.
- Never read, edit, stage, or replace `.env`, credentials, or secrets. `.env.example` may contain safe empty defaults only.
- Never inspect, migrate, or modify live `.data/` databases or payloads.
- All tests use temporary directories and temporary SQLite files.
- Do not add dependencies unless the design is amended and explicitly approved.
- Do not install/configure provider services, activate billing, or make live provider/network calls.
- Keep all changes on `feature/adult-agent-mvp`; do not merge to `main`.

## 3. Exact operation contracts

| Task type | Inputs | Output | Adapter contract |
|---|---|---|---|
| `image.analyze` | One image payload | Text analysis | `ImageUnderstandingAdapter.analyze` |
| `image.generate` | One text prompt payload | One image | `ImageGenerationAdapter.generate` |
| `image.edit` | One image payload + one text prompt payload | One edited image | `ImageEditingAdapter.edit` |

The operation mapping is already defined in Stage 6 capability DTOs. A route must be verified for the exact task type, modality pair, MIME type, byte count, and dimensions. A generic vision label or a capability for another image operation is not sufficient.

No user-provided URL, filesystem path, provider name, model name, endpoint, MIME declaration, or adapter selection may override the route chosen by the Adult router.

## 4. Image input envelope and payload ownership

Stage 3's `AdultTaskRequest` and `StepRecord` expose one primary opaque payload reference. Stage 8 therefore uses a small canonical JSON envelope as that primary payload. The envelope contains only opaque references to separately stored task-owned payloads; it never contains image bytes or prompt text.

Schema version 1 uses exact keys:

- `schema_version`: integer 1 (boolean is not accepted).
- `task_type`: exactly one of `image.analyze`, `image.generate`, `image.edit`.
- `image_payload_id`: opaque task-owned image input reference, required only for analyze/edit.
- `prompt_payload_id`: opaque task-owned UTF-8 prompt reference, required only for generate/edit.

Exact operation-specific key sets:

- `image.analyze`: schema_version, task_type, image_payload_id.
- `image.generate`: schema_version, task_type, prompt_payload_id.
- `image.edit`: schema_version, task_type, image_payload_id, prompt_payload_id.

Encoding/decoding rules:

- Canonical UTF-8 JSON, sorted keys, compact separators, no NaN/Infinity.
- Strict UTF-8 and strict JSON parsing; duplicate keys, unknown keys, missing keys, wrong types, wrong operation, malformed identifiers, and trailing invalid data are rejected.
- The envelope is bounded to 4 KiB.
- Prompt bytes are strict UTF-8, non-empty after trimming, max 4,000 Unicode characters and 16 KiB encoded bytes; prohibited control characters are rejected. Prompt data is untrusted user data, never system/developer instructions.
- Each referenced payload is loaded using the exact task ID. Cross-task references, wrong payload kinds, missing/expired payloads, and ownership mismatches fail closed.
- The primary envelope reference is the one stored in the task request and current step. Sub-payload IDs remain inside the encrypted/ephemeral envelope and are never copied into task events or attempt metadata.
- The actual image MIME type and dimensions are detected from the bytes. Any MIME value supplied by a caller or stored as metadata is advisory only.

The existing `AdultArtifactStore` remains the only payload/artifact storage owner. Add only narrow metadata/read helpers if required; do not introduce a second payload database or generic file API.

## 5. Image validation contract

Add a deterministic standard-library image validator. Do not add Pillow or another decoder dependency in this stage unless a revised design is explicitly approved.

Supported formats are exactly:

- PNG: `image/png`
- JPEG: `image/jpeg`
- WebP: `image/webp`

Reject SVG, GIF, BMP, TIFF, AVIF, animated formats, wildcard MIME types, unknown formats, and format/signature mismatches.

Hard application limits (route limits may be stricter, never looser):

| Property | Input | Output |
|---|---:|---:|
| Encoded bytes | 15 MiB | 20 MiB |
| Width | 8,192 px | 8,192 px |
| Height | 8,192 px | 8,192 px |
| Total pixels | 40 megapixels | 40 megapixels |

Validation requirements:

1. Reject empty/truncated data, malformed signatures, invalid container lengths, invalid dimensions, zero dimensions, integer overflow, and trailing/contradictory container structure.
2. PNG: validate signature, chunk boundaries, IHDR constraints, required terminal structure, and chunk CRCs.
3. JPEG: parse marker segments with strict bounds, require a supported SOF dimension segment and a valid terminal EOI marker, and reject malformed segment lengths.
4. WebP: validate RIFF/WEBP signatures, declared RIFF size, chunk boundaries/padding, and dimensions from supported VP8/VP8L/VP8X headers. Reject animation and unsupported feature flags.
5. Enforce maximum bytes before parsing; enforce dimensions and pixel count before any decode-like processing or provider dispatch.
6. The validator returns a small immutable metadata DTO: detected MIME, byte count, width, height, and SHA-256 digest. It never returns image bytes in repr or serialization.
7. This is structural/container validation, not a claim that a full raster decoder has rendered every pixel. If full decode validation becomes a requirement, stop and propose a separately reviewed dependency/design change rather than silently adding a package.

The validator must not trust filenames, extensions, HTTP content types, user-declared MIME values, or provider metadata.

## 6. DTO and adapter boundaries

Preserve the Stage 6 image request DTO names and fields, but specialize the image adapter return types in Stage 8: `ImageUnderstandingAdapter.analyze` returns `ImageAnalysisResult`, while `ImageGenerationAdapter.generate` and `ImageEditingAdapter.edit` return `ImageAdapterResult`. The generic Stage 6 `AdapterResult` and `ArtifactResult` remain available for compatibility but are not used as Stage 8 image output containers. This avoids passing artifact IDs from adapters before the pipeline has validated and persisted the bytes.

Proposed DTOs:

- `ImageInputEnvelope`: immutable schema/task/reference DTO; no raw prompt or image bytes.
- `ValidatedImage`: detected MIME, byte count, dimensions, SHA-256 only.
- `ImageAnalysisResult`: analysis text hidden from repr; bounded usage and optional cost metadata.
- `ImageAdapterResult`: image bytes hidden from repr; declared output MIME, bounded usage, optional cost metadata. Bytes are in-memory only and have no generic serializer.
- `ImagePipelineOutcome`: bounded status, stable error code, attempt/artifact reference, and evaluation summary only. Never includes image bytes, prompt, or analysis text.

Adapter contracts:

- Analysis adapter receives `ImageAnalysisRequest` containing task/step IDs, image payload reference, and validator-derived MIME. It loads bytes from the task-scoped payload store and returns `ImageAnalysisResult`.
- Generation adapter receives `ImageGenerationRequest` containing task/step IDs and prompt payload reference. It returns `ImageAdapterResult`.
- Editing adapter receives `ImageEditRequest` containing task/step IDs, image/prompt references, and validator-derived MIME. It returns `ImageAdapterResult`.
- Adapters may not select a model, endpoint, credential, route, MIME policy, or storage path from request content.
- The pipeline, not the adapter, owns validation, artifact persistence, attempt finalization, and cleanup.
- The selected adapter is resolved by the exact `(adapter_id, adapter_version)` pair. No fallback adapter is allowed.
- One adapter method call maximum per pipeline invocation. Adapter-level automatic retries are disabled.

Provider adapters must use Adult-only configuration and fixed operation-specific message construction. Prompts/images are untrusted user data. Provider response URLs are not fetched by this stage; an adapter must return bounded in-memory bytes. If a provider can only return a URL, that route remains unavailable until a separately reviewed, SSRF-safe fetch design exists.

## 7. Policies and evaluator

Add deterministic `AdultImagePolicies` and `AdultImageEvaluator`. No moderation model, semantic image reviewer, OCR model, or extra provider call is introduced.

Input policy:

- allow only the three exact image task types;
- enforce envelope schema and operation-specific reference shape;
- enforce prompt and image byte/dimension/pixel bounds;
- require validator-derived supported MIME and route MIME compatibility;
- fail closed on policy exceptions or malformed policy decisions.

Output evaluation:

- `image.analyze`: validate analysis text as strict UTF-8, non-empty, bounded to 20,000 characters / 80 KiB, and free of prohibited control characters. Use deterministic structural checks only.
- `image.generate` / `image.edit`: validate output bytes using the same strict image validator and output limits; require detected MIME to be in the selected route's output MIME allowlist.
- Reject mismatches between operation, result DTO, declared MIME, detected MIME, dimensions, byte count, and digest.
- Evaluation result contains only bounded check IDs, reason codes, score, accepted flag, and retryability classification. Never include output text or image bytes.
- Policy denial is terminal and non-retryable. Invalid/corrupt/oversized model output is a failed attempt with the Stage 5-approved retryability classification; Stage 9 decides whether another pipeline invocation is allowed.
- Structural acceptance does not claim semantic quality or safety of generated image content.

## 8. Image pipeline execution contract

Implement one `AdultImagePipeline` mirroring the Stage 7 single-attempt contract.

Inputs: task snapshot, step ID, persisted `AdultTaskRequest`, configuration fingerprint, and excluded approach keys. Dependencies: TaskStore, ArtifactStore, router, AttemptTracker, image policies/evaluator, and exact adapter registry.

Required sequence:

1. Validate task/request/step identity, operation, current step, payload reference, and configuration fingerprint.
2. Load and strictly decode the task-owned image input envelope.
3. Load image/prompt sub-payloads using task-scoped references.
4. Validate prompt and image input before routing. No attempt is created for invalid input.
5. Build `CapabilityQuery` from exact operation, validator-derived image MIME/size/dimensions, keyed input fingerprint, and configuration fingerprint.
6. Build `CostPolicy` exclusively from persisted TaskBudget fields. Default provider-call budget remains zero; paid/unknown remain blocked.
7. Ask the existing router for eligible routes. No route means no adapter call, no attempt, and no output artifact.
8. Resolve only the exact adapter ID/version. Missing adapter means unavailable; no fallback.
9. Call `AttemptTracker.begin` before adapter invocation.
10. Invoke exactly one operation-specific adapter method exactly once.
11. Reject any positive reported cost before artifact persistence. Preserve zero-cost and usage metadata only in bounded allowlisted form.
12. Run the output evaluator and output policy. Do not persist rejected output.
13. Save accepted analysis as `text/plain`; save accepted generation/edit output using the validator-detected image MIME.
14. Finalize success through `AttemptTracker.succeed` with the artifact ID. Do not mutate task status or lease ownership inside the pipeline.
15. If finalization fails after save, call the existing task/attempt-bound `discard_output(artifact_id, task_id, attempt_id)`. If cleanup cannot be confirmed, do not report success; TTL remains the final cleanup boundary.

Stable error codes should reuse existing Stage 7/Stage 5 codes where semantics match and add only bounded image-specific codes such as `INVALID_IMAGE`, `IMAGE_OUTPUT_INVALID`, `PROMPT_INVALID`, `OUTPUT_STORAGE_FAILED`, `POLICY_REJECTED`, and `CAPABILITY_UNAVAILABLE`. No raw provider exception, prompt, URL, image bytes, path, or response body may escape.

## 9. Retry, lease, cancellation, and cost boundaries

- Stage 8 pipeline never claims, releases, renews, or changes a task lease.
- AttemptTracker remains the sole authority for attempt budgets, provider-call budgets, approach de-duplication, failure classification, and durable attempt outcomes.
- One pipeline invocation equals at most one attempt and one adapter call.
- No hidden SDK retry, HTTP retry, fallback model, URL fetch, or secondary image-processing provider call.
- Default `max_provider_calls=0` remains offline. A remote `free_verified` route is eligible only when the task explicitly has a positive provider-call budget and current capability plus zero-cost evidence.
- `max_cost_microusd=0`; paid and unknown routes remain blocked. A positive reported cost is rejected.
- Cancellation or lease loss must prevent artifact publication/finalization using the existing lease-generation fencing contract.
- Stage 9 alone owns multi-invocation retries, checkpointing, waiting for user approval, resume, and run-until-done orchestration.

## 10. Privacy and security invariants

- Image bytes, prompt text, generated image bytes, and analysis text are ephemeral in memory until accepted and persisted as encrypted/ephemeral artifacts.
- No raw prompt, image, base64, provider request/response, image URL, or file path in SQLite operational metadata, payload metadata, checkpoints, task events, attempt rows/events, logs, reprs, or exceptions.
- Only opaque task-owned payload/artifact IDs, keyed fingerprints, hashes, dimensions, byte counts, MIME types, normalized status/error codes, and bounded usage/cost counters cross metadata boundaries.
- Do not write plaintext images to disk. Existing ArtifactStore encryption/transient-cache behavior is mandatory.
- Do not trust provider-declared MIME, dimensions, or hashes; recompute from returned bytes.
- Do not fetch provider-returned URLs in Stage 8.
- Tests use synthetic image fixtures and fake adapters. No live provider/network calls.

## 11. Acceptance test matrix

### Envelope and payload ownership
- IMG-ENV-01: canonical round-trip for analyze/generate/edit.
- IMG-ENV-02: duplicate/unknown/missing keys, wrong schema, bool schema version, invalid UTF-8/JSON, wrong task type rejected.
- IMG-ENV-03: cross-task, missing, expired, wrong-kind sub-payload references rejected.
- IMG-ENV-04: prompt empty, malformed Unicode, controls, over-character/byte limits rejected.
- IMG-ENV-05: no prompt or sub-payload references leak into TaskStore metadata/events/attempt metadata beyond the primary opaque envelope reference.

### Image validator and policy
- IMG-VAL-01: valid PNG/JPEG/WebP fixtures detected with exact MIME/dimensions/hash.
- IMG-VAL-02: MIME spoofing, wrong signature, SVG/GIF/BMP/TIFF/AVIF/animated inputs rejected.
- IMG-VAL-03: truncated headers, malformed chunk/segment lengths, CRC errors, RIFF size errors, invalid dimensions rejected.
- IMG-VAL-04: byte, width, height, and pixel limits enforced before routing.
- IMG-VAL-05: policy exceptions and malformed decisions fail closed.
- IMG-VAL-06: route-specific MIME/size/dimension restrictions enforced.

### Adapter and pipeline
- IMG-ADAPTER-01: exact operation-to-adapter method mapping for analyze, generate, and edit.
- IMG-ADAPTER-02: exact `omniroute` adapter ID/version and verified operation/cost class required; no fallback or user-selected model/endpoint/credential.
- IMG-ADAPTER-03: one invocation only; timeout and zero retries; redirects disabled; no URL fetching.
- IMG-ADAPTER-04: images/prompts are loaded with task ownership checks; input image MIME is revalidated before dispatch.
- IMG-ADAPTER-05: image analysis uses inline base64 image content; generation/edit use their exact image endpoints and return one inline base64 image.
- IMG-ADAPTER-06: URL-only, malformed, duplicate-key, invalid-MIME, corrupt, and oversized provider outputs are rejected without exposing source content.
- IMG-ADAPTER-07: usage/cost metadata is bounded and allowlisted; reported positive cost is preserved for pipeline enforcement.
- IMG-ADAPTER-08: adapter/provider exceptions and timeouts are sanitized; tests make no live network calls.
- IMG-PIPE-01: image.analyze succeeds with fake adapter and publishes text/plain only after evaluation.
- IMG-PIPE-02: image.generate succeeds with valid synthetic image bytes.
- IMG-PIPE-03: image.edit succeeds with valid image + prompt and publishes validated image bytes.
- IMG-PIPE-04: invalid input rejected before route/attempt.
- IMG-PIPE-05: no route / provider-call budget zero makes no adapter call and creates no attempt.
- IMG-PIPE-06: paid/unknown and positive-cost results are blocked.
- IMG-PIPE-07: corrupt, MIME-mismatched, oversized, or dimension-invalid output is never published.
- IMG-PIPE-08: output policy rejection is terminal and stores no artifact.
- IMG-PIPE-09: adapter exceptions are sanitized.
- IMG-PIPE-10: output storage/finalization failure never reports success and discards only the matching uncommitted artifact.
- IMG-PIPE-11: stale lease/cancellation prevents publication.
- IMG-PIPE-12: no raw image/prompt/analysis data in outcomes, reprs, persistence, logs, or exceptions.

### Regression and security
- IMG-REG-01: all MEM-*, TASK-*, CP-*, ATT-*, CAP-*, TXT-* and payload-store tests remain green.
- IMG-REG-02: focused image tests, all Adult tests, and full repository suite pass from a fresh process.
- IMG-REG-03: MCP scan, OSV Python audit, and optional browser audit pass.
- IMG-REG-04: complete diff contains no protected-path, live-data, secret, or plaintext-payload changes.
- IMG-REG-05: tests and CI perform no live model/provider/network calls.

## 12. Required implementation order and gate

### Step 2 — structural image validation and image policies (PASS)

Implement a standard-library-only validator for PNG, JPEG, and static WebP containers. Enforce the 15 MiB input / 20 MiB output limits, 8,192-pixel dimension limits, 40-megapixel cap, signatures, structural lengths, CRCs where defined, terminal container markers, and rejection of animation/unsupported features. Return only immutable metadata and SHA-256. Add deterministic input/output policy checks for MIME allowlists and route-specific byte/dimension limits. No raster decoding or new dependency is permitted.



### Step 3 — deterministic image evaluator (PASS)

Add `AdultImageEvaluator` with no semantic model, moderation call, OCR, network access, or secondary provider invocation.

- For `image.analyze`, accept only `ImageAnalysisResult`; enforce strict UTF-8, non-empty text, the 20,000-character / 80 KiB bounds, permitted whitespace controls, and the existing `AdultImagePolicies.validate_analysis_output` decision.
- For `image.generate` and `image.edit`, accept only `ImageAdapterResult`; validate the actual returned bytes using `validate_image(..., output=True)`; require the declared MIME to match the detected MIME; enforce route output MIME, byte, width, height, and pixel constraints through `AdultImagePolicies.validate_image_output`.
- The selected route's four output constraints (`output_mime_types`, `max_bytes`, `max_width`, `max_height`) are mandatory for `image.generate` and `image.edit`. If any is absent, evaluation terminates with `route_policy_missing`; no application-wide default may substitute for a missing route constraint.
- Invalid route constraints (including booleans/non-integers/non-positive limits, limits above hard application ceilings, malformed/empty MIME allowlists, or unsupported MIME values) are terminal policy errors. Valid stricter route limits are enforced exactly.
- Return only the existing bounded `EvaluationResult` DTO: accepted flag, pass-fraction score, normalized check IDs, normalized reason codes, and retryability classification. Never include output text, image bytes, or exception content.
- Treat corrupt/invalid model output and route output-contract mismatches as revision candidates. Treat unsupported tasks, wrong adapter DTOs, unavailable/malformed policy decisions, and malformed route policy configuration as terminal. No output is semantically scored.

Step 3 gate closed: the user confirmed the Adult suite (260 tests) and full repository suite (399 tests) passed; CI #367 passed unit tests, MCP inventory export, MCP definition scan, OSV Python audit, and optional browser audit.


### Step 4 — image adapter boundaries and mocked Adult-only provider wrappers (IMPLEMENTED; STAGE CLOSURE PENDING)

Implement three operation-specific adapters in `agent/adult/integrations/omniroute_image.py`:

- `OmniRouteImageUnderstandingAdapter.analyze(ImageAnalysisRequest)` for `image.analyze`;
- `OmniRouteImageGenerationAdapter.generate(ImageGenerationRequest)` for `image.generate`;
- `OmniRouteImageEditingAdapter.edit(ImageEditRequest)` for `image.edit`.

Adapter boundary requirements:

1. Accept only the exact `omniroute` adapter ID/version `1`, the matching operation, a verified route with `free_verified` cost class, and a non-empty registered provider/model identity. Never accept caller-selected model, provider, endpoint, credentials, or route fields.
2. Use only the supplied `AdultOmniRouteConfig`; do not read workspace `MODEL_*` settings or select a credential from payload content. Construct the HTTP client lazily with a 30-second timeout, explicit zero transport retries, and redirects disabled.
3. Load each image and prompt through `AdultArtifactStore.load_input(payload_id, task_id)`. Revalidate input image structure and MIME against the request; decode prompt bytes using `decode_image_prompt`. Reject missing, cross-task, wrong-kind, malformed, or MIME-mismatched payloads before dispatch.
4. `image.analyze` makes one `chat/completions` request with a fixed system instruction and an inline base64 data URL derived from the owned image bytes. The image is data, not an instruction source; never pass a remote image URL.
5. `image.generate` makes one `images/generations` request. `image.edit` makes one multipart `images/edits` request with the owned image bytes and decoded prompt. Both request exactly one image.
6. Accept one inline `data[0].b64_json` result only. Reject URL-only results; do not fetch URLs. Bound the streamed JSON response, decode base64 strictly, validate returned image bytes structurally, and derive the declared MIME from the bytes rather than a provider declaration.
7. Copy only allowlisted usage counters and cost telemetry. Convert provider USD cost to microusd conservatively (round any positive fractional microusd upward); malformed reported cost metadata is a sanitized failure. Never assume a missing cost value is proof of zero-cost capability.
8. Sanitize all provider, HTTP, and client errors to stable codes. No response body, prompt, image bytes, URL, secret, or raw exception may escape. One adapter method invocation causes one HTTP request; no retry, redirect, fallback, URL retrieval, or secondary provider call is allowed.

Tests must use fake clients/responses only; no live provider/network call is permitted. Step 4 does not add `AdultImagePipeline`, retry orchestration, or task/lease mutations.


After explicit design approval, implement in this order:

1. Revalidate Stages 1–7 and record the full-suite baseline.
2. Add image DTOs, strict envelope codec, and task-scoped payload metadata helpers if necessary.
3. Add deterministic PNG/JPEG/WebP structural validator and image input/output policies.
4. Add deterministic image evaluator and focused tests.
5. Add image adapter boundaries and mocked Adult-only provider adapter wrappers.
6. Add one-attempt `AdultImagePipeline`.
7. Add comprehensive pipeline/adapter/security tests.
8. Run focused tests after each unit; then all Adult tests and full repository regression.
9. Review complete diff and verify protected paths.
10. Verify CI unit tests, MCP security scan, OSV Python audit, and optional browser audit.
11. Record Stage 8 PASS only when every IMG-* test, all prior-stage regression, full suite, and CI/security checks pass.

No Stage 9 implementation is included in Stage 8.

---

## Evidence reconciliation, 2026-11-24

Stage 8 is **not PASS**. This section separates remote commits, local test evidence, CI evidence, and external review. A passing local suite does not establish CI or completion of every acceptance requirement.

### Remote implementation baseline

Branch: `feature/adult-agent-mvp`. No merge to `main` is part of this work.

| Work | Remote commit | Verification |
|---|---|---|
| Image adapter Decimal overflow handling | `1c85feb` | Earlier full suite: 412 tests OK; CI #377 succeeded |
| RouteCandidate output-constraint propagation | `7553b7c64cb3ccee1278c5f4ab6ff5164504203f` | Full suite: 414 tests OK; CI #378 succeeded |
| One-attempt image pipeline and initial tests | `510235b84044287086d6fc6071703e0684a1f652` | Full suite: 430 tests OK; CI #379 succeeded |
| Prompt validation and attempt-finalization hardening | `6e56e550da0cc7aea8998adbdc183d1d6f4fe050` | 24 pipeline tests and full suite of 438 tests OK in Codespace; CI #380 succeeded |

Verified CI pages:

- #377: https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45381630391
- #378: https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45388892392
- #379: https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45392719766
- #380: https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45393993104

The remote pipeline dispatches analyze/generate/edit through the exact adapter ID/version, validates task-owned image and prompt input before routing, passes explicit route output constraints to the evaluator, rejects positive reported cost before persistence, and saves output only after acceptance. Follow-up hardening validates prompt bytes before routing, maps provider timeout codes, finalizes evaluator failures, and preserves non-retryable evaluator decisions.

CI uses the repository quality/security workflow, including unit tests, MCP inventory/definition checks, and dependency audits. These successful runs apply to their listed commits, not to later local changes.

### Acceptance-test delivery and local validation

Additional changes to `tests/test_adult_image_pipeline.py`, based on remote `6e56e55`, were delivered through the authenticated repository file editor in commit `52216dfa6fdf2be7d14856345367f5b089cae228`. The fetched remote file exactly matches the locally tested file. The test change adds 143 lines and no production-code changes. CI for this newer commit is not yet recorded here. Validation in a temporary local checkout used Python 3.12 and SQLite 3.53.1:

- Pipeline module: **31 tests OK**.
- Full repository suite: **445 tests OK**.
- `git diff --check`: clean.

These additional tests add:

- Real SQLite TaskStore, AttemptTracker and ArtifactStore lifecycle checks for all three operations, with fake adapters and synthetic images.
- Cancellation and expired-lease checks during adapter execution, with no successful finalization and matching artifact cleanup.
- Task-owned input failures for cross-task, wrong-kind, expired and missing image references, before routing or attempts.
- Fail-closed input policy exceptions/malformed decisions, terminal output-policy denial and sanitized adapter errors.
- Operational SQLite checks excluding prompt text, analysis text and image/prompt sub-payload IDs.

Existing component tests separately cover envelope and prompt validation, PNG/JPEG/WebP structural validation, image policy/evaluator rules, provider response parsing and bounded transport behavior, exact router input restrictions, positive provider-call budgets, expired cost evidence, and paid/unknown route rejection. They passed as part of the local full suite. Component coverage is not represented as a new integrated test where no such test was added.

### Acceptance reconciliation follow-up

The explicit coverage follow-up is remote commit `23c32c3b32449cb71cf6435e1232fd8ea53a3dda`. The fetched test file matches the locally tested file byte-for-byte. No production code changed. It expands task-owned reference failures to prompt references and directly tests BMP/TIFF/AVIF/GIF/SVG signatures, unknown-cost routes, corrupt output, declared/detected MIME disagreement, and route output-byte limits. Local validation: **34 pipeline tests OK**, **448 full-suite tests OK**, clean `git diff --check`, Python 3.12. The full suite includes prior Adult stages and payload-store tests.

CI #381 (`52216df`) and #382 (`31a803bf`) completed successfully, including unit tests, MCP inventory/definition checks, Python dependency audit and optional browser dependency audit:

- https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45402078669
- https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45402359104
- New test follow-up CI #383: https://github.com/Kero2001aaa/ai-tools-workspace/actions/runs/45404410864. In progress when this reconciliation was prepared; do not infer its result or the result of this documentation commit.

### IMG matrix evidence map

This is a coverage map, not a declaration that Stage 8 passed. All test names below are in the repository and were inspected. Component and integrated checks are distinguished. The test modules share the prefix `tests/test_adult_`.

| Requirement | Evidence and scope |
|---|---|
| IMG-ENV-01 | `image_input`: canonical round-trip for all operations. |
| IMG-ENV-02 | `image_input`: duplicate/unknown/missing keys, bool version, wrong operation/value types, UTF-8/JSON/trailing/noncanonical data. |
| IMG-ENV-03 | `image_pipeline.test_task_owned_subpayload_failures_never_route`: real transient cache, image and prompt references, cross-task/wrong-kind/expired/missing. Adapter tests separately check ownership. |
| IMG-ENV-04 | `image_input`: empty/overlong/control prompts, UTF-8 and byte overflow; pipeline rejects invalid prompt before routing. |
| IMG-ENV-05 | Real pipeline lifecycle inspects SQLite dump for prompt/analysis text and sub-payload IDs; DTO repr checks in `image_input`. This is not exhaustive dynamic logging instrumentation. |
| IMG-VAL-01 | `image_validator.test_valid_png_jpeg_and_webp_return_detected_metadata_and_digest`: synthetic structural fixtures, MIME/dimensions/hash presence. Not full raster decoding. |
| IMG-VAL-02 | Pipeline unsupported-signature cases; validator animation, spoofing and unsupported-feature cases. |
| IMG-VAL-03 | Validator PNG CRC/chunk/truncation, JPEG segment/scan, WebP RIFF/padding/header/dimension cases. |
| IMG-VAL-04 | Validator global input/output byte, dimension/pixel bounds; router exact input limits; pipeline invalid input before routing. Combined component coverage, not every limit in a separate integrated case. |
| IMG-VAL-05 | Input pipeline policy exception/malformed-decision checks; evaluator output policy exception/malformed-decision checks. |
| IMG-VAL-06 | `router.test_image_routes_enforce_exact_mime_byte_and_dimension_constraints`; evaluator and pipeline route output constraints. |
| IMG-ADAPTER-01 | Pipeline operation dispatch and adapter analyze/generate/edit endpoint tests. |
| IMG-ADAPTER-02 | `omniroute_image_adapters.test_wrong_route_type_cost_class_adapter_identity_and_version_are_rejected`; fixed route/model inspection. |
| IMG-ADAPTER-03 | Lazy HTTP client no-retry/redirect configuration; single-call and timeout/redirect/URL-only response tests. |
| IMG-ADAPTER-04 | Adapter image/prompt ownership and declared/detected MIME checks before fake-client call. |
| IMG-ADAPTER-05 | Adapter request tests inspect inline analysis base64, generation payload and edit multipart endpoint/body. |
| IMG-ADAPTER-06 | Strict duplicate-key, malformed/base64/URL-only/corrupt/oversized response tests. MIME is recomputed from bytes, never trusted from provider claims. |
| IMG-ADAPTER-07 | Adapter usage allowlist/bounds and positive tiny-cost rounding; DTO counter bounds and Decimal overflow sanitization. |
| IMG-ADAPTER-08 | Fake-client provider error/timeout tests, stable codes, single call; no live provider requests. |
| IMG-PIPE-01 | Analyze success and real store/tracker lifecycle publish accepted `text/plain`. |
| IMG-PIPE-02 | Generate success with synthetic PNG and real store/tracker lifecycle. |
| IMG-PIPE-03 | Edit dispatch and real store/tracker lifecycle. |
| IMG-PIPE-04 | Invalid image/prompt/reference cases assert router/attempt/adapter not called. |
| IMG-PIPE-05 | Empty route creates no attempt; tracker refusal prevents adapter; real router positive-provider-budget requirement covered separately. Budget-zero is component coverage, not a separate real-router pipeline test. |
| IMG-PIPE-06 | Explicit paid/unknown pipeline routes and positive result cost rejection; tracker/router cost evidence tests separately. |
| IMG-PIPE-07 | Pipeline corrupt/declared-MIME/route-byte/dimension/MIME-allowlist output cases assert no storage. |
| IMG-PIPE-08 | Pipeline output policy denial terminal and never persisted. |
| IMG-PIPE-09 | Pipeline exception marker absent from outcome/tracker arguments; stable provider/timeout codes. |
| IMG-PIPE-10 | Storage failure prevents success; finalization failure calls matching task/attempt-bound discard. Payload-store tests check discard ownership. |
| IMG-PIPE-11 | Real TaskStore/AttemptTracker cancellation and expired-lease cases for all operations discard saved output and cannot finalize success. |
| IMG-PIPE-12 | DTO hidden fields, bounded outcome, sanitized adapter errors and real SQLite metadata checks. Static code inspection complements tests; no exhaustive log/exception instrumentation claim. |
| IMG-REG-01 | Fresh-process full suite includes prior-stage and payload-store tests, 448 OK locally. |
| IMG-REG-02 | Fresh-process focused pipeline 34 and full suite 448 OK locally; full suite includes all Adult modules. |
| IMG-REG-03 | CI381/382 all workflow checks succeeded. Follow-up test/doc exact-head CI remains to be verified. |
| IMG-REG-04 | Stage8 changed-path review contains only Adult source/tests and Stage8 documentation. No protected path, live database, credential, dependency or provider configuration change was made in this follow-up. |
| IMG-REG-05 | Image tests use synthetic fixtures/fake adapters and clients. CI security audits access package/audit services; no live model/provider call is made. The broader full suite uses local loopback HTTP test servers. |

### Code acceptance assessment and independent workflow status

The previous matrix qualifications were reviewed against the actual requirements:

- Component versus integrated coverage is not itself a failure. Input limits are checked by `validate_image` before the router call; exact route limits are checked by the real router and evaluator. Structural-only validation is an explicit design boundary, not a missing raster decoder.
- IMG-PIPE-05 now has a direct real-registry/router image-pipeline test: a current verified free route with provider budget zero creates no attempt, adapter call or output; the same route succeeds when only the budget changes to one.
- The privacy review found and fixed a genuine IMG-PIPE-12 gap: arbitrary tracker finalization exceptions could escape after cleanup. Both success and failure finalization now raise only stable `ImagePipelineError("finalization_failed")`, suppressing traceback chaining. Saved-output cleanup remains task/attempt bound and never reports success when finalization fails. A failed cleanup still leaves TTL as the specified final boundary.
- Invalid-analysis Unicode DTO validation now suppresses the raw encoding-error cause. Tests check public formatted exceptions and hidden DTO values, alongside existing real SQLite metadata checks. No exhaustive dynamic logger instrumentation is required by the spec. The pipeline/evaluator/input code does not log raw values; adapter failures use stable errors.
- Security audits accessing package/audit services and repository tests using loopback servers do not constitute live model/provider calls. The image adapter tests use fake clients. No live model/provider test was performed.
- Stage8 changed-path review includes only Adult source/tests and its specification. No protected path, live data, credential, dependency or provider-configuration change was made.

Local post-fix evidence: **37 pipeline tests**, **88 focused image tests** (validator/input/policy/evaluator/pipeline modules), and **452 full-suite tests OK**, with clean `git diff --check`. The final remote files are compared byte-for-byte to the tested versions. Exact final-head CI must still be verified after this documentation commit; prior CI384 passed all checks at `789e552`.

**Code verdict:** the actionable acceptance gaps identified in the matrix review are fixed, with targeted tests. No known code-acceptance blocker remains beyond verifying the exact resulting head's CI/security run. Component coverage and the stated structural-validation scope are not extra gates. Record code acceptance only once that CI check succeeds.

**Separate user-requested workflow:** final ChatGPT review and Hindsight verification remain incomplete. The submitted New chat with Think selected ended in a connection/no-response alert, without an assistant review. No actual Hindsight invocation was observed. These are not named IMG code acceptance requirements and must not be presented as failing code tests or converted into new code gates. Do not repeat exhausted connection recovery without a new signal. No Stage9 work or main merge is included.

Codespace remote-connection/setup failures were bypassed for delivery using the authenticated github.com editor. The Codespace was not stopped. The GPT connection failure is a missing review result, not a failing code test.

---

## 13. Design decisions requiring approval

The proposed design deliberately makes the following choices:

1. **No new imaging dependency:** PNG/JPEG/WebP structural validation uses the Python standard library. Full pixel decoding is out of scope; if it becomes necessary, the dependency and security impact must be reviewed separately.
2. **No provider URL retrieval:** image adapters must return in-memory bytes. URL-only provider results remain unavailable until an SSRF-safe fetch subsystem is separately designed.
3. **Envelope references:** one canonical task input envelope points to separately stored task-owned image and prompt payloads, preserving the existing one-primary-payload TaskStore contract.
4. **Centralized artifact ownership:** adapters return ephemeral results; only the pipeline validates and persists artifacts.
5. **Structural evaluation only:** no semantic image-quality or content-safety model is added.
6. **Strict zero-cost boundary:** local by default; remote free_verified only with explicit positive provider-call budget and current zero-cost evidence; paid/unknown always blocked.

**Approval record:** The user explicitly authorized Stage 8 implementation on 2026-11-21. Continue to enforce per-step local-test and CI gates; this approval does not authorize Stage 9 implementation.
