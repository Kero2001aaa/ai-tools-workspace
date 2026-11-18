# Setup and operating guide

## 1. Create the Codespace

1. Open https://github.com/Kero2001aaa/ai-tools-workspace
2. Select **Code** → **Codespaces** → **Create codespace on main**.
3. Wait for the container setup to finish.
4. Open the terminal inside the browser-based VS Code.

The repository connection can prepare files and commits, but it cannot create or operate your Codespace session.

## 2. Update and verify the environment

The project uses Python 3.12 and a local virtual environment. After pulling repository changes, run:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent "Research LangGraph"
```

The test suite should report ten tests and `OK`. With no model credentials, the CLI runs in offline mode.

## 3. Optional zero-budget inference with OpenRouter

The project can use OpenRouter's OpenAI-compatible API with free model routing. OpenRouter currently documents a free plan with 50 requests per day; free model variants are also limited to 20 requests per minute. Limits and model availability can change.

1. Create an OpenRouter account and generate an API key: https://openrouter.ai/keys
2. In the Codespace, open the local `.env` file. Do not edit `.env.example` with real credentials.
3. Add the following values:

```dotenv
MODEL_API_KEY=your_openrouter_api_key
MODEL_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=openrouter/free
```

4. Save `.env`, then run:

```bash
.venv/bin/python -m agent "Explain the difference between LangGraph nodes and edges"
```

The `openrouter/free` router selects an available free model. This is not a guaranteed model identity, fixed quality level, or unlimited service. Free model availability, context limits, and rate limits vary. Do not purchase credits or switch to a paid model for this zero-budget project.

OpenRouter free-model information: https://openrouter.ai/collections/free-models/
OpenRouter pricing and limits: https://openrouter.ai/pricing

### Data and credential precautions

- Requests are sent to OpenRouter and then to the selected model provider. Review the applicable provider data policies before use.
- Do not send passwords, API keys, private source code, confidential work material, or sensitive personal information.
- Keep the API key only in the Codespace's ignored `.env` file. Never paste it into chat, commit it, or place it in source code.
- The current model adapter makes a request only when the CLI runs with complete model configuration.
- If you want offline-only operation, leave all three `MODEL_*` variables blank.

## 4. Generic compatible endpoint configuration

Other OpenAI-compatible providers can be configured using the same variables:

```dotenv
MODEL_API_KEY=your_provider_key
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_NAME=your-model-id
```

Verify current pricing, free-tier eligibility, rate limits, and data handling before configuring another provider. Do not attach billing or enable paid inference.

## 5. Local MCP server (Stage 3)

A minimal local MCP server is included. It exposes two read-only tools:

- `text_stats(text)` — counts characters, words, and lines in text supplied by the caller.
- `workspace_status()` — reports the workspace label and Python version.

It does not execute shell commands, read arbitrary files, access environment variables, or make network requests.

After pulling the latest changes, run the tests and discover the tools:

```bash
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m agent.mcp_client
```

The MCP client starts the local server over standard input/output, discovers its tools, prints their names and descriptions, and exits. This verifies the MCP transport and adapter. The tools are now wired into the model's reasoning loop in Stage 4 below. Review each future server's code and permissions before connecting it.

## 6. Model-driven MCP tool calling (Stage 4)

The tool-calling workflow is implemented in `agent/tool_agent.py`. It gives the configured chat model access only to the two local MCP tools described above. LangGraph routes model tool calls to the MCP tools, returns their results to the model, and stops after a bounded number of graph steps.

Run it after configuring a model:

```bash
.venv/bin/python -m agent.tool_agent "Count the words and characters in this sentence: CNC plasma cutting uses a controlled electrical arc."
```

This command makes a model API request. It requires a model that supports tool/function calling; a free-model router may select a model that does not. If the selected model rejects tool definitions or does not call tools, choose a currently available tool-capable model and verify its pricing is still zero before using it. Do not add credits or enable paid routing. The graph has a recursion limit of ten steps to prevent unbounded tool loops.

The MCP tools currently perform only local text counting and runtime-status reporting. They do not provide web search, file access, shell execution, or persistent memory.

## 7. Recommended build order

1. **LangGraph** — workflow and orchestration; implemented and tested.
2. **Chat-model adapter** — configurable OpenAI-compatible interface; live inference was verified with the configured free-model route.
3. **Local MCP server and client** — implemented; local transport and tool discovery verified.
4. **Model tool-calling loop** — implemented; the included test uses a fake model to verify tool execution without spending API requests.
5. **Persistent memory** — choose a storage backend, retention policy, and deletion process before adding Mem0 or an alternative.
6. **Research providers** — evaluate Exa and Firecrawl; configure keys only if needed.
7. **Prompt compression** — evaluate LLMLingua only after measuring context and resource requirements.
8. **MCP security scanning** — scan third-party servers before enabling them.
9. **Browser automation** — add only after deciding which browser and authentication model to use.

Do not install every repository blindly. Components can overlap, require separate services, consume significant memory, or need paid API credentials. Add and test one component at a time.

## 8. What this setup does not do

- It does not increase ChatGPT's native context window, token allowance, or model permissions.
- It does not connect these services to this ChatGPT conversation automatically.
- It does not provision paid cloud resources or API subscriptions.
- It does not store credentials in GitHub.
- It does not guarantee a free provider's uptime, model availability, or quality.

## 9. Control Codespaces costs

Stop Codespaces when they are not in use. Check the Codespaces page for active instances and stop unused ones. Review your GitHub billing and included quota before leaving a Codespace running.
