# Strict Adult request contracts, validation only

adult_request_contracts defines the nine draft section8 request shapes with
Pydantic strict mode and additionalProperties=false. No FastMCP instance, tool
registration, gateway allowlist, dispatcher, default provider or state store is
created. validate_request returns a validated in-process model or the stable
invalid_adult_request error without echoing raw input or exception details.
Payload fields are excluded from model repr, but the returned model still holds
private content in memory and must never be logged or returned as metadata.

Validation covers exact integer/boolean types, UUID task IDs, opaque artifact
IDs, bounded text, strict base64, actual image decoding and declared MIME match.
Conflicting text/image replacement, cancellation with replacement and MIME
without image are refused. Image generation/edit prompts must be non-empty.
Unknown fields such as principal, namespace, SQL, paths, model keys or endpoints
are refused. This is not semantic moderation or proof of image consent.

These models describe the draft external contract, not implemented dispatch
support. In particular, a valid300-second request exceeds the current internal
facade240-second bound, and generic replacement still lacks the full
state-aware correction contract. The optional image-analysis prompt is now
supported end to end by the internal envelope, executor and adapter as marked
untrusted user data. Valid requests must not be silently truncated, dropped or
executed through an unfinished dispatcher. Request validation does
not authenticate a caller or approve a continuation. Public exposure remains
blocked on the explicit release boundaries and supported operation mapping.

Tests are pure offline validation. They establish neither live AI availability
nor a usable public MCP service. Output schemas, auth/approval provenance,
state-aware replacement, artifact-ID ownership mapping and transport are later
work, not supplied by this module.
