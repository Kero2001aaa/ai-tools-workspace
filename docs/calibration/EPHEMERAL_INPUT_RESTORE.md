# Narrow exact transient input restoration

Implemented as `restore_input_and_run` on the fixed service/application.
This slice restores only the exact text envelope of a zero-attempt ephemeral
fixed-free task in needs_input. A changed task/instruction is rejected by the
original keyed fingerprint. It is not a general replacement/edit API, encrypted
input repair, provider retry permission or the full Adult resume contract.

The application must validate envelope/type/fingerprint and current state before
putting bytes under the original process-local input reference. The artifact
store may not overwrite a persistent input or a different cache entry. A dedicated
operational-store transaction (`adult_task_restore.py`) checks needs_input, ephemeral retention, zero attempts,
original reference/fingerprint, expected state version, cancellation and budgets
before queuing. Original deadline and limits do not reset. The existing checkpoint
and step retain their immutable original input reference. No task IDs, payload
IDs or fingerprints are changed.

Cache insertion precedes the state commit. If the state commit/readback fails,
input remains transient and bounded rather than deleted under a possibly
committed task. An explicit service restoration call runs at most one fixed-free
segment after the queue commit; a terminal/cancelled/active task cannot be
restored. No model call occurs for mismatched input or failed restore validation.
The caller is an explicitly invoked trusted operator/application. No public MCP
or HTTP approval source is added by this change.

Tests cover: exact reupload and bounded mock completion; mismatch preserves
needs_input/cache absence; cancellation wins; expired deadline cannot execute;
new process original reference remains usable; unknown commit/readback never
causes duplicate dispatch; no persistent plaintext; non-ephemeral and prior
attempt tasks refused. Full waiting-user approval and changed replacement input
remain separate work.

A queue commit followed by another process restart is also handled. The missing
transient input is observed before routing, and the existing claim/release lease
path returns that queued task to needs_input. No attempt is made, and an exact
reupload can be supplied again. An actual subprocess test disables the synthetic
route first, restores the original reference, and confirms zero attempts without
network. The fixture uses the registry's guarded disable method.

The input restoration is an explicit trusted application/operator action. The
lower operational-store function verifies metadata, not content; the service checks the
keyed fingerprint and the artifact store verifies the exact cache bytes. No
public transport, model-selected input repair or new user-approval semantics are
introduced. Existing encrypted input behavior remains unchanged.
