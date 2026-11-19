# Phase 16 — Persistent bounded planning

The planner adds durable, resumable plans without introducing arbitrary code execution.

## MCP tools

- `create_plan(title, steps)`: stores a plan with 1–10 ordered steps.
- `get_plan(plan_id)`: reads the plan, statuses, and bounded results.
- `list_plans(limit)`: lists recent plans.
- `run_next_plan_step(plan_id)`: claims and executes exactly one step.
- `retry_failed_plan_step(plan_id, step_id)`: explicitly requeues one failed step; it does not execute it.

## Allowed step types

Only these local read-only operations are accepted:

- `workspace_status` — report Python version, workspace path, and timestamp. Payload must be empty.
- `memory_recent` — retrieve 1–20 recent local memories. Payload accepts only an optional integer `limit`.

Plans are limited to ten steps; titles and descriptions are bounded. Unknown task types, extra payload fields, shell commands, Python snippets, URLs, and arbitrary tool names are rejected.

## Execution and recovery

Plan metadata and step results are stored in `.data/plans.sqlite3` (override with `WORKSPACE_PLAN_DB`). Each call executes at most one step. A failure pauses the plan; a user must explicitly request retry before the failed step is requeued. A step left running for more than ten minutes is eligible for recovery on the next execution attempt. The next step is not run automatically.

The planner is registered as a local MCP stdio server in the same way as the task queue. No new Python package, hosted service, or API key is required. Plan steps themselves do not call the model or external research providers; the model may use its normal quota when it reasons about a request or calls planner tools.

## Verification

```bash
.venv/bin/python -m unittest tests.test_plan_store tests.test_tool_agent -v
.venv/bin/python -m agent.mcp_client
```

Expected discovery includes five planner tools. Full suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

This is bounded planning, not unrestricted autonomous execution or a scheduler. Only add future step types with narrow schemas, source review, tests, explicit permission boundaries, and approval gates for side effects.

## Multi-tool agent budget

The LangGraph tool-calling loop defaults to 25 graph steps per request so a bounded multi-step plan can finish and return a final answer. Override with `AGENT_RECURSION_LIMIT` (valid range 5–50). This is a loop guard, not permission to execute more plan steps in one planner call: each `run_next_plan_step` still executes exactly one task.
