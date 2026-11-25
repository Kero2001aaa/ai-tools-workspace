# Shared safe operational views

Text, image and fixed-free text snapshots now share one explicit field list.
This includes failure-since-approval, approval count, total/provider/time/cost
caps, recorded cost and cancellation request. These are source-store counters,
not estimates of elapsed completion or evidence of a real user's approval.

Bounded text/image attempt history includes model/tool versions, tool ID,
failure class, retryable flag and known usage counters. Stored usage is
revalidated against a small fixed counter-key allowlist. No private error
summary, retry reason, fingerprint, raw output, content or provider body enters
the view. Returned usage is a copy. Unknown usage fields fail closed.

Strict response models match the added fields. Counts use actual store maxima
(20 attempts/calls, 600 seconds per task). Public run segments remain narrower.
Duplicate skips are stored separately from attempts and are not falsely reported
as new attempts; a separate bounded skip history is still pending. No public
endpoint, real owner-authentication or semantic-policy backend is added here.
