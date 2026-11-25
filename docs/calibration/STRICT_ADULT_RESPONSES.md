# Strict response contracts

Pure validation covers the existing safe task snapshot, bounded task page,
ordered attempt page, execution/cleanup envelope, and accepted artifact chunk.
Every nested object forbids additional properties. Failures are stable and do
not echo the value or raw exception. Unknown fields cause rejection, never
silent removal. Optional IDs/status/counters and timestamp timezone are checked.
Artifact data is strict base64, bounded to 256 KiB decoded, consistent with
size/offset/eof fields and MIME allowlist. A full-content chunk independently
checks its digest; a partial chunk's hash can only be verified after assembly.

The schema describes current safe internal fields. Attempt summaries do not yet
include tool IDs, usage and duplicate-skip detail from the full specification.
Task snapshots do not yet expose the full specification counters. These models
are not tool registration, endpoint authentication, semantic policy, or a claim
that the nine public operations are ready. Execution currently remains capped
at 240 seconds rather than the draft request schema's 300. ErrorResponse is a
closed stable error envelope; no arbitrary provider exception is supported.
