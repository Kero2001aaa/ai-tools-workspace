# Optional local statistics MCP interface

Start explicitly with `python -m agent.adult.local_stats_mcp_server` over STDIO
only after the caller configures LOCAL_STATS_STATE_DIR and the existing
ADULT_PAYLOAD_ENCRYPTION_KEY securely. No secrets belong in arguments or source.
The module has no import-time database, directory, provider or network activity.

Five tools reuse the existing deterministic counting adapter and encrypted
executor: local_stats_stage, local_stats_get_task, local_stats_run,
local_stats_result and local_stats_cancel. Tool names deliberately do not claim
AI inference or generic Adult model execution. Stage and run are separate
explicit actions. Stage stores encrypted input and yields; get_task is read-only
and source-free; run makes at most one local counting attempt; result returns
counts, never the source text; cancel uses the existing durable cancellation.
Completed reruns return the same result without another attempt. The original
600second task deadline and artifact retention bounds still apply.

This server is not registered in build_workspace_server_config, the universal
gateway, the model agent or the default18tool inventory. Starting it is an
operator opt-in for a local trusted MCP client, not an approval bridge for
untrusted model output. It has no HTTP bind, generic shell/file tools, provider
routing, automatic feedback publication, or resume/approval tool. The bounded
supervisor remains an independently tested library contract. No external service
was called or deployed for this interface.

All operation errors are fixed codes rather than paths, raw input or exceptions.
Tests cover real STDIO schema discovery and separate server-process
stage/restart/run/result, direct cancellation/no replay, unchanged default agent
and gateway inventory, no import-time state, missing configuration/state and
invalid input. This is usable offline counting plumbing, not usable live AI.
