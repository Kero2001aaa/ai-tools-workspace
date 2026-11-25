# Fixed-free text application boundary

`FixedFreeTextApplication` is a text-only application boundary over the existing
`FixedFreeTextService`. The operator injects one fixed service and a positive
provider-call budget. Caller methods accept no key, endpoint, state path, model,
namespace, principal or arbitrary tool selection. No transport is registered.

Methods implemented:

- `submit_text_task`: zero-call encrypted staging, returns redacted task metadata.
  Default `retain_payload=False` stages input only in the existing process-local
  cache. `True` explicitly selects encrypted persistent input with configured key.
  Non-boolean retention values are rejected before task creation.
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
support image submission, a waiting-user approval resume,
MCP registration or gateway integration. It has a distinct class/module name so
that these missing parts cannot be mistaken for implemented contractual methods.
The existing five-failure local supervisor remains separate from the fixed-free
single-attempt service. No caller can use this boundary to certify a route.

Tests exercise real stores, pagination, zero-call submission, ephemeral/default and explicit encrypted retention,
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

Task-owned result reads now pass an explicit expected task ID to the existing
artifact store. Persistent and transient output ownership is checked under its
lock before decryption or cache access. A corrupted or forged task-to-artifact
pointer cannot read another task's output. The optional low-level owner argument
preserves existing trusted library callers; the application/CLI boundary always
supplies it. Regression tests exercise both storage modes and reject a cross-task
pointer before any plaintext read. This is boundary hardening, not evidence of an
observed external attack or previous disclosure.

Zero-call staging now uses the existing executor's general `stage` method.
`stage_encrypted` remains the compatible explicit encrypted entry point, and the
operator CLI keeps using `retain_payload=True`. The fixed service's low-level
submit default stays encrypted for existing trusted callers, while the application
boundary's default is ephemeral. No plaintext input row/file is persisted for an
ephemeral request. This slice still uses the service's configured fingerprint and
output-encryption key, even for transient input; keyless ephemeral service setup
is not implemented or claimed.

If transient input is absent after cache eviction, expiry or process restart, a
checkpointed fixed-free task moves atomically to needs_input before any attempt.
Repeated run calls keep that state without provider work. Wrong keys and retained
input integrity/expiry errors are not reclassified as transient loss. General replacement
input resume is not implemented; the separately documented exact zero-attempt
ephemeral reupload is available, as is cancellation. Tests include
same-process fake completion, no input plaintext on disk, and an actual new Python
process observing needs_input with zero attempts. No MCP tool is registered.

`restore_input_and_run` accepts the same text and instruction only for a lost,
zero-attempt ephemeral task in needs_input. It restores the original transient
reference, persists one guarded queue transition and runs one bounded segment.
It is not `resume_task`, waiting-user approval or a general changed-input API.
See EPHEMERAL_INPUT_RESTORE.md for failure/restart semantics and boundaries.

Completed and cancelled application tasks now call the existing input-only
cleanup method. This removes retained/transient original input, not generated
outputs, task history or checkpoints. The run/restore response includes a
separate input_cleanup status. Cancellation keeps its existing snapshot fields
and adds that status. Cleanup failure is a fixed code: it cannot mask durable
completion/cancellation, delete output, publish feedback or trigger another
provider attempt. An explicit terminal run/cancel can retry cleanup safely.
Metadata reads remain read-only. Failed/paused tasks keep bounded input according
to their existing retention policy because they may still need recovery.
This application wiring does not claim a background retention janitor or change
the existing keyless CLI cancel implementation; direct low-level service/store
callers remain responsible for their input lifecycle.
