# Internal bounded image application

This is an internal composition, not the full AdultAgent contract or a public
MCP/HTTP endpoint. The caller injects a real AdultImageExecutor, a validated
fixed configuration fingerprint and an explicit TaskBudget. No provider is
registered, verified or invoked by constructor defaults. The caller must wire
the registry atomic image validator into its attempt tracker and supply only
separately verified adapters.

submit_image_task stages validated image/prompt input with ephemeral retention
by default and makes zero model calls. run_task executes at most one attempt;
there is no image retry supervisor or approval-resume API here. Exact zero-attempt
ephemeral restoration delegates to the executor's narrow restore path.

Read methods return bounded metadata, paged task lists, safe attempt history or
a task-owned result chunk. Callers do
not select artifact IDs or paths. Metadata reads have no cleanup side effects.

Completed or cancelled tasks trigger input-only cleanup, leaving task history
and accepted outputs intact. A cleanup failure is reported separately and
cannot hide completion or cause a provider replay. Explicit terminal run or
cancel retries input cleanup. This application does not change the lifecycle
contract of direct executor/store callers. Failed tasks retain input under the
existing retention policy; they are not presented as completed results.

Tests use deterministic offline adapters, real task/artifact stores, and
verified local test capability records. They do not demonstrate live image AI,
free provider availability, semantic quality, user approval or gateway access.

The existing image policy/evaluator gates are structural, not semantic
moderation or consent verification. This internal plumbing is not a claim of
readiness for unrestricted live image tasks or Adult content generation.
