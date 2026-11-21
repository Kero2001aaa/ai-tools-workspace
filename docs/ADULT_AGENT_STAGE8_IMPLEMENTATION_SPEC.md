# Adult Agent — Stage 8 Implementation Specification
## Image Input Validation, Image Adapters, Evaluator, and One-Attempt Pipeline

**Status:** Approved design — Step 1 PASS; Step 2 implementation in progress
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
- IMG-ADAPTER-01: exact operation-to-adapter method mapping.
- IMG-ADAPTER-02: exact adapter ID/version required; no fallback.
- IMG-ADAPTER-03: one invocation only; SDK retries disabled; no URL fetching.
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

### Step 2 — structural image validation and image policies (IN PROGRESS)

Implement a standard-library-only validator for PNG, JPEG, and static WebP containers. Enforce the 15 MiB input / 20 MiB output limits, 8,192-pixel dimension limits, 40-megapixel cap, signatures, structural lengths, CRCs where defined, terminal container markers, and rejection of animation/unsupported features. Return only immutable metadata and SHA-256. Add deterministic input/output policy checks for MIME allowlists and route-specific byte/dimension limits. No raster decoding or new dependency is permitted.



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

## 13. Design decisions requiring approval

The proposed design deliberately makes the following choices:

1. **No new imaging dependency:** PNG/JPEG/WebP structural validation uses the Python standard library. Full pixel decoding is out of scope; if it becomes necessary, the dependency and security impact must be reviewed separately.
2. **No provider URL retrieval:** image adapters must return in-memory bytes. URL-only provider results remain unavailable until an SSRF-safe fetch subsystem is separately designed.
3. **Envelope references:** one canonical task input envelope points to separately stored task-owned image and prompt payloads, preserving the existing one-primary-payload TaskStore contract.
4. **Centralized artifact ownership:** adapters return ephemeral results; only the pipeline validates and persists artifacts.
5. **Structural evaluation only:** no semantic image-quality or content-safety model is added.
6. **Strict zero-cost boundary:** local by default; remote free_verified only with explicit positive provider-call budget and current zero-cost evidence; paid/unknown always blocked.

**Approval record:** The user explicitly authorized Stage 8 implementation on 2026-11-21. Continue to enforce per-step local-test and CI gates; this approval does not authorize Stage 9 implementation.
