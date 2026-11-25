# Supervisor shared input budget

The existing supervisor now wraps the one selected model in an invocation-boundary
ledger shared by planning, both optional specialists and final review. Before each
call it validates text message content, counts its UTF-8 bytes, and refuses if that
call would exceed four attempted calls or 40,000 total content bytes. Arabic and
other non-ASCII text are charged by encoded bytes, not character count.

Attempt counters are charged before invoking the underlying model. Failed calls
stay charged and propagate without retry or fallback. The normal completed result
includes the attempted-call and content-byte totals and the byte cap. Failed
workflows still raise; no completed report or durable resume is invented.

The count covers message content only, not JSON/HTTP framing, schemas, outputs,
actual tokenizer tokens, billed cost or a provider's quota. The existing production
model response cap and free-route transport remain unchanged. The elapsed checks
still cannot interrupt a blocking inference call. Specialists remain tool-less;
this change grants no file, network, account or persistence capability.

Qualification uses injected models only. It is not proof of a live available
provider, real model quality or full Stage 5.2 acceptance.
