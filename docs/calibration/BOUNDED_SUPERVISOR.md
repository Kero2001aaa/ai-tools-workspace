# Bounded text supervisor

AdultSupervisor reuses AdultTextExecutor's fenced lease, input attachment,
TextPipeline, route registry, attempt tracker, evaluator, artifact store and
checkpoint finalization. There is one execution implementation. The default
executor.run remains single-attempt and pauses on failure, as before.

An explicit run_until_done(task_id, config_fingerprint, canonical_input=None,
max_attempts=5, max_seconds=240) library call enables a bounded segment. It may
continue only terminal retryable failures, selecting an excluded-approach-aware
route each time. Known prior failures may continue from a matching checkpoint;
unknown, running, successful or nonretryable prior attempts cannot replay.
No paused task becomes generally resumable. Changed configuration is rejected.

At five failed attempts since the last approval, the existing set_waiting_user
transaction stores the approval checkpoint and releases the lease. No sixth
attempt runs. Waiting tasks cannot run. A caller must persist an explicit continue
or cancel decision through the existing resume API; continue_after_approval only
accepts already-queued tasks with recorded approval and never grants consent.
The existing authenticated tool boundary must own that decision when connected.
This library is not a new approval endpoint and has no background scheduler.

Total task attempts, provider calls, cost and wall deadline remain bounded. On a
fifth failure with no remaining budget the task fails, rather than asking for
unusable approval. A segment yields checkpointed after its smaller attempt/time
bound. max_seconds is checked between synchronous calls, not hard preemption of a
blocked adapter; adapters still need their own timeout contract. The task lease
is300seconds, segment maximum240seconds. No unlimited-compute claim is made.
Policy rejection is terminal, not routed around. Missing routes pause with zero
additional attempts. Nonretryable failures and uncertain finalization stop.
Cancellation and caller lease fencing remain in force before each attempt.

All acceptance uses deterministic local test adapters and real stores. It proves
five distinct failures/no sixth, explicit synthetic decision/reopen/sixth result,
smaller segment/yield/distinct continuation, cap-on-fifth terminal failure,
unknown-outcome refusal, cancellation, nonretryable storage/evaluation failure,
output policy rejection, time-yield, and process loss immediately before the
approval checkpoint. Synthetic decisions test software, not real user authority.

No live provider is called by tests or publication. This module is not yet wired
to user-facing commands or deployed execution endpoints. Existing provider429
unavailability remains; no paid fallback, privacy relaxation or probe loop was
added. Generic feedback remains a separate explicit proposal/publish library.
