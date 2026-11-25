# Combined internal Adult application

AdultAgent composes the existing bounded text and image applications over one
shared task store, artifact store, fingerprint key and explicit task budget.
It contains no provider HTTP, SQL, automatic model certification or installation.
No constructor creates a default provider, transport or background worker.

The facade routes submission, safe metadata, mixed-modality task paging,
attempt history, bounded execution, explicit internal continuation/cancel and
task-owned result chunks to the existing applications. Metadata never includes
raw input or input/config/approach fingerprints. Mixed task lists now work at
this combined boundary; modality-specific facades remain fail-closed.

Image submission validates actual decoding and any declared MIME before
staging. Image analysis accepts an optional bounded prompt: it is validated
like generate/edit prompts, stored as its own task-scoped payload, bound into
the exact content fingerprint and restored only byte-identically. It reaches
providers strictly as marked untrusted user data, never as system or developer
instructions. Promptless analysis keeps its prior image-only envelope bytes.

Exact zero-attempt ephemeral restoration has separate text/image methods so
instruction and image/prompt shape stay explicit. Generic replacement_payload
is refused. Changed input and post-attempt replacement remain unsupported.
A continuation is an internal caller's explicit decision, not evidence of real
user consent. Existing limits are at most five attempts and 240 seconds per
segment, within total task budgets. This is narrower than the draft public
300-second maximum and is not presented as its full implementation.

No public MCP/HTTP endpoint, authenticated owner-approval source, semantic image
moderation or live zero-cost model availability is established by this facade.
The structural image checks do not prove safety or consent. Those release gaps
must stay visible before transport exposure. Tests use real stores with
explicit deterministic offline adapters and do not demonstrate AI inference.
