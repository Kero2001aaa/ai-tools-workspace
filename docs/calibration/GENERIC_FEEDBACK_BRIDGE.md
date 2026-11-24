# Generic feedback bridge

This library slice reuses AdultTaskStore, AttemptTracker, AdultMemory and the
fixed write-only FeedbackPublisherStore. No new database, provider, MCP tool,
HTTP route, background publisher, installation or executor retry is introduced.

Construct AdultFeedback with the same real task store/tracker, fixed publisher
scope, AdultMemory and caller-owned keyed PayloadCipher. After a task yields a
reusable known failure, call build_proposal(task_id). This persists an immutable
structured proposal in Adult-private scoped memory. It does not publish it.
Call sanitize(proposal) to inspect the schema-validated generic record, and call
publish(proposal.feedback_id) separately to publish that exact durable proposal.
Reopening requires the same configured fingerprint key. No new key is generated.

The bridge re-derives fixed generic templates from bounded attempt metadata when
publishing, rather than trusting mutable proposal text or copying an error
message. It never reads task input, artifact bytes, provider responses, user
names or arbitrary logs. A domain-separated keyed task/attempt fingerprint is
published, not the input fingerprint or operational identifiers. Component ID
is the fixed adult-text-pipeline implementation, not an unverified adapter label.
Policy rejection, budget exhaustion, active tasks and unknown error codes produce
no proposal. MODEL_OUTPUT_INVALID is a deliberately recognized quality code even
though the existing tracker classifies it as unknown. Other unmapped codes are
not generalized. All published language is observation, not proof of a fix.

The existing publisher now accepts explicit idempotent=True. Its sanitized-data
digest lookup, ACL validation, insert and audit occur under BEGIN IMMEDIATE in
the existing memory database. Concurrent publication and retry after a committed
write with lost response return the same MemoryRef. Default direct publication
keeps its previous duplicate behavior. Repeated proposal builds may create more
than one private proposal, but they publish only one corresponding lesson.
No schema migration or persistent read permission is granted to the publisher.
Exact private-ID reads enforce the existing fixed namespace/owner ACL.

A later task can call AdultMemory.recall_shareable_workspace with the operation
and failure-code terms embedded in lesson content. Recall returns reference data
only. It cannot trigger calls, retries, routing, budget changes or installation.
Existing recall is term-ranked, not semantic validation or a guarantee of relevance.

This is not complete implementation-spec section9.3 orchestration. The spec's
adult_feedback_proposals operational table, proposal status lifecycle, private
publication receipt and automatic task-end wiring remain deferred. Private
structured proposals here use the existing memory namespace, not a substitute
outcome note that could lose the publication identity. Revalidation can fail
closed if the task is active, deleted, policy-rejected or the caller key changes.

Offline real-store acceptance covers failure/proposal/publication/reopen/next-task
recall, unchanged next-task attempt count, no input/output leaks, forged proposals,
wrong-scope IDs, concurrent idempotency, commit-then-lost-response retry, malformed
publication rejection, changed ACL, corrupt stored result and legacy semantics.
