# Bounded text journey

This increment reuses TaskStore, TextIntake, CapabilityRegistry, ModelRouter,
AttemptTracker, TextPipeline, Policies, Evaluator and ArtifactStore. No replacement
architecture or changes to control-plane routes.

`AdultTextExecutor` owns one task lease, finishes intake, creates one text step,
runs at most one pipeline attempt, checkpoints an evaluated result, then marks
the task completed. Reopening a completed task returns its result reference with
no adapter call. A prior attempt in a nonterminal task requires reconciliation,
not replay. Missing input becomes needs_input; no route or a failed attempt pauses.
Paused tasks currently require a separately implemented corrective action, not
an automatic resume or provider retry. This is deliberately not the five-failure
orchestration loop yet. A process crash between commits can leave a nonterminal
state; existing lease/uncertain-attempt recovery must reconcile it. No claim of
crash-atomic whole-journey execution is made.

## Runnable offline slice

From an installed project environment:

```sh
printf 'One safe text input.\nSecond line.' | python -m agent.adult.text_stats_cli
```

The command performs deterministic text counting, **not AI inference**. It uses
real isolated stores, a locally smoke-tested counting capability, the same bounded
executor and evaluated artifact path. The counting capability is never certified
as text generation, image support or an external provider. It calls no network.
It reads bounded UTF-8 from stdin, returns JSON containing counts and safe task
metadata, and removes its temporary state on exit. Input text is not echoed into
stdout, errors or persistent logs. Do not paste secrets into shell arguments.

The command is ephemeral by design. Durable user-request submission, five-failure wait/resume, lasting lessons, semantic quality,
production inference and web execution endpoints are later acceptance gates.
The generic executor only validates structural/policy acceptance already provided
by TextPipeline; it does not claim semantic correctness of an AI result.

## Verified checks

- Real task to artifact/checkpoint/completed state, reopen without duplicate call.
- Missing input, cancellation before/during call, zero-attempt unavailable route.
- One provider failure pauses without retry; finalization failure cannot replay.
- CLI stdin/output, empty/oversize/invalid UTF-8 rejection, network-disabled run.

All tests are offline. No live provider availability or image/training claim.

## Durable encrypted staging

`AdultTextExecutor.stage_encrypted(task_type, canonical_input, budget)` validates
input and requires a matching caller-configured `ADULT_PAYLOAD_ENCRYPTION_KEY`
before creating any task. It claims a lease, attaches encrypted input, saves a
checkpoint and yields without selecting a route or invoking any adapter. It does
not generate, print or persist the configured key. Reconstruct the same stores,
pipeline and cipher in a later process and call `run(task_id, None, config_fingerprint)`.
A successful result is encrypted by the existing ArtifactStore and survives reopen.

The configured key must remain available to the caller. This is a library API,
not a durable submission CLI, web endpoint or automatic scheduler. Existing input
and output retention bounds still apply. The task's wall deadline starts at task
creation and is not extended by staging or reopening, currently at most600seconds.
Staging is multiple fenced commits, not one crash-atomic operation. An uncertain
readback preserves committed input and pauses for reconciliation. Cancellation
wins; no model runs during staging. Neither paused corrective recovery nor
five-failure orchestration is implemented by this method.

## Interrupted pre-attempt recovery

After the existing store has recovered an expired lease, the executor can reuse
its running text step only when the persisted attempt count is zero and the
latest lease-recovery checkpoint names the same step and input. It preserves
the step ID, configuration and original deadline. The executor passes its own
TaskStore-issued lease through TextPipeline to atomic attempt creation, so an
old worker cannot start an attempt under a replacement worker's lease. Existing
direct pipeline/tracker callers retain their old API; they are not upgraded to
this fenced-executor contract merely by importing these modules.

No attempt after an unknown or committed prior attempt is replayed. No paused
task becomes resumable, no provider retry or five-failure loop is added, and a
manual-yield checkpoint alone cannot authorize a running-step recovery. Tests
simulate process loss both before and after begin(), plus a stale worker racing
a replacement lease. These checks are offline plumbing, not live inference.

## Durable offline command-line journey

For a caller who has already configured ADULT_PAYLOAD_ENCRYPTION_KEY securely,
these commands run in separate processes using the same local state directory:

```sh
python -m agent.adult.durable_stats_cli --state-dir .data/local-stats stage < safe-input.txt
python -m agent.adult.durable_stats_cli --state-dir .data/local-stats status TASK_ID
python -m agent.adult.durable_stats_cli --state-dir .data/local-stats run TASK_ID
python -m agent.adult.durable_stats_cli --state-dir .data/local-stats result TASK_ID
```

stage returns the opaque TASK_ID and checkpointed state after encrypted input
storage, zero attempts. status is keyless and opens the task database read-only;
no file or migration is created for missing state. run validates the retained
input/key before claiming and executes only deterministic counting. result reads
the encrypted completed result. Neither command is AI inference or an installed
service. The existing ephemeral text_stats_cli still works unchanged.

The caller supplies and retains their key, never in a command argument or source
file. Stage rejects missing keys and invalid input before creating state. A wrong
run key leaves the staged task checkpointed, rather than pausing and losing the
normal path. Original task deadline starts at stage and is not extended by run,
currently at most600seconds. Existing artifact retention and one-active-task
limits apply; this is not an indefinite queue. No provider request is made and
no paid route is available. run reuses the exact local counting capability and
fails closed for a revoked or conflicting record. It does not enable unrelated
capabilities. Commands return safe JSON only, source text is not echoed.

Tests launch actual separate processes for stage/status/run/result/reopen,
verify counts and no duplicate attempt, ciphertext/no output leak, key errors,
keyless no-write status, invalid input/no state and cancelled/no-call behavior.
Generic lessons are a separate explicit library path documented in
GENERIC_FEEDBACK_BRIDGE.md, not an automatic CLI publication side effect.
