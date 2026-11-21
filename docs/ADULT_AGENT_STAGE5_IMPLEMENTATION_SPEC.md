# Adult Agent — Stage 5 Attempt Tracker and Retry De-duplication

## Status and scope

This document is the Stage 5 execution contract. It adds durable attempt evidence, failure classification, stable approach de-duplication, retry audit, and recovery of attempts whose worker lease was lost.

New implementation:

- \`agent/adult/adult_attempt_tracker.py\`
- \`tests/test_adult_attempt_tracker.py\`

The operational database receives an additive schema migration from version 1 (\`adult_operational_store_v1\`) to version 2 (\`adult_attempt_tracking_v2\`). Existing task, step, checkpoint, and task-event rows remain unchanged. The migration creates only \`adult_attempts\`, \`adult_attempt_events\`, their indexes, and integrity triggers. It must execute in the same \`BEGIN IMMEDIATE\` migration transaction and remain idempotent.

Stage 5 does not add model/provider adapters, provider HTTP calls, routing or capability discovery, an Executor, a Supervisor, MCP registration, automatic retries, or paid routes.

## Attempt lifecycle and durable guarantees

- An attempt is inserted with status \`running\` in a committed transaction before the provider adapter is invoked.
- Starting an attempt atomically increments \`adult_tasks.total_attempts\`; it also increments \`provider_calls\` only for a \`free_verified\` non-local provider route. Local routes do not consume provider-call budget.
- Attempt completion occurs in a separate transaction and changes the row exactly once from \`running\` to one terminal state: \`succeeded\`, \`failed\`, \`rejected\`, \`timed_out\`, or \`unknown_outcome\`.
- A terminal attempt cannot be modified or deleted. Attempt audit events are append-only.
- Attempt start stores the task's current lease generation. A late completion from a worker holding an older generation is rejected as stale.
- The tracker never treats a crash, timeout, lost lease, or ambiguous provider response as success.
- \`recover_uncertain_attempts()\` changes a still-running attempt to \`unknown_outcome\` only after the task's lease generation has advanced or its lease has been cleared by TaskStore recovery. It never re-dispatches the provider operation.
- \`failed\`, \`timed_out\`, and \`unknown_outcome\` increment \`failed_attempts_since_approval\` (capped at five). \`rejected\` is reserved for a provider policy rejection and does not count as a retryable failure. A new \`begin\` is refused when the five-failure checkpoint threshold has been reached; Stage 9's Supervisor will persist \`waiting_user\` and the checkpoint.
- Attempts are bounded by the TaskStore lifetime maximum of 20 and provider-call budget. No sixth failure can be followed by a new attempt before the later explicit approval workflow.

## Attempt schema v2

\`adult_attempts\` stores only bounded operational metadata:

- opaque attempt/task/step identifiers and monotonically increasing attempt number;
- stable approach key and route identity;
- adapter/provider/model/tool identifiers and versions;
- task type, keyed input fingerprint, non-secret configuration fingerprint, optional environment/capability fingerprint;
- task lease generation at dispatch;
- status, failure class, retryable flag and optional sanitized retry reason;
- normalized error code and bounded sanitized error summary;
- output artifact reference/fingerprint (never output bytes or text);
- bounded usage JSON containing numeric counters only;
- reported cost in micro-USD, start/finish timestamps and elapsed milliseconds.

No raw prompt, instruction, image, base64, URL, provider request/response body, secret, credential, local path, or unbounded error text may be stored.

\`adult_attempt_events\` is a separate append-only audit log. Event types are limited to attempt start/terminal transitions, duplicate-approach suppression, retry authorization, and uncertain-outcome recovery. Event metadata contains only task/attempt IDs, approach key, normalized status/error codes, failure class, bounded counters, and timestamps. It never contains raw input or provider bodies.

Required indexes:

- task + attempt number (descending reads);
- task + approach key (de-duplication);
- one running attempt per task (partial unique index);
- task + audit-event creation time.

Required triggers:

- attempt identity/route/fingerprint/lease-generation/start fields are immutable;
- only a \`running\` attempt may transition once to an allowed terminal status;
- attempt rows cannot be deleted;
- attempt audit events cannot be updated or deleted.

## Stable approach key and retry authorization

The approach key is SHA-256 over canonical UTF-8 JSON (sorted keys, compact separators, no NaN) containing exactly:

- task type;
- adapter ID/version;
- provider ID;
- model ID/version;
- tool ID/version;
- configuration fingerprint;
- input fingerprint;
- environment/capability fingerprint (empty string when absent).

Secrets are never part of the canonical representation. The resulting key is a 64-character lowercase hexadecimal digest.

A prior attempt with the same task and approach key is never dispatched again. A repeat is audited as \`approach_skipped\` and raises \`DuplicateApproach\`, whether the prior attempt failed, timed out, became uncertain, was rejected, is still running, or already succeeded. A retry with a different approach key is allowed only if it is a genuinely different route family or the caller supplies a non-empty \`retry_reason\` that explains a material input/configuration/environment/version change.

A route family is the task type plus adapter/provider/model/tool identifiers (not their mutable configuration or input fingerprints). If the same route family has prior attempt history and the new approach key differs, \`retry_reason\` is mandatory and is recorded in both the new attempt and a \`retry_authorized\` event. A new route family does not require retry_reason. Identical approach keys cannot be forced through by providing retry_reason.

### Contract clarification

The architecture interface's \`begin(...)\` signature omitted a place to pass the already-required material-change explanation. Stage 5 adds an optional keyword-only \`retry_reason: str | None = None\`. This is backwards compatible for callers and makes the explicit retry authorization auditable. The deviation is documented here before implementation.

## Failure classification

Normalized classes:

- \`transient\`: provider timeout, connection reset, rate limit, provider 5xx, temporary local backend outage;
- \`capability_mismatch\`: route lacks the required modality or operation;
- \`permanent_request\`: invalid schema/input, unsupported input, corrupt image, invalid configuration;
- \`policy_rejection\`: upstream policy refusal; terminal \`rejected\`, never routed around;
- \`budget_exhaustion\`: attempt/time/provider/cost limit reached; no new dispatch;
- \`unknown\`: unclassified failure, stopped by default unless the trusted caller explicitly marks a bounded distinct route as retryable.

Only \`failed\`, \`timed_out\`, and \`unknown_outcome\` increment the five-failure count. Policy rejections are recorded as \`rejected\`, are not treated as transient, and cannot be used to route around upstream safety decisions.

The public \`fail(handle, error_code, sanitized_error, retryable)\` contract remains. The tracker derives the failure class from a strict normalized error-code map and validates retryable against that class. It will not accept \`retryable=true\` for policy rejection, permanent request errors, capability mismatch, or budget exhaustion.

## Public API

The implementation provides the architecture contract:

\`\`\`python
class AdultAttemptTracker:
    def begin(
        self, task_id: str, step_id: str, route: RouteCandidate,
        input_fingerprint: str, config_fingerprint: str,
        *, retry_reason: str | None = None,
    ) -> AttemptHandle: ...

    def succeed(
        self, handle: AttemptHandle, output_ref: str | None,
        usage: dict | None, cost_microusd: int | None,
    ) -> AttemptResult: ...

    def fail(
        self, handle: AttemptHandle, error_code: str,
        sanitized_error: str, retryable: bool,
    ) -> AttemptResult: ...

    def find_prior_approach(self, task_id: str, approach_key: str) -> AttemptSummary | None: ...
    def failures_since_approval(self, task_id: str) -> int: ...
    def total_attempts(self, task_id: str) -> int: ...
    def recover_uncertain_attempts(self) -> int: ...
\`\`\`

Handles are immutable and issued only by the tracker. Finishing an attempt validates its task ID, attempt ID, and stored lease generation. A caller cannot construct a valid handle by supplying arbitrary IDs.

## Validation and privacy rules

- Task must exist, be non-terminal and active under a current unexpired lease before begin.
- Step must belong to the task; step/task cross-association is rejected.
- Route task type must match the task.
- Only \`local\` and \`free_verified\` routes are accepted in the zero-cost MVP. \`paid\` and \`unknown\` cost classes fail closed before attempt creation.
- Fingerprints must be normalized keyed/non-secret digests. Route identifiers and versions must be bounded opaque identifiers.
- Usage JSON allows only a small key allowlist and non-negative integer values. Booleans, arbitrary strings, prompt text, request bodies and nested objects are rejected.
- Cost and elapsed values reject booleans, negatives and out-of-range values.
- Sanitized errors/retry reasons are bounded, reject URLs, credential indicators, home-directory paths and raw payload-like fields.
- Database SQL is parameterized. The tracker exposes no raw SQL, file path, provider key, or namespace API.

## Acceptance tests

\`tests/test_adult_attempt_tracker.py\` must cover:

1. schema-v1 to v2 additive migration preserves task/step/checkpoint/event rows and is idempotent;
2. new database initializes directly at the latest schema;
3. attempt start commits before terminal outcome and survives a fresh store instance;
4. success, failure, timeout, policy rejection and unknown outcome finalize exactly once;
5. failed/timed-out/unknown outcomes increment failure count; rejected/succeeded do not;
6. total-attempt and provider-call limits fail closed before a new row is inserted;
7. same approach key is suppressed and audit event persists across restart;
8. same route family with material config/input/environment change requires a non-empty retry reason;
9. a genuinely different route family can proceed without retry_reason;
10. retry authorization is persisted and contains no secret/configuration value;
11. crash after begin is recovered to unknown_outcome only after lease-generation recovery;
12. a stale worker cannot finish an attempt after task lease generation changes;
13. concurrent begin calls cannot create two running attempts or exceed budgets;
14. invalid routes, mismatched steps, malformed fingerprints, unsafe error/retry strings, arbitrary usage fields and invalid cost values are rejected;
15. attempt and event rows are immutable under direct SQL update/delete;
16. plaintext source/provider strings are absent from attempt rows, usage JSON, audit metadata and log capture.

The Stage 5 gate requires all ATT-* tests, the Stage 2 MEM-* tests, Stage 3 TASK-* tests, Stage 4 payload security tests, the full unit suite, protected-path review, and CI/security checks to pass. No provider call or live database migration is used by these tests.
