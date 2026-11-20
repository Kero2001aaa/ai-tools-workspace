# Phase 19 — Isolated LLM Specialist Workers

## Scope

The optional specialist MCP server exposes one tool, `delegate_specialist`, with three fixed roles:

- `research`: evidence-calibrated analysis of supplied material; it does not browse.
- `coding`: code/task analysis and proposed changes; it does not edit or execute code.
- `review`: critical review, risk identification, and test/acceptance criteria.

Each delegation is a single model inference with no MCP tools, filesystem, shell, browser, or external connectors. Inputs are bounded (4,000 task characters and 8,000 context characters); returned analysis is capped at 12,000 characters. Inputs are untrusted data.

## Enablement

Specialists are disabled by default. Add this to the private `.env` only when you want the main agent to be able to delegate:

```dotenv
ENABLE_SPECIALIST_AGENTS=true
```

Restart the agent process after changing environment configuration. The configured `MODEL_API_KEY`, `MODEL_BASE_URL`, and `MODEL_NAME` are used. A delegation invokes the configured model and can consume provider quota; no paid fallback is configured or implied. With no model configured, delegation fails closed.

## Boundaries

Specialists return analysis, not actions. They cannot create plans, mutate files, run commands, browse, or call other tools. The main agent remains responsible for validating the output and deciding whether to act. This phase does not yet add autonomous multi-agent loops, parallel delegation, or background execution.

## Verification

```bash
.venv/bin/python -m unittest tests.test_specialist_worker tests.test_tool_agent -v
.venv/bin/python -m unittest discover -s tests -v
```
