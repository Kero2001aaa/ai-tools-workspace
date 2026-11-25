# Image intake and bounded executor composition

Status: offline composition over the existing image pipeline, validator and
evaluator. No transport registration, no live provider probe, no supervision
and no background execution. Image capability availability remains a separate
verified precondition; the composition only routes through the existing
registry/router evidence checks.

## Gap closed

The deterministic one-attempt `AdultImagePipeline` existed with its validator
and evaluator, but no operational path created an image task, stored its
validated input over the real stores, ran one bounded attempt, or restored a
lost process-local input. Mock-only pipeline tests could not cover that
composition.

## Fingerprint binding

Text tasks fingerprint deterministic canonical input bytes before creation.
Image envelope bytes cannot be known before creation because the artifact
store chooses payload identifiers. The composition therefore binds the durable
`input_fingerprint` to a deterministic, content-addressed canonical envelope:
`image_payload_id`/`prompt_payload_id` are the truncated SHA-256 of the exact
validated image bytes and encoded prompt. Equal validated content always
yields equal canonical bytes, so the keyed fingerprint binds exact content
before any task or payload identifier exists. The stored operational envelope
references the actual task-scoped payload identifiers; intake re-derives the
canonical fingerprint from the read-back stored content before attachment, and
the executor re-derives it again before any attempt.

## Additive contract

1. `AdultImageExecutor.create` validates content per task type
   (`image.analyze`: image only; `image.generate`: prompt only; `image.edit`:
   both) through the existing validator and prompt encoder, then creates the
   task with the content-bound fingerprint. Invalid input creates no task.
2. `AdultImageIntake.attach` under a claimed lease validates content and
   fingerprint, stores image/prompt/envelope inputs with the task's retention
   policy, reads each back through the task-owned input path, and attaches the
   envelope reference once. A failed commit removes only payloads created by
   that call; a payload the task now owns is never deleted; cleanup and
   readback failures are surfaced with stable error codes.
3. `AdultImageExecutor.stage` creates, attaches and checkpoints without any
   route selection or adapter call. Ephemeral input uses the existing
   process-local cache only; encrypted retention requires the configured key
   before creation, exactly like the text executor.
4. `AdultImageExecutor.run` owns one lease and at most one pipeline attempt.
   The prior-attempt boundary is checked first: after a committed attempt,
   even lost cached input surfaces as `prior_attempt_requires_recovery`,
   never as a re-upload request that restore would refuse. The step's
   original configuration fingerprint is required and the recovered
   pre-attempt step guard is preserved. The execution step is marked running
   only once an attempt actually committed, so a zero-attempt outcome leaves
   it pending and genuinely runnable after an exact restore; a stranded
   running step still requires an actual expired-lease recovery checkpoint.
   Only the pipeline's evaluated, committed artifact becomes the task
   result. Non-success outcomes checkpoint and pause; cancellation before or
   during an attempt wins and is never completed.
5. A missing process-local input moves an ephemeral queued/checkpointed task
   to `needs_input` with zero adapter calls, through the existing transition
   or claim/release lease path. Input lost after the pre-run check, surfacing
   as a zero-attempt pipeline outcome, is converted to the same recoverable
   `needs_input` rather than a paused end. Wrong keys, authentication errors
   and retained input expiry are never softened into replacement permission.
6. `restore_input` never deletes or overwrites surviving input. It validates
   caller content and re-derives the canonical fingerprint first, decodes any
   surviving envelope and keeps its exact bytes and original references,
   restores only genuinely missing parts under those references, and lets
   the artifact store's byte-identity guard reject any replacement of live
   different bytes as `restored_input_mismatch` before any insertion. All
   checks complete before any insertion, so a failed, stale or expired
   restore leaves valid input untouched. After a full loss the rebuilt
   references are content-addressed, making a lost commit response safe to
   retry byte-identically. Queueing goes through the existing
   `resume_restored_ephemeral` transaction, which atomically revalidates
   status, retention, zero attempts, payload, fingerprint, version, deadline
   and budget; no reference, fingerprint, budget, deadline, approval or
   attempt is invented, and a repeated restore after a successful queue is
   refused before any dispatch.

## Explicitly not claimed

No live image generation/analysis success, no provider availability, no MCP
registration or gateway exposure, no supervision/retry loop, no approval
contract, no changed-input resume, no semantic moderation. Terminal input
cleanup remains the application boundary's responsibility, as documented for
the text side. The tracker's atomic pre-attempt route revalidation is
currently text-specific; an image variant is proposed shared-file work for the
integrator, not part of this additive lane.

## Tests

`tests/test_adult_image_intake.py` and `tests/test_adult_image_executor.py`
run the composition over real task/artifact stores with a deterministic
offline adapter: completion and reopen persistence for all three task types,
no duplicate dispatch on terminal rerun, unavailable-route pause without an
attempt, missing/lost input handling, exact restore after process-local
loss, partial cache loss restoring only the missing part under the surviving
envelope's original references, stale/deadline restore failure preserving
surviving input, conflicting live bytes refusing restore without mutation,
terminal restore refusal, cache loss after a committed attempt staying an
explicit recovery boundary, the post-precheck cache-loss race yielding
zero-attempt `needs_input` then restore and completion, changed-content
rejection, cancellation before and during an attempt, one bounded failure
without retry, policy rejection without an artifact, fingerprint binding
before storage, commit/cleanup/readback failure handling, and encrypted
retention roundtrip across a reopened store. Composition wiring revalidates
the current route in the tracker transaction through the registry hook
provided by the integrator's shared change, and the pipeline attempt is
fenced by the worker lease.

## Integrator capacity guard

The cache uses normal least-recently-used eviction, which restoration must not
trigger. The restore insertion set is checked under the existing artifact and
cache locks for all live-reference conflicts and available entries/bytes before
any insertion. Insufficient capacity fails without evicting surviving task
input, unrelated input or output. This focused internal guard uses existing
store locks/cache accounting; no schema or persistence policy changes.
