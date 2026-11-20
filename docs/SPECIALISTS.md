# Phase 20 — Bounded supervisor orchestration

## What it does

The optional specialist MCP server exposes `supervise_request(request, context="")`. It runs a single sequential workflow:

1. One supervisor model call creates a JSON plan with one or two distinct assignments, selected only from `research` and `coding`.
2. The selected specialists run sequentially. Each receives only its bounded task and context and has no tools or side effects.
3. A fixed `review` specialist runs last with the original request and the collected findings.
4. The supervisor returns each result plus a deterministic synthesis. It does not make another model call to write the synthesis.

## Hard limits

- At most 4 model calls: one plan, up to two analysis workers, and one review.
- At most 3 specialist calls; no parallel dispatch.
- One planning attempt and no automatic retries.
- Request: 4,000 characters; context: 4,000 characters.
- Each generated assignment: 1,000 characters.
- Review context: 8,000 characters.
- Workflow wall-clock budget: 150 seconds; model HTTP timeout: 30 seconds per request.
- Invalid model plans fail closed. Only `research` and `coding` can be planned; `review` is reserved for the final step.

## Enable and run

Set `ENABLE_SPECIALIST_AGENTS=true` in the private `.env` file, or enable it for a single command:

```bash
ENABLE_SPECIALIST_AGENTS=true .venv/bin/python -m agent.mcp_client
```

Then ask the tool-calling agent to use `supervise_request` for a task that benefits from multiple perspectives:

```bash
ENABLE_SPECIALIST_AGENTS=true .venv/bin/python -m agent.tool_agent "Use supervise_request to assess the security and maintainability of this design: store API keys in source code."
```

The workflow uses the configured model endpoint and consumes its quota. It does not guarantee that the endpoint supports every requested model feature. Keep paid routing disabled for this zero-budget project.

## Safety boundaries

- Specialists are tool-less and cannot browse, execute commands, read files, or modify the repository.
- The supervisor only passes bounded text between model calls.
- Model-generated plans are parsed and validated against a strict role/schema allowlist.
- No files, plans, tasks, or external systems are changed.
- A failed call stops the workflow; there is no retry or background execution.
- The wall-clock budget is checked between calls; the provider's per-request timeout bounds an in-flight model request.
- The synthesis is a formatted aggregation of the specialist outputs, not a separate model-generated conclusion. The review findings must be considered before acting.

## Verification

```bash
.venv/bin/python -m unittest tests.test_supervisor_worker tests.test_specialist_worker -v
.venv/bin/python -m unittest discover -s tests -v
```
