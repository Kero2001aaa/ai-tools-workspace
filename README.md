# AI Tools Workspace

A cloud-first, cost-conscious foundation for building an external AI agent workspace.

## Included

- Minimal GitHub Codespaces development container
- Python 3.12 with a workspace-local `.venv`
- LangGraph orchestration and MCP client dependencies
- Persistent bounded planner with checkpointed execution, a one-decision Supervisor, and specialist allowlisted read-only task handlers
- Optional OpenAI-compatible chat-model adapter
- Optional isolated LLM specialists and bounded supervisor orchestration (no tools or side effects)
- Offline mode that makes no model API calls when credentials are absent
- Optional Graphify code knowledge graph
- Optional public-only Agent Reach bridge for GitHub and V2EX
- Safe environment-variable template and automated tests

## Start

Open the repository, then select **Code → Codespaces → Create codespace on main**:

https://github.com/Kero2001aaa/ai-tools-workspace

After pulling updates, install dependencies and run the tests:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent "Explain LangGraph"
```

Start the control dashboard with `.venv/bin/python -m agent.control_plane`; open forwarded port 8000, keep it private, and enter the one-time token printed in the terminal to use the allowlisted task controls. See [docs/CONTROL_PLANE.md](docs/CONTROL_PLANE.md) for security boundaries.

See [docs/SUPERPOWERS_WORKFLOW.md](docs/SUPERPOWERS_WORKFLOW.md) for the engineering workflow adapted from Superpowers, [docs/SETUP.md](docs/SETUP.md) for model configuration and credential handling, [docs/AI_CAPABILITY_EXPANSION.md](docs/AI_CAPABILITY_EXPANSION.md) for the Phase 13.5 integration plan, and [docs/AUTOMATION.md](docs/AUTOMATION.md) for scheduled maintenance automation. The [specialist and supervisor guide](docs/SPECIALISTS.md) documents isolated workers and the bounded multi-agent workflow. The [task worker guide](docs/TASK_WORKER.md) covers the durable local queue and its allowlisted jobs. The [planning guide](docs/PLANNING.md) documents Phase 16 persistent planning and bounded autonomous execution; [Session Relay](docs/SESSION_RELAY.md) explains checkpoint and resume behavior.

## Important limitations

This project adds external tools and workflows; it cannot increase ChatGPT's native token quota, context window, or model capability. The model adapter is only a client interface: inference requires a separately available compatible endpoint. Hosted providers may charge for usage. No API keys are included.


- Dashboard Agent (opt-in): authenticated request execution is documented in docs/CONTROL_PLANE.md. It is disabled by default and may consume model/provider quota.
