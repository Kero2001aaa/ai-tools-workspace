# Phase 17 — Native Session Relay

This phase adds bounded run-until-done execution to the existing LangGraph + SQLite planner rather than introducing a second agent runtime.

## Execution model

- The existing planner remains the source of truth for plans and step state.
- `run_plan_until_done(plan_id, max_steps=3, max_seconds=60)` executes a foreground batch.
- The hard limits are 1–10 steps and 1–300 seconds per invocation.
- Each step is committed to SQLite before the next step begins.
- A completed plan returns `completed`; reaching a budget returns `checkpointed`; a failed step returns `paused`.
- A checkpointed plan can be resumed by invoking the tool again after inspecting its state.
- A failed step is never retried automatically. An explicit `retry_failed_plan_step` action is required.
- A running step with an expired ten-minute lease can be reclaimed on the next execution attempt.

## What this does not do

This is not an always-on daemon. It does not keep Codespaces alive, run after the Codespace stops, or guarantee execution across a suspended host. It persists progress so a later process can resume. It does not grant arbitrary shell or Python execution. Current step types remain read-only `workspace_status` and `memory_recent`.

## Cost and safety

The planner's local task execution does not call an LLM. The model may incur its normal provider quota when creating, inspecting, or resuming a plan through the conversational agent. There is no paid fallback. The runner stops at its configured per-invocation budgets; it does not silently extend them. Keep the Codespaces port private and do not expose control tokens.

## Verification

```bash
.venv/bin/python -m unittest tests.test_plan_store -v
.venv/bin/python -m unittest discover -s tests -v
```
