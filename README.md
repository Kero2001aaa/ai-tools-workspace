# AI Tools Workspace

A cloud-first, cost-conscious foundation for building an external AI agent workspace.

## Included

- Minimal GitHub Codespaces development container
- Python 3.12 with a workspace-local `.venv`
- LangGraph orchestration and MCP client dependencies
- Optional OpenAI-compatible chat-model adapter
- Offline mode that makes no model API calls when credentials are absent
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

See [docs/SUPERPOWERS_WORKFLOW.md](docs/SUPERPOWERS_WORKFLOW.md) for the engineering workflow adapted from Superpowers, [docs/SETUP.md](docs/SETUP.md) for model configuration and credential handling, [docs/AI_CAPABILITY_EXPANSION.md](docs/AI_CAPABILITY_EXPANSION.md) for the Phase 13.5 integration plan, and [docs/AUTOMATION.md](docs/AUTOMATION.md) for scheduled maintenance automation. The [task worker guide](docs/TASK_WORKER.md) covers the durable local queue and its allowlisted jobs.

## Important limitations

This project adds external tools and workflows; it cannot increase ChatGPT's native token quota, context window, or model capability. The model adapter is only a client interface: inference requires a separately available compatible endpoint. Hosted providers may charge for usage. No API keys are included.
