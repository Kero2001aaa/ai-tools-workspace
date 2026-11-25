# Bounded image supervisor

AdultImageSupervisor reuses AdultImageExecutor's fenced lease, image intake,
AdultImagePipeline, route registry, attempt tracker, image evaluator, artifact
store and checkpoint finalization. There is one execution implementation. The
default executor.run remains single-attempt and pauses on failure, as before.

An explicit run_until_done(task_id, config_fingerprint, image=None, prompt=None,
max_attempts=5, max_seconds=240) library call enables a bounded segment. It may
continue only terminal retryable failures, selecting an excluded-approach-aware
route each time. Unknown, running, successful or nonretryable prior attempts
cannot replay. No paused task becomes generally resumable. Changed configuration
is rejected.

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
is 300 seconds, segment maximum 240 seconds. No unlimited-compute claim is made.
Policy rejection is terminal, not routed around. Missing routes pause with zero
additional attempts. Nonretryable failures and uncertain finalization stop.
Cancellation and caller lease fencing remain in force before each attempt.

Ephemeral image input keeps its existing recovery boundaries under supervision.
A zero-attempt task whose process-local input is lost moves to needs_input and
only exact validated content restores it. After a committed attempt, lost cached
input is a recovery matter, never a needs_input re-upload request: the segment
pauses with the pipeline's bounded error and no further route is dispatched,
because restore_input refuses that state.

All acceptance uses deterministic local test adapters and real stores. It proves
five distinct failures/no sixth, explicit synthetic decision/reopen/sixth result,
smaller segment/yield/distinct continuation, cap-on-fifth terminal failure,
unknown-outcome refusal, cancellation, nonretryable storage/evaluation failure,
output policy rejection, time-yield, process loss immediately before the
approval checkpoint, unstaged needs_input with zero calls, and cached-input loss
after a committed attempt pausing without a re-upload path. Synthetic decisions
test software, not real user authority.

No live provider is called by tests or publication. This module is not yet wired
to user-facing commands or deployed execution endpoints, and the image
composition remains internal and offline only: no supervision transport, semantic
content moderation, public endpoint, enabled live-image default or provider
capability certification is claimed. Existing provider unavailability remains; no
paid fallback, privacy relaxation or probe loop was added.
