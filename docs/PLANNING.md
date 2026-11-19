# Phase 16 — Persistent bounded planning

The planner adds durable, resumable plans without introducing arbitrary code execution.

## MCP tools

- `create_plan(title, steps)`: stores a plan with 1–10 ordered steps.
- `get_plan(plan_id)`: reads the plan, statuses, and bounded results.
- `list_plans(limit)`: lists recent plans.
- `run_next_plan_step(plan_id)`: claims and executes exactly one step.
- `run_plan_until_done(plan_id, max_steps=3, max_seconds=60)`: runs a bounded batch, committing the result/checkpoint after each step. Valid limits are 1–10 steps and 1–300 seconds. Invoke again to resume a checkpointed plan.
- `supervise_plan(plan_id, max_steps=3, max_seconds=60)`: inspect current state and make exactly one bounded decision: execute a batch, stop, or wait. It never spins, retries failures, or launches background work.
- `retry_failed_plan_step(plan_id, step_id)`: explicitly requeues one failed step; it does not execute it.

## Allowed step types

Only these local read-only operations are accepted:

- `workspace_status` — report Python version, workspace path, and timestamp. Payload must be empty.
- `memory_recent` — retrieve 1–20 recent local memories. Payload accepts only an optional integer `limit`.

Plans are limited to ten steps; titles and descriptions are bounded. Unknown task types, extra payload fields, shell commands, Python snippets, URLs, and arbitrary tool names are rejected.

## Execution and recovery

Plan states are `queued` (not started), `running` (a step is claimed), `checkpointed` (completed steps are saved and pending steps remain), `paused` (a step failed), and `completed`. The persisted plan state now reports `checkpointed` after each successful non-final step, matching the run result.

The task worker dispatches through separate, strictly validated handlers for `workspace_status` and `memory_recent`. These are deterministic specialist workers, not independent LLM agents.

Plan metadata and step results are stored in `.data/plans.sqlite3` (override with `WORKSPACE_PLAN_DB`). `run_next_plan_step` executes exactly one step; `run_plan_until_done` may execute a bounded batch. Its time budget is checked between steps and cannot interrupt a step already in progress. A failure pauses the plan; a user must explicitly request retry before the failed step is requeued. A step left running for more than ten minutes is eligible for recovery on the next execution attempt. `run_plan_until_done` is an explicit foreground run, not a background daemon: it stops at its per-run step/time budget, records each completed step in SQLite, and can be invoked again to resume. It never retries a failed step automatically. If the Codespace stops, no worker remains running; saved plans can resume when the workspace is started again.

The planner is registered as a local MCP stdio server in the same way as the task queue. No new Python package, hosted service, or API key is required. Plan steps themselves do not call the model or external research providers; the model may use its normal quota when it reasons about a request or calls planner tools.

## Verification

```bash
.venv/bin/python -m unittest tests.test_plan_store tests.test_tool_agent -v
.venv/bin/python -m agent.mcp_client
```

Expected discovery includes six planner tools. Full suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

This is bounded autonomous execution, not unrestricted code execution or a persistent always-on scheduler. The current plan task allowlist remains read-only (`workspace_status`, `memory_recent`). There is no automatic paid-model fallback, arbitrary shell, or side-effecting task execution. Only add future step types with narrow schemas, source review, tests, explicit permission boundaries, and approval gates for side effects.

## Multi-tool agent budget

The LangGraph tool-calling loop defaults to 25 graph steps per request so a bounded multi-step plan can finish and return a final answer. Override with `AGENT_RECURSION_LIMIT` (valid range 5–50). This is a loop guard, not permission to execute more plan steps in one planner call: each `run_next_plan_step` still executes exactly one task.
