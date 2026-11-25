# Fixed-free text application boundary

`FixedFreeTextApplication` is a text-only application boundary over the existing
`FixedFreeTextService`. The operator injects one fixed service and a positive
provider-call budget. Caller methods accept no key, endpoint, state path, model,
namespace, principal or arbitrary tool selection. No transport is registered.

Methods implemented:

- `submit_text_task`: zero-call encrypted staging, returns redacted task metadata.
  `retain_payload=True` must be explicit. The omitted/false default is refused
  before task creation. This durable-only slice does not silently turn the
  specification's default ephemeral choice into persistent input storage.
- `run_task`: one explicit bounded fixed-free service segment, at most one
  provider attempt. Returns the redacted snapshot and fixed error code. No
  background runner, auto-certification, alternate route or provider retry loop.
- `get_task`, `list_tasks`, `get_attempts`: bounded safe metadata only. Pagination
  uses the existing store's opaque cursor, with at most 20 records per page.
  No source text, provider secrets, local paths, raw provider errors or content
  fingerprints are returned. The application must use a dedicated state store.
- `cancel_task`: the existing cancellation transaction. In-flight semantics
  remain owned by the existing executor and task store.
- `get_result_chunk`: explicit task-owned completed output read as bounded
  base64. Caller supplies a task ID, offset and byte bound, not a path or an
  arbitrary artifact ID. It intentionally returns generated result content.

This is NOT the full AdultAgent interface or Stage 11 acceptance. It does not
support ephemeral submission, image submission, a waiting-user approval resume,
MCP registration or gateway integration. It has a distinct class/module name so
that these missing parts cannot be mistaken for implemented contractual methods.
The existing five-failure local supervisor remains separate from the fixed-free
single-attempt service. No caller can use this boundary to certify a route.

Tests exercise real stores, pagination, zero-call submission, explicit retention,
no-route pause, cancellation, bounded task-owned artifact reads and a patched
fake-client completion with no duplicate call. No live AI result or provider
availability is claimed. No image generation or editing success is claimed.

Cross-stack HTTP contract tests also run the real SDK through the unchanged
production FreeRouteTransport. Only its lowest network transport is replaced
with an in-process HTTP mock. They inspect the serialized exact model, fixed
zero-price/privacy provider policy, non-streaming and no-tools/plugins request.
A mocked success becomes an encrypted task-owned result. Mocked 429 and 307
responses are sanitized, never followed/retried and never yield completion.
Terminal reruns remain one attempt. This closes an offline wiring check, not
live provider certification, semantic-quality evaluation or a real AI success.

An optional injected AdultFeedback bridge can now prepare a private proposal
only after run_task observes a new durably finished attempt. It must share the
same task store and fingerprint key. Default is disabled. The run result reports
private_proposal_created, not_applicable or proposal_failed separately from the
authoritative task outcome. A feedback-storage failure does not hide a completed
task or discard its result, and terminal reruns do not rebuild a proposal. Zero-
call pauses do not build one. Only the existing fixed generic lesson templates
qualify. Publication is never part of this hook and still requires a separate
explicit call. No integration installation, retry or background job is triggered.
