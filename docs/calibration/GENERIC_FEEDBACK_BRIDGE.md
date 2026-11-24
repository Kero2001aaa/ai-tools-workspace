# Generic feedback bridge

This library slice reuses AdultTaskStore, AttemptTracker, AdultMemory and the
fixed write-only FeedbackPublisherStore. No new database, provider, MCP tool,
HTTP route, background publisher, installation or executor retry is introduced.
An additive v4 migration adds the specification's operational proposal table
to the existing task database.

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
No persistent read permission is granted to the publisher.
Exact private-ID reads enforce the existing fixed namespace/owner ACL.

A later task can call AdultMemory.recall_shareable_workspace with the operation
and failure-code terms embedded in lesson content. Recall returns reference data
only. It cannot trigger calls, retries, routing, budget changes or installation.
Existing recall is term-ranked, not semantic validation or a guarantee of relevance.

The v4 operational table now mirrors each validated private proposal with an
immutable UUID, task reference, category, sanitized summary, structured feedback,
source fingerprint, original private-memory ID and created time. Explicit publish
records proposed -> published with an immutable exact published-memory ID and
time. Existing integer private proposal IDs remain compatible. A legacy memory-
only proposal is mirrored on its next explicit publish, after full revalidation.

Publication and receipt are in separate existing databases, so no cross-database
atomicity is claimed. The publisher commits idempotently first. If writing or
returning the receipt fails, a retry revalidates the proposal, current ACL and
published record integrity, obtains the same publication ID and records the
receipt. Concurrent callers and retries cannot replace the receipt. A status read
returns private proposal/receipt metadata only after current proposal validation.
A valid legacy proposal with no operational row has no receipt until publish.

This is still not complete implementation-spec section 9.3 orchestration.
Automatic task-end wiring is deferred. Private structured proposals remain in the
existing Adult memory namespace as well as the operational record. Revalidation
can fail closed if the task is active, policy-rejected or the caller key changes.
No proposal or receipt read starts execution, installs an integration or publishes
anything automatically.

Offline real-store acceptance covers failure/proposal/publication/reopen/next-task
recall, unchanged next-task attempt count, no input/output leaks, forged proposals,
wrong-scope IDs, concurrent idempotency, commit-then-lost-response retry, malformed
publication rejection, changed ACL, corrupt stored result and legacy semantics.

Migration tests cover actual v1/v2/v3 databases upgraded to v4, preservation of
migration history, failed-v4 rollback, read-only refusal of unmigrated stores,
current read-only reopen without writes, canonical schema validation, immutable
proposal/receipt fields, legacy proposal recovery, lost receipt before commit and
lost response after receipt commit. Existing TaskStore and registry upgrade tests
now explicitly remove later schemas when constructing older fixtures. A partial
or gapped migration ledger remains refused.

Canonical validation now checks owned table SQL as well as indexes/triggers.
A same-name feedback table with removed constraints is refused on reopen.
