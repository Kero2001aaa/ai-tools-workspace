# Text intake integration candidate

Status: offline draft, no production entry point enabled.

## Gap found

Task creation assigns task_id before payload storage, while the artifact store
requires task_id to save an input. AdultTaskRequest previously required its final
payload_id at creation and had no supported attach path. Mock-only pipeline tests
could not detect this circular dependency.

## Additive contract

1. Canonically encode a text envelope and compute its keyed fingerprint.
2. Create a task without payload_id, then claim the task normally.
3. AdultTextIntake validates the envelope, input fingerprint, optional instruction
   fingerprint, and encryption-key identity before saving task-owned input.
4. AdultTaskStore.attach_input permits one opaque payload reference only while
   analyzing, with the original pending intake step, zero attempts, current
   state version, and a current store-issued lease. Cancellation wins.
5. Failed attachment first reads the committed task request. If it already owns
   this input, preserve it and report a readback failure. If commit state cannot
   be read, preserve retention-bound data and report unknown state. Only a
   confirmed unattached input is removed. Cleanup errors are explicit. Storage
   and task database are not a distributed transaction:
   a process crash between writes can leave a bounded-retention orphan. It must
   not cause execution; recovery is not claimed to repair this automatically.
6. The caller owns subsequent step creation, checkpoints, lease renewal, task
   completion and retries. This is not an executor, supervisor or UI endpoint.

The storage primitive trusts the internal intake caller to verify ownership and
fingerprint, as the existing step-store primitives do. It is not an untrusted
client API. No payload bytes enter task metadata, events or checkpoints.

## Tests and limits

Actual SQLite TaskStore, ArtifactStore, CapabilityRegistry, Router,
AttemptTracker, policy and evaluator now participate in a single text-pipeline
integration test. Only the adapter output is deterministic offline test code.
The test demonstrates one tracked attempt and a retrievable artifact without a
provider call, paid route, or automatic retry. Tests also cover duplicate attach,
stale versions, wrong-task lease, malformed and changed input, encrypted reopen,
plaintext exclusion, exact cleanup and input/output ownership separation.

This does not certify live inference, semantic quality, image input, image
generation/editing, five-failure orchestration, production lifecycle wiring, or
final task completion. The production free-route guard still denies tools,
plugins and image parts. The prior unavailable-inference response is unchanged.
