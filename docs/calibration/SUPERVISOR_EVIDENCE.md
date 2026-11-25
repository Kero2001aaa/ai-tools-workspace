# Existing supervisor evidence handoff correction

Stage5.2candidate, reuses existing sequential supervisor and specialist workers.
No new provider, tool, model route, permissions, retries or autonomous execution.
The planner still chooses at most two distinct research/coding specialists; one
review runs last. At most four model calls. Actual provider/model availability is
not established and no live request is made by tests.

Final review previously received raw first8000characters of serialized JSON, which
could cut strings and omit later specialist results without saying so. It now
receives complete valid JSON capped8000serializedcharacters, with equal per-result
analysis allocation(ceiling3000each) and explicit original length, excerpt/upstream
truncation flags, source trust label and incomplete-evidence reminder. Request is
capped2000and task500with flags. Serialization escaping counts toward the cap;
if fixed overhead alone cannot fit, it refuses rather than send malformed JSON.
This is fair bounded projection, not a complete source record or semantic truth
check. A reviewer can still miss details in omitted text and must flag limits.

A final elapsed-time check now refuses completed status when final model review
returns after150seconds. It is an elapsed outcome check, NOT cancellation of a
blocking model invocation, hard end-to-end deadline, token cap or cost proof.
No extra calls/retries happen after failures. Existing non-text/Unicode malformed
specialist evidence is refused before handoff. Original full bounded specialist
results remain in the normal return, not silently replaced by excerpts. No new
persistence is added, and untrusted text is never executed or granted tools.

Tests use injected models only, including two long outputs, escaping expansion,
valid JSON/fair inclusion, malformed evidence and late final response. No claim
of Stage5.2acceptance or model-backed research completion follows from these tests.

A conservative minimum legal review-overhead check runs before model loading or
planning; impossible request-only overhead causes zero calls. Exact planned role
and task overhead is checked after planning but before specialists; impossible
planned overhead costs only the single planner call. Later output-dependent
failures still cannot be predicted without the call. Upstream truncated flags
must be actual booleans, never inferred from malformed values. Tests assert these
call counts. This is budget avoidance, not provider/token/cost qualification.
