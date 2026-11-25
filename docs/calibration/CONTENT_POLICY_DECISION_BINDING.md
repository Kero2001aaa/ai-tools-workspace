# Fail-closed content-policy decision binding

This is an internal preparatory library, not a moderator or production policy.
No executor, provider, facade or public tool calls it yet. Existing policy gates
remain structural only. No live content policy or consent verification is proven.

A private injected backend reviews exact bytes for a task/operation/stage. Only
a typed verdict can issue a receipt. Backend errors, uncertain verdicts and
malformed responses deny. Receipt signatures bind task ID, operation, input or
output stage, keyed content fingerprint, policy version and a short expiry.
Caller/model-supplied fields cannot create a matching receipt. Each process has
its own random signing key; restart requires fresh review. No receipt or content
is persisted. Content, signature and fingerprint do not appear in receipt repr.
Input receipts cannot authorize output publication, another task, another
operation or changed bytes. Digest substitution and expiry changes invalidate it.

This boundary denies any backend uncertainty or flagged minor involvement.
Sexual real-person image generation/editing is always refused in this MVP,
even if a backend otherwise allows it. User attestation is not evidence of a
third person's consent; there is no consent override parameter. Those are
conservative boundary checks, not proof the backend detects such content.

Before public execution, a real private semantic backend must be selected,
verified for each supported input/output modality and reviewed for privacy/cost.
Wire exact canonical input reviews before routing and new output reviews before
publication, including cancellation/expiry race checks. Tests here use synthetic
verdicts only. They prove binding/denial mechanics, not semantic accuracy.
