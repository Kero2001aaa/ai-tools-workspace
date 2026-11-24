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

The command is ephemeral by design. Durable user-request submission, encrypted
restartable input, five-failure wait/resume, lasting lessons, semantic quality,
production inference and web execution endpoints are later acceptance gates.
The generic executor only validates structural/policy acceptance already provided
by TextPipeline; it does not claim semantic correctness of an AI result.

## Verified checks

- Real task to artifact/checkpoint/completed state, reopen without duplicate call.
- Missing input, cancellation before/during call, zero-attempt unavailable route.
- One provider failure pauses without retry; finalization failure cannot replay.
- CLI stdin/output, empty/oversize/invalid UTF-8 rejection, network-disabled run.

All tests are offline. No live provider availability or image/training claim.
